"""Conservative, repeatable automatic duplicate reconciliation."""

from __future__ import annotations

import re

from common import db

RULES_VERSION = 10
PHASH_ALGORITHM_VERSION = "stash-phash-689a6b400625d6da2afb58a7f908c88bcdb6f8ab-v2"
PHASH_DECODER_PROFILE = "resolver-ffmpeg-stash-v2"
VISUAL_ALGORITHM_VERSION = "visual-sequence-v1"
MAX_PHASH_ENQUEUES = 25
MAX_AUTO_LOSERS = 25
VISUAL_SIGNATURES_PER_RUN = 100
VISUAL_PAIR_CHECKS_PER_RUN = 100_000
VISUAL_VERIFICATIONS_PER_RUN = 25


def _as_json(value):
    if isinstance(value, str):
        try:
            import json
            return json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def _part_identity(row: dict) -> tuple[bool, int | None]:
    split_source = str(row.get("scene_match_source") or "").casefold().endswith(
        "split_title_duration_v1")
    text = " ".join(str(row.get(key) or "") for key in
                    ("source_filename", "source_text", "title"))
    marker = re.search(r"(?i)(?<![A-Za-z0-9])(?:part[\s_.-]*|pt\.?[\s_.-]*|s[\s_.-]*)(\d{1,2})(?!\d)", text)
    part = int(marker.group(1)) if marker and 1 <= int(marker.group(1)) <= 12 else None
    return split_source or part is not None, part


def _duration_matches(left: dict, right: dict) -> bool:
    try:
        a, b = int(left.get("duration") or 0), int(right.get("duration") or 0)
    except (TypeError, ValueError):
        return False
    return a > 0 and b > 0 and abs(a - b) <= min(15, max(5, .01 * max(a, b)))


def _telegram_file_id(video_id: str) -> str | None:
    parts = str(video_id or "").rsplit(":", 2)
    return parts[2] if len(parts) == 3 and parts[0].startswith("telegram:") else None


def _quality(row: dict) -> tuple:
    probe = _as_json(row.get("media_probe_data"))
    heights = [int(stream.get("height") or 0) for stream in probe.get("streams", [])
               if isinstance(stream, dict) and stream.get("codec_type") == "video"]
    height = max(heights, default=0)
    size = int(row.get("file_size_bytes") or probe.get("file_size_bytes") or 0)
    source_text = str(row.get("source_text") or "")
    source_filename = str(row.get("source_filename") or "")
    return (int(bool(row.get("playable"))), height, size,
            len(str(row.get("title") or "")), len(source_text), len(source_filename),
            str(row.get("id") or ""))


