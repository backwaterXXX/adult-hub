"""Direct, source-aware proof required for every duplicate hide."""
from __future__ import annotations

import hashlib
import json
import re

PHASH_GENERATOR = 'stash-phash-689a6b400625d6da2afb58a7f908c88bcdb6f8ab-v2:resolver-ffmpeg-stash-v2'
VISUAL_POLICY = 'strict-5-of-8-v2'


def _source(row):
    return hashlib.sha256('\0'.join(str(row.get(k) or '') for k in ('id', 'duration', 'file_size_bytes', 'oshash')).encode()).hexdigest()


PART_REGEX = re.compile(
    r'(?i)(?<![A-Za-z0-9])(?:(vol(?:ume)?\.?|part|pt\.?|ep(?:isode)?\.?|sc(?:ene)?\.?|ch(?:apter)?\.?|cd)[\s_.-]*(\d{1,5})|(s)[\s_.-]*(\d{1,2}))(?!\d)'
)


def _normalize_marker_kind(prefix: str) -> str:
    p = prefix.lower().rstrip('.')
    if p in ('vol', 'volume'):
        return 'vol'
    if p in ('part', 'pt'):
        return 'part'
    if p in ('ep', 'episode'):
        return 'ep'
    if p in ('sc', 'scene'):
        return 'sc'
    if p in ('ch', 'chapter'):
        return 'ch'
    if p == 'cd':
        return 'cd'
    if p == 's':
        return 's'
    return p


def extract_part_identity(row: dict) -> tuple[bool, int | tuple[tuple[str, int], ...] | None]:
    split_source = bool(str(row.get('scene_match_source') or '').casefold().endswith('split_title_duration_v1'))
    text = ' '.join(str(row.get(k) or '') for k in ('source_filename', 'source_text', 'title'))
    markers: dict[str, int] = {}
    conflict = False
    for match in PART_REGEX.finditer(text):
        if match.group(1):
            kind = _normalize_marker_kind(match.group(1))
            num = int(match.group(2))
        else:
            kind = 's'
            num = int(match.group(4))
        if kind in markers and markers[kind] != num:
            conflict = True
            break
        markers[kind] = num

    if conflict:
        return True, None
    if not markers:
        return split_source, None
    if len(markers) == 1:
        return True, next(iter(markers.values()))
    return True, tuple(sorted(markers.items()))


def _part(row):
    return extract_part_identity(row)


def _telegram_document_id(video_id: str) -> str | None:
    clean = str(video_id or "").strip()
    if not clean.startswith("telegram:"):
        return None
    ref = clean[len("telegram:"):]
    parts = ref.rsplit(":", 2)
    if len(parts) == 3 and parts[0] and parts[1].isascii() and parts[1].isdigit() and parts[2]:
        return parts[2]
    return None


def pair_evidence(conn, left_id: str, right_id: str) -> dict | None:
    left_id, right_id = sorted((left_id, right_id))
    if left_id == right_id:
        return None
    rows = conn.execute('''SELECT v.id,v.duration,v.file_size_bytes,v.title,v.source_filename,v.source_text,
          v.scene_match_source,o.fingerprint AS oshash,p.fingerprint AS phash
        FROM videos v LEFT JOIN video_fingerprints o ON o.video_id=v.id AND o.algorithm='OSHASH'
        LEFT JOIN video_fingerprints p ON p.video_id=v.id AND p.algorithm='PHASH'
          AND EXISTS (SELECT 1 FROM video_phash_provenance pp WHERE pp.video_id=p.video_id
              AND pp.fingerprint=p.fingerprint AND pp.generator_version=%s)
        WHERE v.id=ANY(%s) ORDER BY v.id''', (PHASH_GENERATOR, [left_id,right_id])).fetchall()
    names = ('id','duration','file_size_bytes','title','source_filename','source_text','scene_match_source','oshash','phash')
    if len(rows) != 2:
        return None
    a, b = (dict(zip(names, row)) for row in rows)
    doc_a = _telegram_document_id(a['id'])
    doc_b = _telegram_document_id(b['id'])
    same_tg_doc = bool(doc_a and doc_b and doc_a == doc_b)
    try:
        x, y = float(a['duration'] or 0), float(b['duration'] or 0)
        if x > 0 and y > 0:
            if abs(x-y) > min(15, max(5, .01*max(x,y))):
                return None
        elif not same_tg_doc:
            return None
    except (ValueError, TypeError):
        if not same_tg_doc:
            return None
    ap, an = _part(a)
    bp, bn = _part(b)
    if (ap or bp) and (not an or an != bn):
        return None
    if conn.execute('SELECT 1 FROM video_duplicate_dismissals WHERE left_id=%s AND right_id=%s AND active=1', (left_id,right_id)).fetchone():
        return None
    if same_tg_doc:
        return {'type': 'telegram_file_id', 'file_id': doc_a}
    if a['oshash'] and a['oshash'] == b['oshash']:
        return {'type': 'oshash'}
    try:
        ah, bh = str(a['phash'] or ''), str(b['phash'] or '')
        if re.fullmatch(r'[0-9a-fA-F]{16}', ah) and re.fullmatch(r'[0-9a-fA-F]{16}', bh):
            distance = (int(ah,16) ^ int(bh,16)).bit_count()
            if distance <= 10:
                return {'type': 'phash', 'distance': distance}
    except ValueError:
        pass
    row = conn.execute('''SELECT evidence FROM video_visual_comparisons WHERE left_id=%s AND right_id=%s
        AND state='confirmed' AND algorithm_version='visual-sequence-v1' AND left_source_key=%s AND right_source_key=%s''',
        (left_id,right_id,_source(a),_source(b))).fetchone()
    if row:
        evidence = row[0]
        if isinstance(evidence, str):
            try:
                evidence = json.loads(evidence)
            except ValueError:
                evidence = {}
        if isinstance(evidence,dict) and evidence.get('confirmation_policy') == VISUAL_POLICY:
            return {'type': 'visual_sequence', 'policy': VISUAL_POLICY}
    return None
