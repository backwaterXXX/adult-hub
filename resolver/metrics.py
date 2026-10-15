"""Bounded-cardinality HTTP and playback metrics for Prometheus scraping."""

from __future__ import annotations

import re
import threading
from collections import defaultdict


_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
_LABEL_VALUE = re.compile(r"[^A-Za-z0-9_./{}:-]")


def _label(value: str) -> str:
    safe = _LABEL_VALUE.sub("_", str(value))[:160]
    return safe.replace("\\", "\\\\").replace('"', '\\"')


class ResolverMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: dict[tuple[str, str, int], int] = defaultdict(int)
        self._durations: dict[tuple[str, str], dict] = {}
        self._streams = {"count": 0, "sum": 0.0, "buckets": [0] * len(_BUCKETS)}

    def record_request(self, method: str, route: str, status: int, duration: float,
                       *, stream: bool = False) -> None:
        key = (_label(method), _label(route), int(status))
        with self._lock:
            self._requests[key] += 1
            histogram = self._durations.setdefault(
                key[:2], {"count": 0, "sum": 0.0, "buckets": [0] * len(_BUCKETS)}
            )
            histogram["count"] += 1
            histogram["sum"] += duration
            for index, bound in enumerate(_BUCKETS):
                if duration <= bound:
                    histogram["buckets"][index] += 1
            if stream:
                self._streams["count"] += 1
                self._streams["sum"] += duration
                for index, bound in enumerate(_BUCKETS):
                    if duration <= bound:
                        self._streams["buckets"][index] += 1

    def render(self) -> str:
        with self._lock:
            requests = dict(self._requests)
            durations = {key: {**value, "buckets": list(value["buckets"])}
                         for key, value in self._durations.items()}
            streams = {**self._streams, "buckets": list(self._streams["buckets"])}

        lines = [
            "# HELP adult_hub_http_requests_total Completed HTTP requests.",
            "# TYPE adult_hub_http_requests_total counter",
        ]
        for (method, route, status), count in sorted(requests.items()):
            lines.append(
                f'adult_hub_http_requests_total{{method="{method}",route="{route}",status="{status}"}} {count}'
            )
        lines.extend([
            "# HELP adult_hub_http_request_duration_seconds Full response duration, including streamed bodies.",
            "# TYPE adult_hub_http_request_duration_seconds histogram",
        ])
        for (method, route), histogram in sorted(durations.items()):
            labels = f'method="{method}",route="{route}"'
            for index, bound in enumerate(_BUCKETS):
                lines.append(
                    f'adult_hub_http_request_duration_seconds_bucket{{{labels},le="{bound:g}"}} '
                    f'{histogram["buckets"][index]}'
                )
            lines.append(f'adult_hub_http_request_duration_seconds_bucket{{{labels},le="+Inf"}} {histogram["count"]}')
            lines.append(f'adult_hub_http_request_duration_seconds_sum{{{labels}}} {histogram["sum"]:.6f}')
            lines.append(f'adult_hub_http_request_duration_seconds_count{{{labels}}} {histogram["count"]}')
        lines.extend([
            "# HELP adult_hub_stream_request_duration_seconds Full media stream response duration.",
            "# TYPE adult_hub_stream_request_duration_seconds histogram",
        ])
        for index, bound in enumerate(_BUCKETS):
            lines.append(
                f'adult_hub_stream_request_duration_seconds_bucket{{le="{bound:g}"}} '
                f'{streams["buckets"][index]}'
            )
        lines.append(
            f'adult_hub_stream_request_duration_seconds_bucket{{le="+Inf"}} {streams["count"]}'
        )
        lines.append(f'adult_hub_stream_request_duration_seconds_sum {streams["sum"]:.6f}')
        lines.append(f'adult_hub_stream_request_duration_seconds_count {streams["count"]}')
        return "\n".join(lines) + "\n"


resolver_metrics = ResolverMetrics()