def _reconcile_visual(db_url: str | None, pairs: list[dict]) -> dict:
    """Queue frame work only for active duplicate candidates."""
    import time
    from common.video_visual_jobs import ALGORITHM_VERSION, enqueue_pair, enqueue_signature
    from common.duplicate_evidence import pair_evidence
    try:
        from sync.video_visual import source_key, reevaluate_evidence
    except ModuleNotFoundError:
        from video_visual import source_key, reevaluate_evidence
    now = int(time.time())
    conn = db.connect(db_url)
    try:
        conn.row_factory = db.Record
        conn.execute("INSERT INTO video_visual_state(id,algorithm_version,report_started_at,automatic_enabled_at) VALUES (1,%s,%s,%s) ON CONFLICT (id) DO UPDATE SET automatic_enabled_at=COALESCE(video_visual_state.automatic_enabled_at,EXCLUDED.automatic_enabled_at)", (ALGORITHM_VERSION, now, now))
        state = dict(conn.execute("SELECT * FROM video_visual_state WHERE id=1").fetchone())
        counts = {row[0]: int(row[1]) for row in conn.execute("SELECT kind,count(*) FROM video_visual_jobs WHERE state IN ('queued','processing') GROUP BY kind").fetchall()}
        candidates = set()
        for pair in pairs:
            members = pair.get("members") or [pair.get("left"), pair.get("right")]
            ids = sorted({str(x['id']) for x in members if isinstance(x, dict) and x.get('id')})
            dismissed = {tuple(sorted(map(str, x))) for x in pair.get('dismissed_pairs', []) if len(x) == 2}
            for i, left in enumerate(ids):
                for right in ids[i+1:]:
                    if (left, right) not in dismissed:
                        candidates.add((left, right))
        ids = sorted({item for pair in candidates for item in pair})
        # Old catalog-wide queued work no longer consumes Telegram bandwidth.
        conn.execute("UPDATE video_visual_jobs SET state='cancelled',updated_at=CURRENT_TIMESTAMP WHERE state='queued' AND ((kind='signature' AND NOT (video_id=ANY(%s))) OR (kind='verify_pair' AND NOT EXISTS (SELECT 1 FROM unnest(%s::text[],%s::text[]) AS p(l,r) WHERE p.l=left_id AND p.r=right_id)))", (ids, [p[0] for p in sorted(candidates)], [p[1] for p in sorted(candidates)]))
        counts = {row[0]: int(row[1]) for row in conn.execute("SELECT kind,count(*) FROM video_visual_jobs WHERE state IN ('queued','processing') GROUP BY kind").fetchall()}
        rows = conn.execute("""SELECT v.id,v.provider,v.duration,v.file_size_bytes,f.fingerprint AS oshash,s.source_key
            FROM videos v LEFT JOIN video_fingerprints f ON f.video_id=v.id AND f.algorithm='OSHASH'
            LEFT JOIN video_visual_signatures s ON s.video_id=v.id AND s.algorithm_version=%s
            WHERE v.id=ANY(%s)""", (ALGORITHM_VERSION, ids)).fetchall() if ids else []
        videos = {str(row['id']): dict(row) for row in rows}
        # Reclassify complete saved measurements under the new policy locally.
        from psycopg.types.json import Jsonb
        from common.video_visual_jobs import CONFIRMATION_POLICY_VERSION
        old_rows = conn.execute("SELECT id,left_id,right_id,left_source_key,right_source_key,evidence FROM video_visual_comparisons WHERE state IN ('confirmed','review','different','inconclusive') AND evidence->>'confirmation_policy' IS DISTINCT FROM %s", (CONFIRMATION_POLICY_VERSION,)).fetchall()
        for old in old_rows:
            left, right = videos.get(str(old['left_id'])), videos.get(str(old['right_id']))
            if not left or not right or source_key(left) != old['left_source_key'] or source_key(right) != old['right_source_key']:
                continue
            evidence = _as_json(old['evidence'])
            outcome = reevaluate_evidence(evidence)
            if outcome is not None:
                evidence = dict(evidence, confirmation_policy=CONFIRMATION_POLICY_VERSION, confirmation_rule='five_strict_six_loose' if outcome == 'confirmed' else None)
                conn.execute("UPDATE video_visual_comparisons SET state=%s,evidence=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s", (outcome, Jsonb(evidence), old['id']))
        needed = []
        for left, right in sorted(candidates):
            a, b = videos.get(left), videos.get(right)
            if not a or not b or a['provider'] != 'telegram' or b['provider'] != 'telegram' or not _duration_matches(a,b):
                continue
            if pair_evidence(conn, left, right):
                continue
            needed.append((left,right))
        needed_ids = sorted({item for pair in needed for item in pair})
        conn.execute("UPDATE video_visual_jobs SET state='cancelled',updated_at=CURRENT_TIMESTAMP WHERE state='queued' AND ((kind='signature' AND NOT (video_id=ANY(%s))) OR (kind='verify_pair' AND NOT EXISTS (SELECT 1 FROM unnest(%s::text[],%s::text[]) AS p(l,r) WHERE p.l=left_id AND p.r=right_id)))", (needed_ids, [p[0] for p in needed], [p[1] for p in needed]))
        counts = {row[0]: int(row[1]) for row in conn.execute("SELECT kind,count(*) FROM video_visual_jobs WHERE state IN ('queued','processing') GROUP BY kind").fetchall()}
        conn.commit()
    finally:
        conn.close()
    sig_limit = max(0, min(VISUAL_SIGNATURES_PER_RUN, 200-counts.get('signature',0)))
    pair_limit = max(0, min(VISUAL_VERIFICATIONS_PER_RUN, 200-counts.get('verify_pair',0)))
    sig_count = pair_count = 0
    for left, right in needed:
        ready = True
        for video_id in (left,right):
            row = videos[video_id]
            if row.get('source_key') != source_key(row):
                ready = False
                if sig_count < sig_limit and enqueue_signature(db_url, video_id):
                    sig_count += 1
        if ready and pair_count < pair_limit and enqueue_pair(db_url,left,right):
            pair_count += 1
    return {"visual_signatures_queued": sig_count, "visual_pairs_scanned": len(needed),
            "visual_verifications_queued": pair_count, "visual_report_started_at": int(state['report_started_at']),
            "visual_automatic_enabled_at": state['automatic_enabled_at']}


