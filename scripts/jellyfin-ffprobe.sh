#!/usr/bin/env bash
# Keep probing aligned with the bundled Jellyfin FFmpeg and its remote-stream
# safeguards. Jellyfin derives this executable from the configured FFmpeg path.
REAL_FFPROBE="/usr/lib/jellyfin-ffmpeg/ffprobe"

FAST_PROBE="${JELLYFIN_FFMPEG_FAST_PROBE:-true}"
ANALYZE_DUR="${JELLYFIN_FFMPEG_ANALYZE_DURATION:-5M}"
PROBE_SZ="${JELLYFIN_FFMPEG_PROBE_SIZE:-10M}"

if [ "$FAST_PROBE" != "true" ] && [ "$FAST_PROBE" != "1" ]; then
    exec "$REAL_FFPROBE" "$@"
fi

args=()
while [ $# -gt 0 ]; do
    case "$1" in
        -analyzeduration)
            args+=("-analyzeduration")
            if [ "${2:-}" = "200M" ]; then
                args+=("$ANALYZE_DUR")
            else
                args+=("$2")
            fi
            shift 2
            ;;
        -probesize)
            args+=("-probesize")
            if [ "${2:-}" = "1G" ]; then
                args+=("$PROBE_SZ")
            else
                args+=("$2")
            fi
            shift 2
            ;;
        *)
            args+=("$1")
            shift
            ;;
    esac
done

exec "$REAL_FFPROBE" "${args[@]}"
