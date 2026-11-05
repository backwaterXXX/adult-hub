#!/usr/bin/env python3
"""CLI utility to audit, clean black/empty artwork, detect studio bumpers, and backfill StashDB posters."""

import argparse
import os
import sys
import time
from pathlib import Path

# Add project root to sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.image_utils import (  # noqa: E402
    compute_dhash,
    find_bumper_clusters,
    is_black_image,
    save_bumper_hashes,
)
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


def clean_nfo_thumbnail(nfo_path: Path) -> bool:
    """Remove root-level <thumb> elements from NFO file, preserving actor thumbs."""
    if not nfo_path.is_file():
        return False
    try:
        import xml.etree.ElementTree as ET
        tree = ET.parse(str(nfo_path))
        root = tree.getroot()
        changed = False
        for child in list(root):
            if child.tag == "thumb":
                root.remove(child)
                changed = True
        if changed:
            tree.write(str(nfo_path), encoding="utf-8", xml_declaration=True)
            return True
        return False
    except Exception:
        return False


def _hash_valid_files(valid_files: list[Path]) -> dict[Path, str]:
    file_hashes: dict[Path, str] = {}
    start_time = time.time()

    for idx, path in enumerate(valid_files, 1):
        if idx % 1000 == 0 or idx == len(valid_files):
            print(f"  Hashing {idx}/{len(valid_files)} images...", end="\r", flush=True)
        try:
            h = compute_dhash(path)
            file_hashes[path] = h
        except Exception:
            pass

    elapsed = time.time() - start_time
    print(f"\nHashing completed in {elapsed:.2f}s.")
    return file_hashes


def _report_top_clusters(clusters: list, purge: bool) -> tuple[int, set[str], list[Path]]:
    total_bumper_files = 0
    all_bumper_hashes: set[str] = set()
    to_purge: list[Path] = []
    for rank, cluster in enumerate(clusters[:20], 1):
        count = int(cluster["count"])
        rep_h = str(cluster["representative_hash"])
        cluster_hashes = cluster["hashes"]
        if isinstance(cluster_hashes, set):
            all_bumper_hashes.update(cluster_hashes)
            variants = len(cluster_hashes)
        else:
            variants = 1
        paths = cluster["files"]
        if isinstance(paths, list):
            total_bumper_files += count
            sample_names = ", ".join(p.stem[:30] + "..." for p in paths[:3])
            print(f"  Cluster #{rank}: dHash={rep_h} ({variants} variant{'s' if variants > 1 else ''}) | Count={count} videos | Samples: [{sample_names}]")
            if purge:
                to_purge.extend(paths)
    return total_bumper_files, all_bumper_hashes, to_purge


def _collect_cluster_stats(clusters: list, purge: bool) -> tuple[int, set[str], list[Path]]:
    total_bumper_files = 0
    all_bumper_hashes: set[str] = set()
    to_purge: list[Path] = []
    for cluster in clusters:
        count = int(cluster["count"])
        total_bumper_files += count
        cluster_hashes = cluster["hashes"]
        if isinstance(cluster_hashes, set):
            all_bumper_hashes.update(cluster_hashes)
        paths = cluster["files"]
        if purge and isinstance(paths, list):
            to_purge.extend(paths)
    return total_bumper_files, all_bumper_hashes, to_purge


def _purge_bumpers(to_purge: list[Path], execute: bool) -> None:
    print(f"\nAction: {'PURGING' if execute else 'DRY RUN - WOULD PURGE'} {len(to_purge)} bumper artwork files:")
    p_count = 0
    nfo_count = 0
    marker_count = 0
    for p in to_purge:
        if execute:
            try:
                p.unlink(missing_ok=True)
                p_count += 1
                fail_marker = p.with_suffix(".thumb_fail")
                if fail_marker.exists():
                    fail_marker.unlink(missing_ok=True)
                    marker_count += 1
                nfo_path = p.with_suffix(".nfo")
                if clean_nfo_thumbnail(nfo_path):
                    nfo_count += 1
            except Exception as e:
                print(f"  Error deleting {p.name}: {e}", file=sys.stderr)
        else:
            if p_count < 10:
                print(f"  [DRY RUN] Would purge bumper: {p.name}")
            p_count += 1
    if execute:
        print(f"Successfully purged {p_count} bumper files, cleaned {marker_count} .thumb_fail markers, updated {nfo_count} NFO files.")
    else:
        print("[DRY RUN] Run with --execute --purge-bumpers to perform actual deletion.")


def _save_bumper_cache_if_needed(cache_file, save_cache: bool, purge: bool, execute: bool,
                                 all_bumper_hashes: set[str], threshold: int, max_distance: int,
                                 clusters_count: int, total_bumper_files: int) -> None:
    should_save = cache_file and (save_cache or (purge and execute))
    if should_save:
        save_bumper_hashes(
            cache_file,
            all_bumper_hashes,
            metadata={
                "threshold": threshold,
                "max_distance": max_distance,
                "clusters_count": clusters_count,
                "total_bumper_files": total_bumper_files,
            },
        )
        print(f"Saved {len(all_bumper_hashes)} bumper hashes to {cache_file}")
    elif cache_file and (save_cache or purge):
        print(f"[DRY RUN] Would save {len(all_bumper_hashes)} bumper hashes to {cache_file}")


def detect_and_report_bumpers(
    valid_files: list[Path],
    threshold: int = 5,
    max_distance: int = 4,
    purge: bool = False,
    execute: bool = False,
    cache_file: Path | None = None,
    save_cache: bool = False,
):
    """Compute dHash on valid files and cluster repeated bumpers/logos."""
    print(f"\nDetecting repeated bumpers / studio logos (threshold >= {threshold}, max_distance <= {max_distance})...")
    file_hashes = _hash_valid_files(valid_files)

    clusters = find_bumper_clusters(
        file_hashes,
        max_distance=max_distance,
        min_count=threshold,
    )

    print(f"Found {len(clusters)} repeated bumper/logo clusters (>= {threshold} instances):")
    top_total, top_hashes, top_purge = _report_top_clusters(clusters, purge)
    rest_total, rest_hashes, rest_purge = _collect_cluster_stats(clusters[20:], purge)
    total_bumper_files = top_total + rest_total
    all_bumper_hashes = top_hashes | rest_hashes
    to_purge = top_purge + rest_purge

    print(f"Total files in bumper clusters: {total_bumper_files} (across {len(all_bumper_hashes)} distinct hashes)")

    if purge:
        _purge_bumpers(to_purge, execute)

    _save_bumper_cache_if_needed(
        cache_file, save_cache, purge, execute, all_bumper_hashes,
        threshold, max_distance, len(clusters), total_bumper_files,
    )


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
        "--max-distance",
        type=int,
        default=4,
        help="Maximum Hamming distance between perceptual hashes to cluster bumpers (default: 4).",
    )
    parser.add_argument(
        "--cache-file",
        type=Path,
        default=REPO_ROOT / "data" / "cache" / "bumper_dhashes.json",
        help="Path to JSON file storing known bumper perceptual hashes (default: data/cache/bumper_dhashes.json).",
    )
    parser.add_argument(
        "--save-cache",
        action="store_true",
        help="Save detected bumper hashes to cache file even without --purge-bumpers.",
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
            max_distance=args.max_distance,
            purge=args.purge_bumpers,
            execute=args.execute,
            cache_file=args.cache_file,
            save_cache=args.save_cache,
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
