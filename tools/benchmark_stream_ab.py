#!/usr/bin/env python3
"""A/B streaming benchmark: simulated HTTP playback vs resolver (Telegram path).

Simulated playback (NOT a real player): a producer thread drains the HTTP
stream as fast as the server/socket allows (bounded by a simulated client
buffer), while the main thread consumes bytes at the file's average byte
rate. Startup delay, simulated buffer stalls and throughput are derived
from this model. Never label these as measured real-player stalls.

Stdlib only. No credentials are printed.

Modes (env knobs applied via `docker compose up` when --allow-mutate):
  sequential : TELEGRAM_PARALLEL_STREAM_ENABLED=false
  parallel1/2/4 : enabled=true, TELEGRAM_DOWNLOAD_WORKERS=N
Common fixed knobs for all modes: GLOBAL_WORKERS=6, BLOCK=524288,
READ_AHEAD=33554432, CACHE_BYTES=134217728, CACHE_TTL=120.

Cold cache per mode is ensured by recreating the resolver container
(`docker compose up -d --no-deps --force-recreate resolver`), NOT by this
script unless --allow-mutate is passed.

Usage (pure measurement, orchestrator handles docker):
  python3 tools/benchmark_stream_ab.py --mode sequential --window 120 \
      --video-id 'telegram:-100..:5680:...' --total 877247629 --duration 815 \
      --out /tmp/adult-hub-benchmark-mode-sequential.json

Full run with container mutation:
  ... --allow-mutate  (runs docker compose recreate + health wait + diagnostics enable)
  set ADMIN_WRITE_TOKEN when the resolver requires an Admin write token.

Aggregate:
  python3 tools/benchmark_stream_ab.py --readonly-report \
      --inputs a.json b.json --out /tmp/adult-hub-benchmark-results.json
"""
import argparse
import hashlib
import http.client
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

CHUNK = 65536
SIM_CAP_BYTES = 32 * 1024 * 1024       # simulated client buffer cap
START_THRESHOLD = 2 * 1024 * 1024      # start playback after 2 MiB buffered
RESUME_THRESHOLD = 1 * 1024 * 1024    # resume after stall at 1 MiB
CONNECT_TIMEOUT = 15.0
READ_TIMEOUT = 30.0
SEEK_LEN = 2 * 1024 * 1024
TICK = 0.05

BASE_URL = "http://127.0.0.1:8090"

MODES = {
    "sequential": {"TELEGRAM_PARALLEL_STREAM_ENABLED": "false",
                   "TELEGRAM_DOWNLOAD_WORKERS": "3"},
    "parallel1": {"TELEGRAM_PARALLEL_STREAM_ENABLED": "true",
                  "TELEGRAM_DOWNLOAD_WORKERS": "1"},
    "parallel2": {"TELEGRAM_PARALLEL_STREAM_ENABLED": "true",
                  "TELEGRAM_DOWNLOAD_WORKERS": "2"},
    "parallel4": {"TELEGRAM_PARALLEL_STREAM_ENABLED": "true",
                  "TELEGRAM_DOWNLOAD_WORKERS": "4"},
}
COMMON_KNOBS = {
    "TELEGRAM_DOWNLOAD_GLOBAL_WORKERS": "6",
    "TELEGRAM_DOWNLOAD_BLOCK_BYTES": "524288",
    "TELEGRAM_READ_AHEAD_BYTES": "33554432",
    "TELEGRAM_STREAM_CACHE_BYTES": "134217728",
    "TELEGRAM_STREAM_CACHE_TTL_SECONDS": "120",
}


def mode_env(mode):
    env = dict(COMMON_KNOBS)
    env.update(MODES[mode])
    return env


def wait_health(base_url, deadline_s=120.0):
    t0 = time.monotonic()
    last = None
    while time.monotonic() - t0 < deadline_s:
        try:
            with urllib.request.urlopen(base_url + "/admin/health",
                                        timeout=10) as r:
                if r.status == 200:
                    return True, time.monotonic() - t0
        except Exception as e:  # noqa: BLE001
            last = repr(e)[:120]
        time.sleep(2.0)
    return False, last