def reconcile(db_url: str | None, *, limit: int = MAX_AUTO_LOSERS,
              execute_merges: bool = True) -> dict:
    """Queue missing pHashes and reversible hides for high-confidence matches."""
    from common.job_queue import enqueue_jobs_batch
    from common.phash_jobs import enqueue as enqueue_phash
    import video_duplicates
    import video_duplicate_scoring as duplicate_rules

    scan = video_duplicates.scan_duplicate_index(db_url)
    visual = _reconcile_visual(db_url, scan.get("pairs", [])) if scan.get("complete") else {}
    if not scan.get("complete"):
        conn = db.connect(db_url)
        try:
            conn.execute("UPDATE video_visual_jobs SET state='cancelled',updated_at=CURRENT_TIMESTAMP WHERE state='queued'")
            conn.commit()
        finally:
            conn.close()
        return {"outcome": "incomplete_scan", **visual, "scanned": scan.get("scanned", 0),
                "phash_queued": 0, "merge_jobs_queued": 0}

    pairs = scan.get("pairs", [])
    ids = list(dict.fromkeys(str(member.get("id")) for pair in pairs
                             for member in pair.get("members", [pair.get("left"), pair.get("right")])
                             if isinstance(member, dict) and member.get("id")))
    conn = db.connect(db_url)
    try:
        conn.row_factory = db.Record
        rows = conn.execute(
            """SELECT v.id,v.title,v.duration,v.playable,v.file_size_bytes,v.scene_match_source,
                      v.scene_match_external_id,v.source_filename,v.source_text,v.performers,
                      f.fingerprint,f.media_probe_data
               FROM videos v LEFT JOIN video_fingerprints f
                 ON f.video_id=v.id AND f.algorithm='OSHASH'
               WHERE v.id=ANY(%s)""", (ids,),
        ).fetchall() if ids else []
        by_id = {str(row["id"]): dict(row) for row in rows}
        phash_rows = conn.execute(
            """SELECT f.video_id,f.fingerprint FROM video_fingerprints f
               JOIN video_phash_provenance p ON p.video_id=f.video_id
                 AND p.fingerprint=f.fingerprint AND p.generator_version=%s
               WHERE f.algorithm='PHASH' AND f.video_id=ANY(%s)""",
            (PHASH_ALGORITHM_VERSION + ":" + PHASH_DECODER_PROFILE, ids),
        ).fetchall() if ids else []
        phashes = {str(row[0]): str(row[1]) for row in phash_rows}
        active_losers = {str(row[0]) for row in conn.execute(
            "SELECT loser_id FROM video_duplicate_decisions WHERE state='active'").fetchall()}
        visual_state = conn.execute("SELECT automatic_enabled_at FROM video_visual_state WHERE id=1").fetchone()
        visual_pairs = []
        if visual_state and visual_state[0]:
            visual_pairs = conn.execute("""SELECT c.left_id,c.right_id,c.evidence
                FROM video_visual_comparisons c
                JOIN video_visual_signatures ls ON ls.video_id=c.left_id AND ls.source_key=c.left_source_key
                JOIN video_visual_signatures rs ON rs.video_id=c.right_id AND rs.source_key=c.right_source_key
                WHERE c.state='confirmed' AND c.algorithm_version=%s
                  AND NOT EXISTS (SELECT 1 FROM video_duplicate_dismissals d
                    WHERE d.active=1 AND d.left_id=c.left_id AND d.right_id=c.right_id)""",
                (VISUAL_ALGORITHM_VERSION,)).fetchall()
            visual_ids = list(dict.fromkeys(str(value) for row in visual_pairs for value in row[:2]))
            missing_ids = [value for value in visual_ids if value not in by_id]
            if missing_ids:
                extra = conn.execute("""SELECT v.id,v.title,v.duration,v.playable,v.file_size_bytes,v.scene_match_source,
                    v.scene_match_external_id,v.source_filename,v.source_text,v.performers,f.fingerprint,f.media_probe_data
                    FROM videos v LEFT JOIN video_fingerprints f ON f.video_id=v.id AND f.algorithm='OSHASH'
                    WHERE v.id=ANY(%s)""", (missing_ids,)).fetchall()
                by_id.update({str(row["id"]): dict(row) for row in extra})
    finally:
        conn.close()

    phash_jobs = 0
    for video_id in ids:
        row = by_id.get(video_id)
        if not row or video_id in phashes or row.get("duration") is None:
            continue
        if not _duration_matches(row, row):
            continue
        try:
            outcome = enqueue_phash(
                db_url, video_id=video_id, duration_us=int(row["duration"]) * 1_000_000,
                algorithm_version=PHASH_ALGORITHM_VERSION,
                decoder_profile=PHASH_DECODER_PROFILE, user_id="duplicate-reconcile",
            )
            if outcome.get("outcome") == "queued":
                phash_jobs += 1
        except Exception:
            continue
        if phash_jobs >= MAX_PHASH_ENQUEUES:
            break

    auto_groups = {}
    skipped = 0
    for visual in visual_pairs:
        left, right = by_id.get(str(visual[0])), by_id.get(str(visual[1]))
        if left and right:
            pairs.append({"evidence": "visual_sequence", "left": left, "right": right,
                          "members": [left, right], "visual_evidence": visual[2]})
    for pair in pairs:
        evidence = str(pair.get("evidence") or "")
        members = pair.get("members") or [pair.get("left"), pair.get("right")]
        members = [by_id[str(member["id"])] for member in members
                   if isinstance(member, dict) and str(member.get("id")) in by_id]
        if len(members) < 2 or evidence not in {
                "telegram_file_id", "oshash", "phash", "stashdb_scene", "phash_near", "visual_sequence"}:
            continue
        keeper_pool = [row for row in members if row["id"] not in active_losers]
        if len(keeper_pool) < 2:
            continue
        keeper_pool.sort(key=_quality, reverse=True)
        keeper = keeper_pool[0]
        safe_losers = []
        for loser in keeper_pool[1:]:
            if not _duration_matches(keeper, loser):
                skipped += 1
                continue
            keeper_split, keeper_part = _part_identity(keeper)
            loser_split, loser_part = _part_identity(loser)
            from common.duplicate_evidence import pair_evidence
            evidence_conn = db.connect(db_url)
            try:
                proof = pair_evidence(evidence_conn, str(keeper["id"]), str(loser["id"]))
            finally:
                evidence_conn.close()
            if not proof:
                skipped += 1
                continue
            dismissed = {tuple(sorted(map(str, item))) for item in pair.get("dismissed_pairs", [])
                         if isinstance(item, (list, tuple)) and len(item) == 2}
            if tuple(sorted((str(keeper["id"]), str(loser["id"])))) in dismissed:
                continue
            safe_losers.append(str(loser["id"]))
        if not safe_losers:
            continue
        # The hide worker and quarantine snapshots are deliberately bounded.
        # Remaining members stay visible and are picked up by a later pass.
        safe_losers = safe_losers[:MAX_AUTO_LOSERS]
        auto_groups[(str(keeper["id"]), tuple(sorted(safe_losers)))] = {
            "keeper_id": str(keeper["id"]), "loser_ids": sorted(safe_losers),
            "rules_version": RULES_VERSION, "automatic": True, "evidence": evidence,
        }

    work = []
    claimed_ids: set[str] = set()
    if execute_merges:
        for payload in auto_groups.values():
            group_ids = {payload["keeper_id"], *payload["loser_ids"]}
            if group_ids & claimed_ids:
                continue
            work.append(("hide", payload))
            claimed_ids.update(group_ids)
            if len(work) >= max(1, min(limit, MAX_AUTO_LOSERS)):
                break
    job_ids = enqueue_jobs_batch(db_url, work)
    return {"outcome": "completed", **visual, "scanned": scan.get("scanned", 0),
            "phash_queued": phash_jobs, "merge_jobs_queued": len(job_ids),
            "merge_candidates": len(auto_groups), "mode": "automatic" if execute_merges else "report",
            "skipped_safety_checks": skipped}
