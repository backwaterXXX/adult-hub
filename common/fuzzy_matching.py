"""Database scan and grouping for fuzzy performer candidates."""

import re
from collections import defaultdict
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Set, Tuple


def find_fuzzy_performer_candidates(db_url=None, similarity_threshold=0.88, *, _core):
    """Scan the database for potential duplicate performers."""
    conn = _core["connect"](db_url or _core["DB_URL"] or None)
    try:
        tables = _core["table_names"](conn)

        # 1. Zliczanie wystapien w filmach
        video_counts: Dict[str, int] = defaultdict(int)
        video_examples: Dict[str, List[Dict[str, str]]] = defaultdict(list)
        all_performers_set: Set[str] = set()

        if "videos" in tables:
            rows = conn.execute(
                "SELECT id, title, provider, studio, performers FROM videos "
                "WHERE performers <> '[]'::jsonb ORDER BY id"
            ).fetchall()
            for video_id, title, provider, studio, raw in rows:
                try:
                    arr = _core["_json_list"](raw)
                    if isinstance(arr, list):
                        for p in arr:
                            if isinstance(p, str):
                                p_clean = p.strip()
                                if p_clean and _core["is_valid_performer_name"](p_clean):
                                    video_counts[p_clean] += 1
                                    all_performers_set.add(p_clean)
                                    if len(video_examples[p_clean]) < 5:
                                        video_examples[p_clean].append({
                                            "id": str(video_id or ""),
                                            "title": str(title or ""),
                                            "provider": str(provider or ""),
                                            "studio": str(studio or ""),
                                        })
                except Exception:
                    pass

        # 2. Zdjecia i biografie
        photos_set: Set[str] = set()
        if "performer_photos" in tables:
            for (n,) in conn.execute("SELECT name FROM performer_photos").fetchall():
                if n and isinstance(n, str) and _core["is_valid_performer_name"](n.strip()):
                    photos_set.add(n.strip())
                    all_performers_set.add(n.strip())

        bios_set: Set[str] = set()
        if "performer_bios" in tables:
            for (n,) in conn.execute("SELECT name FROM performer_bios").fetchall():
                if n and isinstance(n, str) and _core["is_valid_performer_name"](n.strip()):
                    bios_set.add(n.strip())
                    all_performers_set.add(n.strip())

        known_canonical_set: Set[str] = set()
        alias_map: Dict[str, str] = {}
        if "performer_aliases" in tables:
            for (a, c) in conn.execute("SELECT alias, canonical FROM performer_aliases").fetchall():
                if a and c:
                    a_clean = str(a).strip().lower()
                    c_clean = str(c).strip()
                    if a_clean and c_clean:
                        alias_map[a_clean] = c_clean
                        if _core["is_valid_performer_name"](c_clean):
                            known_canonical_set.add(c_clean)

        all_names = sorted(all_performers_set)

        # Mapy pomocnicze
        clean_map: Dict[str, List[str]] = defaultdict(list)
        for name in all_names:
            ck = _core["normalize_clean_key"](name)
            if ck:
                clean_map[ck].append(name)

        groups: List[Dict[str, Any]] = []
        handled_pairs: Set[Tuple[str, str]] = set()

        def make_group(
            group_id: str,
            category: str,
            category_label: str,
            confidence: float,
            confidence_level: str,
            members: List[str],
            reason_template: str,
        ) -> Optional[Dict[str, Any]]:
            valid_members = [m for m in members if _core["is_valid_performer_name"](m)]
            if len(valid_members) < 2:
                return None
            members = valid_members

            # Choose canonical name based on scoring
            scores = {
                m: _core["score_canonical"](
                    m,
                    has_photo=m in photos_set,
                    has_bio=m in bios_set,
                    video_count=video_counts.get(m, 0),
                    is_known_canonical=m in known_canonical_set,
                )
                for m in members
            }
            canonical = max(members, key=lambda m: (scores[m], len(m)))
            raw_variants = [m for m in members if m != canonical]

            # Filter out variants that have already been aliased/merged with canonical
            variants = []
            for var in raw_variants:
                var_target = alias_map.get(var.lower())
                canon_target = alias_map.get(canonical.lower())
                if (
                    (var_target and var_target.lower() == canonical.lower())
                    or (canon_target and canon_target.lower() == var.lower())
                    or (var_target and canon_target and var_target.lower() == canon_target.lower())
                ):
                    continue
                variants.append(var)

            if not variants:
                return None

            # Sprawdzenie czy wszystkie pary nie byly juz zarejestrowane
            for var in variants:
                pair = tuple(sorted([var, canonical]))
                if pair in handled_pairs:
                    return None
                handled_pairs.add(pair)

            var_details = []
            for var in variants:
                ratio = SequenceMatcher(
                    None, _core["normalize_clean_key"](var), _core["normalize_clean_key"](canonical)
                ).ratio()
                var_details.append({
                    "name": var,
                    "video_count": video_counts.get(var, 0),
                    "video_examples": video_examples.get(var, []),
                    "has_photo": var in photos_set,
                    "has_bio": var in bios_set,
                    "similarity_ratio": round(ratio, 3),
                })

            canon_reason = reason_template.format(
                canonical=canonical,
                count=video_counts.get(canonical, 0),
            )

            return {
                "id": group_id,
                "category": category,
                "category_label": category_label,
                "confidence": confidence,
                "confidence_level": confidence_level,
                "canonical": canonical,
                "canonical_score": round(scores[canonical], 1),
                "canonical_reason": canon_reason,
                "canonical_video_count": video_counts.get(canonical, 0),
                "canonical_video_examples": video_examples.get(canonical, []),
                "canonical_has_photo": canonical in photos_set,
                "canonical_has_bio": canonical in bios_set,
                "variants": var_details,
            }

        # -------------------------------------------------------------------------
        # Category 1 & 2: Diacritics and Spacing/Case/Punctuation
        # -------------------------------------------------------------------------
        for ck, members in sorted(clean_map.items()):
            if len(members) <= 1:
                continue

            no_acc_set = {_core["strip_accents"](m).lower() for m in members}
            raw_lower_set = {m.lower() for m in members}

            if len(no_acc_set) < len(raw_lower_set):
                # Differences in accents (e.g. Artyóm vs Artyom)
                grp = make_group(
                    group_id=f"diacritic_{ck}",
                    category="diacritics",
                    category_label="Diacritics (accents)",
                    confidence=0.95,
                    confidence_level="high",
                    members=members,
                    reason_template="Preferred spelling with diacritics / most common ({count} videos)",
                )
            else:
                # Differences in spacing, letter case, or delimiters
                grp = make_group(
                    group_id=f"spacing_{ck}",
                    category="spacing_case_punct",
                    category_label="Case / spacing / punctuation",
                    confidence=0.92,
                    confidence_level="medium",
                    members=members,
                    reason_template="Standard capitalization and spacing ({count} videos)",
                )

            if grp:
                groups.append(grp)

        # -------------------------------------------------------------------------
        # Category 3: Suffixes / Nicknames (appended VIP, XXX, numbers, etc.)
        # -------------------------------------------------------------------------
        suffix_clusters: Dict[str, Set[str]] = defaultdict(set)
        for name in all_names:
            stem, sfx = _core["extract_suffix_stem"](name)
            if stem and stem in clean_map:
                stem_canonical_candidates = clean_map[stem]
                for cand in stem_canonical_candidates:
                    if cand != name:
                        # Requirement: for single words, stem must have at least 4 characters
                        if len(stem) >= 4:
                            pair = tuple(sorted([name, cand]))
                            if pair not in handled_pairs:
                                suffix_clusters[stem].add(name)
                                suffix_clusters[stem].add(cand)

        for stem, cluster_members in suffix_clusters.items():
            grp = make_group(
                group_id=f"suffix_{stem}",
                category="suffix_nickname",
                category_label="Suffix / Nickname (VIP, XXX, numbers)",
                confidence=0.85,
                confidence_level="medium",
                members=sorted(cluster_members),
                reason_template="Clean performer name without noisy suffixes ({count} videos)",
            )
            if grp:
                groups.append(grp)

        # -------------------------------------------------------------------------
        # Category 4: Similarity (typos, SequenceMatcher)
        # -------------------------------------------------------------------------
        similarity_clusters: Dict[str, Set[str]] = defaultdict(set)
        clean_keys_list = [
            (_core["normalize_clean_key"](n), n) for n in all_names if len(_core["normalize_clean_key"](n)) >= 4
        ]

        # Group by first character (since matching first character is required)
        buckets: Dict[str, List[Tuple[int, str, str, List[int], List[int]]]] = defaultdict(list)
        for ck, n in clean_keys_list:
            vec = [0] * 36
            for ch in ck:
                code = ord(ch)
                if 97 <= code <= 122:
                    vec[code - 97] += 1
                elif 48 <= code <= 57:
                    vec[code - 48 + 26] += 1
            nonzero = [idx for idx, v in enumerate(vec) if v > 0]
            buckets[ck[0]].append((len(ck), ck, n, vec, nonzero))

        # Sort each bucket by string length for early termination
        for char in buckets:
            buckets[char].sort(key=lambda x: x[0])

        for first_char, items in buckets.items():
            n_items = len(items)
            for i in range(n_items):
                len1, ck1, n1, v1, nz1 = items[i]
                for j in range(i + 1, n_items):
                    len2, ck2, n2, v2, _ = items[j]
                    if len2 - len1 > 3:
                        break
                    if ck1 == ck2:
                        continue
                    pair = tuple(sorted([n1, n2]))
                    if pair in handled_pairs:
                        continue

                    total_len = len1 + len2
                    # Upper bound on maximum possible matching characters: sum(min(v1[c], v2[c]))
                    common = sum(v1[idx] if v1[idx] <= v2[idx] else v2[idx] for idx in nz1 if v2[idx])
                    if (2.0 * common) / total_len < similarity_threshold:
                        continue

                    ratio = SequenceMatcher(None, ck1, ck2).ratio()
                    if ratio >= similarity_threshold:
                        # Extra protection: if names are short (e.g. 4-5 chars), require higher threshold (0.92)
                        min_len = min(len1, len2)
                        if min_len <= 5 and ratio < 0.92:
                            continue

                        # Guard against single-letter initials being treated as typos of base name:
                        # e.g. 'Johnny B' vs 'Johnny', 'Johnny V' vs 'Johnny', 'Johnny B' vs 'Johnny V', 'Archie S' vs 'Archie'
                        w1_expanded = set(re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', n1).lower().split())
                        w2_expanded = set(re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', n2).lower().split())
                        diff_words = w1_expanded ^ w2_expanded
                        if any(len(w.rstrip('.')) == 1 for w in diff_words):
                            continue

                        # Guard against common shared words that represent different entities:
                        # e.g. 'CzechGayTwink' vs 'CzechGayTwins', 'Jordan James' vs 'Jordan Jameson'
                        words1 = set(n1.lower().split())
                        words2 = set(n2.lower().split())
                        if len(words1) == len(words2) and len(words1) >= 2:
                            diff1 = words1 - words2
                            diff2 = words2 - words1
                            if len(diff1) == 1 and len(diff2) == 1:
                                w1 = list(diff1)[0]
                                w2 = list(diff2)[0]
                                # If difference involves a completely different surname, reject
                                if SequenceMatcher(None, w1, w2).ratio() < 0.75:
                                    continue

                        cid = f"sim_{ck1}_{ck2}"
                        similarity_clusters[cid].add(n1)
                        similarity_clusters[cid].add(n2)

        for cid, cluster_members in similarity_clusters.items():
            grp = make_group(
                group_id=cid,
                category="similarity",
                category_label="Lexical similarity (typo)",
                confidence=0.88,
                confidence_level="medium",
                members=sorted(cluster_members),
                reason_template="Likely base form ({count} videos)",
            )
            if grp:
                groups.append(grp)

        # Sort report: high confidence first, then by video count
        cat_order = {"diacritics": 0, "spacing_case_punct": 1, "suffix_nickname": 2, "similarity": 3}
        groups.sort(
            key=lambda g: (
                cat_order.get(g["category"], 9),
                -g["confidence"],
                -g["canonical_video_count"],
            )
        )

        high_count = sum(1 for g in groups if g["confidence_level"] == "high")
        medium_count = sum(1 for g in groups if g["confidence_level"] == "medium")

        return {
            "ok": True,
            "stats": {
                "total_unique_performers": len(all_names),
                "total_groups": len(groups),
                "high_confidence_groups": high_count,
                "medium_confidence_groups": medium_count,
            },
            "groups": groups,
        }

    finally:
        conn.close()
