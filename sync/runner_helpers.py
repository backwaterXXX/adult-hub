"""Pure helpers used by the sync runner: sleep math, duration text, child args and wait timers.

None of these read module state, so they live outside runner.py.
runner.py re-exports every name, so existing ``runner.<name>`` references keep working.
"""


def calculate_next_sleep_seconds(daily_at_time, default_interval: int) -> float:
    if not daily_at_time:
        return float(default_interval)
    import datetime
    parts = daily_at_time.split(":")
    target_h, target_m = int(parts[0]), int(parts[1])
    now = datetime.datetime.now()
    target = now.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


def format_duration(seconds: float) -> str:
    total_sec = int(round(seconds))
    hours = total_sec // 3600
    minutes = (total_sec % 3600) // 60
    secs = total_sec % 60
    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0 or hours > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


class _WaitTimers:
    """Next-due times for the periodic checks inside wait_with_trigger."""

    def __init__(self):
        self.next_worker_attempt = 0.0
        self.next_queue_error_log = 0.0
        self.next_schedule_dispatch = 0.0
        self.next_schedule_error_log = 0.0
        self.next_system_schedule_check = 0.0
        self.next_system_schedule_error_log = 0.0


def _sync_child_args(source_filter, sync_mode):
    source_arg = ["--source", source_filter] if source_filter else []
    # Preserve the legacy child invocation for the default mode; only explicit
    # administrative modes need an additional CLI argument.
    mode_arg = ["--sync-mode", sync_mode] if sync_mode != "scheduled" else []
    return source_arg, mode_arg