def diag_api(method, path, timeout=15.0):
    headers = {}
    token = os.environ.get("ADMIN_WRITE_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE_URL + path, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        return {"_error": type(e).__name__}


def recreate_resolver(mode, workdir=".", deadline_s=180.0):
    """Recreate resolver container with inline env only. Returns (ok, info)."""
    env = mode_env(mode)
    cmd = ["docker", "compose", "up", "-d", "--no-deps",
           "--force-recreate", "resolver"]
    import os
    merged = dict(os.environ)
    merged.update(env)
    # Ensure unrelated secret env from host does not leak into output.
    try:
        subprocess.run(cmd, cwd=workdir, env=merged, timeout=170,
                       capture_output=True, text=True, check=False)
    except Exception as e:  # noqa: BLE001
        return False, {"error": repr(e)[:200]}
    ok, info = wait_health(BASE_URL, deadline_s=deadline_s)
    return ok, {"health_wait_s": info, "applied": sorted(env.keys())}


def measure_stream(video_id, total, avg_rate, window):
    """Run the simulated playback. Returns metrics dict.

    Producer thread reads quickly (64 KiB chunks) with backpressure at
    SIM_CAP_BYTES; main thread consumes at avg_rate. Transparent and bounded.
    """
    url_path = "/admin/play/%s/stream?mode=proxy" % urllib.parse.quote(
        video_id, safe=":")  # canonical local path (matches .strm /admin/play)
    parsed = urllib.parse.urlparse(BASE_URL)
    host, port = parsed.hostname, parsed.port or 80

    lock = threading.Lock()
    # Single shared locked buffer byte count: producer adds, consumer removes.
    # This enforces SIM_CAP_BYTES on total occupancy (bounded by cap + 1 chunk).
    state = {"received": 0, "buffered": 0, "stop": False, "error": None,
             "status": None, "content_range": None, "content_length": None,
             "first_body_t": None, "last_read_t": None,
             "max_gap_s": 0.0, "per_sec": {}, "done": False,
             "max_occupancy": 0}
    holder = {"conn": None, "resp": None}
    t0 = time.monotonic()

    def producer():
        conn = None
        resp = None
        try:
            conn = http.client.HTTPConnection(host, port,
                                              timeout=CONNECT_TIMEOUT)
            holder["conn"] = conn
            conn.request("GET", url_path,
                         headers={"Range": "bytes=0-", "User-Agent": "ab-bench/1.0"})
            resp = conn.getresponse()
            holder["resp"] = resp
            with lock:
                state["status"] = resp.status
                try:
                    state["content_range"] = resp.getheader("Content-Range")
                    state["content_length"] = resp.getheader("Content-Length")
                except Exception:  # noqa: BLE001
                    pass
            # Real socket timeout (conn.sock_timeout is a no-op attribute).
            try:
                if conn.sock is not None:
                    conn.sock.settimeout(READ_TIMEOUT)
            except Exception:  # noqa: BLE001
                pass
            prev = time.monotonic()
            while True:
                if time.monotonic() - t0 >= window:
                    break
                with lock:
                    if state["stop"]:
                        break
                    # Backpressure on shared occupancy (producer adds,
                    # consumer removes). Kernel/socket buffers may still
                    # absorb data underneath (see limitations).
                    if state["buffered"] >= SIM_CAP_BYTES:
                        pause = True
                    else:
                        pause = False
                if pause:
                    time.sleep(0.02)
                    continue
                try:
                    chunk = resp.read(CHUNK)
                except Exception as e:  # noqa: BLE001
                    with lock:
                        if state["error"] is None:
                            state["error"] = "read:%s" % type(e).__name__
                    break
                now = time.monotonic()
                if not chunk:
                    break  # clean EOF
                with lock:
                    # First body chunk timestamp (headers arrival is NOT it).
                    if state["first_body_t"] is None:
                        state["first_body_t"] = now - t0
                    state["received"] += len(chunk)
                    state["buffered"] += len(chunk)
                    if state["buffered"] > state["max_occupancy"]:
                        state["max_occupancy"] = state["buffered"]
                    sec = int(now - t0)
                    state["per_sec"][sec] = state["per_sec"].get(sec, 0) + len(chunk)
                    state["last_read_t"] = now - t0
                    gap = now - prev
                    if gap > state["max_gap_s"]:
                        state["max_gap_s"] = gap
                prev = now
        except Exception as e:  # noqa: BLE001
            with lock:
                if state["error"] is None:
                    state["error"] = "%s:%s" % (type(e).__name__, str(e)[:120])
        finally:
            with lock:
                state["done"] = True
            try:
                if resp is not None:
                    resp.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                if conn is not None:
                    conn.close()
            except Exception:  # noqa: BLE001
                pass

    th = threading.Thread(target=producer, daemon=True)
    th.start()

    # Consumer simulation (main thread, never blocks on network).
    # Simulation clock starts at t0 but no media consumption occurs before
    # START_THRESHOLD is reached; while stalled only refill is observed
    # (no consumption) until RESUME_THRESHOLD, then playback resumes.
    started = False
    startup_ms = None
    stalled = False
    stall_count = 0
    stall_total_s = 0.0
    stall_begin = None
    min_buffered = None
    consumed_total = 0.0
    last = time.monotonic()
    deadline = t0 + window
    while time.monotonic() < deadline:
        time.sleep(TICK)
        now = time.monotonic()
        dt = now - last
        last = now
        with lock:
            buffered = state["buffered"]
            if started and not stalled:
                use = min(buffered, avg_rate * dt)
                state["buffered"] -= use
                cur_buffered = state["buffered"]
            else:
                use = 0.0
                cur_buffered = buffered
        if not started:
            if cur_buffered >= START_THRESHOLD:
                started = True
                startup_ms = (now - t0) * 1000.0
                if min_buffered is None:
                    min_buffered = cur_buffered
            continue  # no consumption during initial fill wait
        if stalled:
            # Refill only; do NOT consume media while stalled.
            if cur_buffered >= RESUME_THRESHOLD:
                stalled = False
                stall_total_s += now - stall_begin
                stall_begin = None
            continue
        # Started and not stalled: consume at average byte rate.
        consumed_total += use
        if min_buffered is None or cur_buffered < min_buffered:
            min_buffered = cur_buffered
        if cur_buffered <= 0:
            stalled = True
            stall_count += 1
            stall_begin = now
    if stalled and stall_begin is not None:
        stall_total_s += time.monotonic() - stall_begin
    with lock:
        state["stop"] = True
    # Close ongoing connection to unblock a pending resp.read() boundedly.
    try:
        if holder.get("resp") is not None:
            holder["resp"].close()
    except Exception:  # noqa: BLE001
        pass
    try:
        if holder.get("conn") is not None:
            holder["conn"].close()
    except Exception:  # noqa: BLE001
        pass
    th.join(timeout=20.0)
    producer_alive = th.is_alive()
    wall = time.monotonic() - t0

    received = state["received"]
    max_occ = state["max_occupancy"]
    cap_ok = max_occ <= SIM_CAP_BYTES + CHUNK
    # Delivered HTTP rate over total wall time (NOT Telegram raw transfer),
    # plus initial-fill 5 s sample. No bytes/last_read unpaced claim.
    delivered_mbps = received * 8.0 / max(wall, 1e-6) / 1e6
    first5 = sum(v for k, v in state["per_sec"].items() if k < 5)
    initial_fill_mbps = first5 * 8.0 / 5.0 / 1e6
    return {
        "wall_s": round(wall, 2),
        "http_status": state["status"],
        "content_range_ok": bool(state["content_range"] and
                                 str(total) in str(state["content_range"])),
        "error": state["error"],
        "producer_dangling": producer_alive,
        "bytes_received": received,
        "startup_ms": round(startup_ms, 1) if startup_ms else None,
        "started": started,
        "sim_stall_count": stall_count,
        "sim_stall_total_s": round(stall_total_s, 2),
        "sim_min_buffered_s": round((min_buffered or 0) / avg_rate, 2),
        "sim_consumed_bytes": int(consumed_total),
        "delivered_http_mbps": round(delivered_mbps, 3),
        "paced_throughput_mbps": round(delivered_mbps, 3),
        "raw_throughput_mbps_deprecated": round(delivered_mbps, 3),  # DEPRECATED: was alias for paced HTTP rate
        "initial_fill_5s_mbps": round(initial_fill_mbps, 3),
        "max_read_gap_s": round(state["max_gap_s"], 3),
        "per_sec_bytes": {str(k): v
                          for k, v in sorted(state["per_sec"].items())},
        "first_byte_s": (round(state["first_body_t"], 3)
                         if state["first_body_t"] else None),
        "last_read_s": state["last_read_t"],
        "producer_first_byte_s": (round(state["first_body_t"], 3)
                                  if state["first_body_t"] else None),
        "sim_cap_bytes": SIM_CAP_BYTES,
        "sim_max_occupancy_bytes": max_occ,
        "sim_cap_ok": cap_ok,
    }


def measure_seek(video_id, offset, length=SEEK_LEN, timeout=60.0):
    url_path = "/admin/play/%s/stream?mode=proxy" % urllib.parse.quote(
        video_id, safe=":")
    parsed = urllib.parse.urlparse(BASE_URL)
    host, port = parsed.hostname, parsed.port or 80
    rng = "bytes=%d-%d" % (offset, offset + length - 1)
    t0 = time.monotonic()
    first = None
    got = 0
    status = None
    err = None
    conn = None
    expected_len = length
    try:
        conn = http.client.HTTPConnection(host, port, timeout=CONNECT_TIMEOUT)
        conn.request("GET", url_path,
                     headers={"Range": rng, "User-Agent": "ab-bench/1.0"})
        resp = conn.getresponse()
        status = resp.status
        try:
            cl = resp.getheader("Content-Length")
            if cl is not None:
                expected_len = int(cl)
        except Exception:  # noqa: BLE001
            expected_len = length
        # Real socket timeout for bounded first-byte/completion timing.
        try:
            if conn.sock is not None:
                conn.sock.settimeout(timeout)
        except Exception:  # noqa: BLE001
            pass
        while True:
            if time.monotonic() - t0 > timeout:
                err = "timeout"
                break
            chunk = resp.read(CHUNK)
            if first is None:
                first = time.monotonic() - t0
            if not chunk:
                break
            got += len(chunk)
            if got >= length:
                # drain not needed; finite range: stop after expected length
                break
        resp.close()
        if err is None:
            if status not in (200, 206):
                err = "http_status:%s" % status
            elif got != expected_len:
                err = "short_body:%d!=%d" % (got, expected_len)
    except Exception as e:  # noqa: BLE001
        err = "%s:%s" % (type(e).__name__, str(e)[:120])
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:  # noqa: BLE001
            pass
    total_t = time.monotonic() - t0
    return {"offset": offset, "length": length, "http_status": status,
            "first_byte_s": round(first, 3) if first else None,
            "total_s": round(total_t, 3), "bytes": got, "error": err}


def cmd_measure(args):
    avg_rate = args.total / args.duration  # bytes per second (float)
    res = measure_stream(args.video_id, args.total, avg_rate, args.window)
    mid = args.total // 2
    off2 = 788365312 if 788365312 < args.total else int(args.total * 0.9)
    time.sleep(3.0)  # let diagnostics settle
    seeks = [measure_seek(args.video_id, mid),
             measure_seek(args.video_id, off2)]
    out = {
        "tool": "benchmark_stream_ab/1.0",
        "mode": args.mode,
        "simulated": True,
        "note": ("HTTP simulation with paced consumer; simulated stalls are "
                 "NOT real-player stalls."),
        "video_id_hash": hashlib.sha256(
            args.video_id.encode()).hexdigest()[:16],
        "total_bytes": args.total,
        "duration_s": args.duration,
        "avg_rate_bps": avg_rate,
        "avg_rate_mbps": round(avg_rate * 8 / 1e6, 3),
        "window_s": args.window,
        "env": mode_env(args.mode),
        "main": res,
        "seeks": seeks,
        "seek_offsets": [mid, off2],
        "seek_cache_note": ("seek samples may hit resolver cache warmed by "
                            "the main run; flagged, not cold"),
        "limitations": [
            "shared 32MiB sim buffer cap enforced (producer adds, consumer "
            "removes); kernel/socket buffers still absorb data underneath",
            "no media consumption before start threshold or while stalled; "
            "refill-only until resume threshold",
            "consumer ticks every 50ms; timing approximate",
            "delivered_http_mbps is total-wall delivered HTTP rate (NOT "
            "Telegram raw transfer); use initial_fill_5s_mbps for "
            "unpaced early-fill sample",
            "single run per mode; no statistical significance claimed",
        ],
    }
    with open(args.out, "w") as f:
        json.dump(out, f, indent=1)
    print(json.dumps({"mode": args.mode, "out": args.out,
                      "stalls": res["sim_stall_count"],
                      "stall_s": res["sim_stall_total_s"],
                      "startup_ms": res["startup_ms"],
                      "delivered_mbps": res["delivered_http_mbps"],
                      "cap_ok": res.get("sim_cap_ok"),
                      "error": res["error"]}))
    return 0


def cmd_report(args):
    mods = []
    for p in args.inputs:
        with open(p) as f:
            mods.append(json.load(f))
    mods.sort(key=lambda m: list(MODES).index(m.get("mode", "")))
    table = []
    for m in mods:
        main = m.get("main", {})
        table.append({
            "mode": m.get("mode"),
            "startup_ms": main.get("startup_ms"),
            "sim_stalls": main.get("sim_stall_count"),
            "sim_stall_s": main.get("sim_stall_total_s"),
            "min_buf_s": main.get("sim_min_buffered_s"),
            "delivered_mbps": main.get("delivered_http_mbps",
                                            main.get("paced_throughput_mbps")),
            "paced_mbps": main.get("paced_throughput_mbps"),
            "fill5_mbps": main.get("initial_fill_5s_mbps"),
            "max_gap_s": main.get("max_read_gap_s"),
            "cap_ok": main.get("sim_cap_ok"),
            "bytes": main.get("bytes_received"),
            "seeks": [(s.get("offset"), s.get("first_byte_s"),
                       s.get("total_s"), s.get("http_status"))
                      for s in m.get("seeks", [])],
            "error": main.get("error"),
        })
    res = {"simulated": True, "modes": table, "detail_files": args.inputs}
    with open(args.out, "w") as f:
        json.dump(res, f, indent=1)
    print(json.dumps(table, indent=1))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=list(MODES) + ["all"],
                    default="sequential")
    ap.add_argument("--window", type=float, default=120.0)
    ap.add_argument("--video-id", default="")
    ap.add_argument("--total", type=int, default=0)
    ap.add_argument("--duration", type=float, default=815.0)
    ap.add_argument("--out", default="/tmp/adult-hub-benchmark-mode.json")
    ap.add_argument("--allow-mutate", action="store_true",
                    help="allow docker compose recreate + diagnostics enable")
    ap.add_argument("--readonly-report", action="store_true")
    ap.add_argument("--inputs", nargs="*", default=[])
    a = ap.parse_args(argv)
    if a.readonly_report:
        return cmd_report(a)
    if a.mode == "all" and not a.allow_mutate:
        print("refusing multi-mode run without --allow-mutate", file=sys.stderr)
        return 2
    if a.mode == "all" and a.allow_mutate:
        print("multi-mode mutate runs must be orchestrated externally; "
              "call per-mode with --allow-mutate", file=sys.stderr)
        return 2
    if not a.video_id or not a.total:
        print("--video-id and --total required", file=sys.stderr)
        return 2
    if a.allow_mutate:
        ok, info = recreate_resolver(a.mode)
        print(json.dumps({"recreated": ok, "info": info}))
        if not ok:
            return 1
        st = diag_api("GET", "/admin/api/v2/diagnostics/playback/status")
        if st.get("ok") is not True:
            print("playback diagnostics status unavailable; aborting",
                  file=sys.stderr)
            return 1
        active_count = st.get("active_count")
        if type(active_count) is not int or active_count < 0:
            print("playback diagnostics active count is invalid; aborting",
                  file=sys.stderr)
            return 1
        print(json.dumps({"diag_before": {"enabled": st.get("enabled"),
                                           "active_count": active_count}}))
        if active_count != 0:
            print("active diagnostics requests present; aborting",
                  file=sys.stderr)
            return 1
        enabled = diag_api("POST", "/admin/api/v2/diagnostics/playback/enable")
        if enabled.get("ok") is not True:
            print("could not enable playback diagnostics; aborting",
                  file=sys.stderr)
            return 1
        print(json.dumps({"diag_enable": {key: enabled.get(key) for key in
                                           ("enabled", "enabled_until", "active_count")}}))
    return cmd_measure(a)


if __name__ == "__main__":
    sys.exit(main())
