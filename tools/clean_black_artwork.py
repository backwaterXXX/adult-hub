#!/usr/bin/env python3
"""CLI utility to audit, clean black/empty artwork, detect studio bumpers, and backfill StashDB posters."""

import argparse
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

# Add project root to sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.image_utils import (  # noqa: E402
    compute_dhash,
    is_black_image,
)  # noqa: E402
from common import db  # noqa: E402


def scan_artwork(media_dir: Path):
    """Scan all .jpg files in media_dir and identify black/empty images."""
    if not media_dir.is_dir():
        print(f"Error: Directory not found: {media_dir}", file=sys.stderr)
        return [], []

    all_jpgs = sorted(media_dir.glob("*.jpg"))
    black_files = []
    valid_files = []

    print(f"Scanning {len(all_jpgs)} artwork files in {media_dir}...")
    start_time = time.time()
    for idx, path in enumerate(all_jpgs, 1):
        if idx % 1000 == 0 or idx == len(all_jpgs):
            print(f"  Processed {idx}/{len(all_jpgs)} files...", end="\r", flush=True)

        if is_black_image(path):
            black_files.append(path)
        else:
            valid_files.append(path)

    elapsed = time.time() - start_time
    print(f"\nScan completed in {elapsed:.2f}s.")
    print(f"Total artwork files: {len(all_jpgs)}")
    print(f"Black / blank files: {len(black_files)} ({len(black_files)/max(len(all_jpgs), 1)*100:.1f}%)")
    print(f"Valid / colorful files: {len(valid_files)}")

    return black_files, valid_files


def purge_black_files(black_files: list[Path], execute: bool):
    """Delete black artwork files and clean .thumb_fail markers."""
    if not black_files:
        print("No black files to clean.")
        return

    print(f"\nAction: {'PURGING' if execute else 'DRY RUN - WOULD PURGE'} {len(black_files)} black artwork files:")
    deleted_count = 0
    marker_count = 0

    for path in black_files:
        if execute:
            try:
                path.unlink(missing_ok=True)
                deleted_count += 1
                # Also clean failure marker next to it so sync or scraper can retry
                fail_marker = path.with_suffix(".thumb_fail")
                if fail_marker.exists():
                    fail_marker.unlink(missing_ok=True)
                    marker_count += 1
            except Exception as err:
                print(f"  Error deleting {path.name}: {err}", file=sys.stderr)
        else:
            if deleted_count < 10:
                print(f"  [DRY RUN] Would delete: {path.name} ({path.stat().st_size} bytes)")
            deleted_count += 1

    if not execute and deleted_count > 10:
        print(f"  ... and {deleted_count - 10} more files.")

    if execute:
        print(f"Successfully deleted {deleted_count} black files and {marker_count} .thumb_fail markers.")
    else:
        print("[DRY RUN] Run with --execute to perform actual deletion.")


