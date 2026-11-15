"""Subprocess entry point for pHash / thumbnail generation.

Spawned by ``resolver.phash_worker._compute``; communicates over stdin/stdout
with JSON so the main worker process stays free for streaming.  Running as a
separate process isolates native library crashes from the resolver.
"""

from __future__ import annotations

import base64
import json
import sys

from common.telegram_transfer_budget import (
    DailyTelegramTransferBudgetExceeded,
    DailyTelegramTransferBudgetUnavailable,
)
from common.telegram_playback_activity import has_active_playback
from sync.video_fingerprints import FingerprintBudgetExceeded, telegram_phash


def main() -> None:
    payload = json.load(sys.stdin)

    def phash_transfer_rate():
        return (payload["playback_bytes_per_second"] if has_active_playback()
                else payload["idle_bytes_per_second"])

    try:
        kind = payload["job_kind"]
        output = telegram_phash(
            payload["video_id"], payload["duration_us"] / 1000000,
            byte_budget=payload["byte_budget"], timeout_seconds=payload["timeout_seconds"],
            resolver_url="http://127.0.0.1:8090/admin",
            max_bytes_per_second=phash_transfer_rate,
            allow_rate_above_configured=True,
            cache_dir=payload["cache_dir"], cache_id=payload["cache_id"],
            include_thumbnail=(kind != "phash"), thumbnail_only=(kind == "thumbnail"))
        fingerprint, bytes_read = output[:2]
        thumbnail = output[2] if kind != "phash" else None
        if kind != "phash" and not thumbnail:
            raise ValueError("thumbnail_missing")
        print(json.dumps({
            "ok": True, "fingerprint": fingerprint, "bytes_read": bytes_read,
            "thumbnail": base64.b64encode(thumbnail).decode("ascii") if thumbnail else None,
        }))
    except DailyTelegramTransferBudgetExceeded:
        print(json.dumps({"ok": False, "error_code": "daily_transfer_budget_exhausted", "retryable": False}))
    except DailyTelegramTransferBudgetUnavailable:
        print(json.dumps({"ok": False, "error_code": "transfer_budget_unavailable", "retryable": False}))
    except FingerprintBudgetExceeded:
        print(json.dumps({"ok": False, "error_code": "byte_budget_exhausted", "retryable": False}))
    except TimeoutError:
        print(json.dumps({"ok": False, "error_code": "timeout", "retryable": True}))
    except OSError:
        print(json.dumps({"ok": False, "error_code": "source_unavailable", "retryable": True}))
    except ValueError:
        print(json.dumps({"ok": False, "error_code": "invalid_video", "retryable": False}))
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        print(json.dumps({
            "ok": False, "error_code": "generation_failed", "retryable": True,
            "error_detail": detail[:2000],
        }))


if __name__ == "__main__":
    main()
