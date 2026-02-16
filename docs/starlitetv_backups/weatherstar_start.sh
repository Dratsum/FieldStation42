#!/bin/bash
# WeatherStar streaming pipeline for FieldStation42
# Runs on starlite, HLS output to NFS share for Pi access
# Video: WeatherStar webpage capture via Chromium
# Audio: SomaFM Vaporwaves stream (auto-reconnects on drop)

set -uo pipefail

URL="https://weatherstar.netbymatt.com/?latLonQuery=New+York+City,+New+York&kiosk=true&settings-speed-select=0.5&settings-scanLines-checkbox=false&hazards-checkbox=true&current-weather-checkbox=true&latest-observations-checkbox=true&hourly-checkbox=true&hourly-graph-checkbox=true&travel-checkbox=true&regional-forecast-checkbox=true&local-forecast-checkbox=true&extended-forecast-checkbox=true&almanac-checkbox=true&spc-outlook-checkbox=true&radar-checkbox=true"
AUDIO_URL="http://ice1.somafm.com/vaporwaves-128-aac"
HLS_DIR="/mnt/media-raid/MEDIA/weatherstar"
PROFILE_DIR="/tmp/chrome_weather_profile"
DISPLAY_NUM=94
XVFB_DISPLAY=":${DISPLAY_NUM}"
WIDTH=1280
HEIGHT=720
FRAMERATE=15

export DISPLAY=$XVFB_DISPLAY

cleanup() {
  echo "[CLEANUP] Stopping processes..."
  kill "${CHROMIUM_PID:-}" "${XVFB_PID:-}" 2>/dev/null || true
  rm -rf "$PROFILE_DIR" "/tmp/.X${DISPLAY_NUM}-lock"
  echo "[CLEANUP] Done."
}
trap cleanup EXIT

rm -rf "$PROFILE_DIR" "$HLS_DIR"/*.ts "$HLS_DIR"/index.m3u8
mkdir -p "$HLS_DIR"

echo "[XVFB] Launching virtual display on $XVFB_DISPLAY"
Xvfb "$XVFB_DISPLAY" -screen 0 ${WIDTH}x${HEIGHT}x24 > /tmp/xvfb_weather.log 2>&1 &
XVFB_PID=$!
sleep 2

echo "[CHROMIUM] Launching Chromium in kiosk mode..."
chromium \
  --no-sandbox \
  --disable-gpu \
  --disable-infobars \
  --disable-session-crashed-bubble \
  --no-first-run \
  --kiosk \
  --hide-scrollbars \
  --force-device-scale-factor=1 \
  --window-size=${WIDTH},${HEIGHT} \
  --user-data-dir="$PROFILE_DIR" \
  --window-position=0,0 \
  "$URL" &
CHROMIUM_PID=$!
sleep 15

# Watchdog: monitors index.m3u8 freshness, kills FFmpeg if stale (>60s)
watchdog() {
  local pid=$1
  while kill -0 "$pid" 2>/dev/null; do
    sleep 30
    if [ -f "$HLS_DIR/index.m3u8" ]; then
      local age=$(( $(date +%s) - $(stat -c %Y "$HLS_DIR/index.m3u8") ))
      if [ "$age" -gt 60 ]; then
        echo "[WATCHDOG] index.m3u8 is ${age}s stale — killing FFmpeg (PID $pid)"
        kill "$pid" 2>/dev/null
        sleep 2
        kill -9 "$pid" 2>/dev/null || true
        return
      fi
    fi
  done
}

# FFmpeg restart loop — if the SomaFM stream drops and FFmpeg exits,
# wait a few seconds and reconnect. Xvfb + Chromium stay alive.
while true; do
  echo "[FFMPEG] Starting FFmpeg with video + SomaFM Vaporwaves audio..."
  ffmpeg -y \
    -f x11grab -draw_mouse 0 -video_size ${WIDTH}x${HEIGHT} -i "${XVFB_DISPLAY}.0" \
    -rw_timeout 30000000 \
    -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 30 \
    -i "$AUDIO_URL" \
    -r $FRAMERATE \
    -c:v libx264 -preset ultrafast -tune zerolatency -b:v 1500k \
    -c:a aac -b:a 128k \
    -pix_fmt yuv420p \
    -map 0:v -map 1:a \
    -f hls \
    -hls_time 4 -hls_list_size 6 -hls_flags delete_segments+omit_endlist \
    -hls_segment_filename "$HLS_DIR/segment_%03d.ts" \
    "$HLS_DIR/index.m3u8" &
  FFMPEG_PID=$!

  watchdog "$FFMPEG_PID" &
  WATCHDOG_PID=$!

  wait "$FFMPEG_PID" || true
  kill "$WATCHDOG_PID" 2>/dev/null || true

  echo "[FFMPEG] Exited, restarting in 5 seconds..."
  sleep 5
done