def detect_and_report_bumpers(valid_files: list[Path], threshold: int = 5, purge: bool = False, execute: bool = False):
    """Compute dHash on valid files and find repeated bumpers/logos (count >= threshold)."""
    print(f"\nDetecting repeated bumpers / studio logos (threshold >= {threshold})...")
    hashes = defaultdict(list)
    start_time = time.time()

    for idx, path in enumerate(valid_files, 1):
        if idx % 1000 == 0 or idx == len(valid_files):
            print(f"  Hashing {idx}/{len(valid_files)} images...", end="\r", flush=True)
        try:
            h = compute_dhash(path)
            hashes[h].append(path)
        except Exception:
            pass

    elapsed = time.time() - start_time
    print(f"\nHashing completed in {elapsed:.2f}s.")

    # Filter groups with >= threshold occurrences
    bumper_groups = {h: paths for h, paths in hashes.items() if len(paths) >= threshold}
    sorted_groups = sorted(bumper_groups.items(), key=lambda kv: len(kv[1]), reverse=True)

    print(f"Found {len(sorted_groups)} repeated bumper/logo groups (>= {threshold} instances):")
    total_bumper_files = 0
    to_purge = []

    for rank, (h, paths) in enumerate(sorted_groups[:20], 1):
        count = len(paths)
        total_bumper_files += count
        sample_names = ", ".join(p.stem[:30] + "..." for p in paths[:3])
        print(f"  Group #{rank}: dHash={h} | Count={count} videos | Samples: [{sample_names}]")
        if purge:
            to_purge.extend(paths)

    for _, paths in sorted_groups[20:]:
        total_bumper_files += len(paths)
        if purge:
            to_purge.extend(paths)

    print(f"Total files in bumper groups: {total_bumper_files}")

    if purge:
        print(f"\nAction: {'PURGING' if execute else 'DRY RUN - WOULD PURGE'} {len(to_purge)} bumper artwork files:")
        p_count = 0
        for p in to_purge:
            if execute:
                try:
                    p.unlink(missing_ok=True)
                    p_count += 1
                except Exception as e:
                    print(f"  Error deleting {p.name}: {e}", file=sys.stderr)
            else:
                if p_count < 10:
                    print(f"  [DRY RUN] Would purge bumper: {p.name}")
                p_count += 1
        if execute:
            print(f"Successfully purged {p_count} bumper files.")
        else:
            print("[DRY RUN] Run with --execute --purge-bumpers to perform actual deletion.")


def backfill_stashdb_oshash(db_url: str, media_dir: Path, limit: int = 200):
    """Backfill missing artwork using exact OSHASH matches against StashDB."""
    print(f"\nStarting StashDB OSHASH backfill (batch limit: {limit})...")
    conn = db.connect(db_url)
    try:
        try:
            from sync.scene_scraper_fingerprint_backfill import backfill_telegram_fingerprints
        except ImportError:
            from scene_scraper_fingerprint_backfill import backfill_telegram_fingerprints
        checked, enriched = backfill_telegram_fingerprints(
            conn, media_dir, limit=limit, sleep_sec=0.2
        )
        print(f"StashDB backfill completed: checked={checked}, enriched={enriched}")
    except Exception as exc:
        print(f"Error during StashDB backfill: {exc}", file=sys.stderr)
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Audit and clean black/empty artwork and bumper logos.")
    parser.add_argument(
        "--media-dir",
        type=Path,
        default=Path(os.environ.get("MEDIA_DIR", "/media/adult")),
        help="Path to media directory containing .jpg and .strm files",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Execute destructive actions (file deletions). Default is dry-run.",
    )
    parser.add_argument(
        "--detect-bumpers",
        action="store_true",
        help="Detect repeated studio bumpers / logos via dHash (count >= threshold).",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=5,
        help="Occurrence threshold to classify an image as repeated bumper/logo (default: 5).",
    )
    parser.add_argument(
        "--purge-bumpers",
        action="store_true",
        help="Purge repeated bumper files (requires --detect-bumpers).",
    )
    parser.add_argument(
        "--backfill-stashdb-oshash",
        action="store_true",
        help="Match videos without artwork to StashDB scenes using OSHASH and fetch official posters.",
    )
    parser.add_argument(
        "--batch-limit",
        type=int,
        default=200,
        help="Maximum number of videos to backfill with StashDB per run (default: 200).",
    )

    args = parser.parse_args()

    # Step 1: Scan for black artwork
    black_files, valid_files = scan_artwork(args.media_dir)

    # Step 2: Purge or dry-run black artwork
    purge_black_files(black_files, execute=args.execute)

    # Step 3: Detect bumpers if requested
    if args.detect_bumpers:
        detect_and_report_bumpers(
            valid_files,
            threshold=args.threshold,
            purge=args.purge_bumpers,
            execute=args.execute,
        )

    # Step 4: Backfill StashDB OSHASH if requested
    if args.backfill_stashdb_oshash:
        db_url = os.environ.get("DATABASE_URL")
        if not db_url:
            print("Error: DATABASE_URL environment variable is required for StashDB backfill.", file=sys.stderr)
        else:
            backfill_stashdb_oshash(db_url, args.media_dir, limit=args.batch_limit)


if __name__ == "__main__":
    main()
