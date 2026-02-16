# WeatherStar 4000 Channel — Setup & Configuration Report

## Overview

Channel 4 (WEATHER) is a streaming channel that displays a WeatherStar 4000 simulation (weatherstar.netbymatt.com) with SomaFM Vaporwaves background music. The stream is generated on **starlite** (192.168.0.51) and delivered to the Pi via NFS-shared HLS segments.

The Pi cannot run this itself — it requires Chromium for webpage rendering, which is too resource-intensive for the Pi 3's limited RAM (905MB total, ~154MB free with FieldStation42 running).

---

## Architecture

```
[Starlite]
  Xvfb (virtual display :94, 1280x720)
    └── Chromium (kiosk mode, renders WeatherStar webpage)
          └── FFmpeg
                ├── Input 1: x11grab (captures Xvfb display)
                ├── Input 2: SomaFM Vaporwaves AAC stream
                └── Output: HLS segments → /mnt/media-raid/MEDIA/weatherstar/
                                              ↓ (NFS share)
[StarliteTV / Pi]
  mpv reads /mnt/media/weatherstar/index.m3u8
```

---

## Components

### WeatherStar Webpage
- **URL**: `https://weatherstar.netbymatt.com/?latLonQuery=New+York+City,+New+York&kiosk=true`
- **Source**: https://github.com/netbymatt/ws4kp (WeatherStar 4000+ simulator)
- The `kiosk=true` parameter is critical — it hides the search bar, GitHub badges, playback controls, and all other UI elements, showing only the weather display
- Location is set to New York City via `latLonQuery` parameter
- The FieldStation42 codebase also supports automatic location detection via ipinfo.io (commit 4e7687e), but this channel uses an explicit location

### Audio
- **Stream**: SomaFM Vaporwaves (`http://ice1.somafm.com/vaporwaves-128-aac`)
- 128kbps AAC stream
- Vaporwave/ambient aesthetic music — fits the retro TV weather channel vibe
- Alternative stream URLs from the PLS file:
  - `http://ice2.somafm.com/vaporwaves-128-aac`
  - `http://ice4.somafm.com/vaporwaves-128-aac`
  - `http://ice6.somafm.com/vaporwaves-128-aac`

### Virtual Display (Xvfb)
- Display number: `:94`
- Resolution: 1280x720x24
- Lock file: `/tmp/.X94-lock`
- Log: `/tmp/xvfb_weather.log`

### Chromium
- Flags: `--no-sandbox --disable-gpu --disable-infobars --disable-session-crashed-bubble --no-first-run --kiosk --hide-scrollbars --force-device-scale-factor=1 --window-size=1280,720 --window-position=0,0`
- User data dir: `/tmp/chrome_weather_profile` (cleaned up on exit)
- Waits 15 seconds after launch for page to fully render before FFmpeg starts capturing

### FFmpeg Encoding
- **Video**: x11grab → libx264, ultrafast preset, zerolatency tune, 1500kbps, yuv420p
- **Audio**: SomaFM AAC stream → re-encoded AAC at 128kbps
- **Framerate**: 15fps (sufficient for weather display, saves bandwidth)
- **HLS Output**:
  - Segment duration: 4 seconds
  - Playlist size: 6 segments (rolling window)
  - Flags: `delete_segments+append_list` (auto-cleans old segments)
  - Segment naming: `segment_%03d.ts`

---

## File Locations

### On Starlite (192.168.0.51)
| Path | Description |
|------|-------------|
| `/home/dratsum/weatherstar/start.sh` | Main launch script |
| `/mnt/media-raid/MEDIA/weatherstar/` | HLS output directory |
| `/mnt/media-raid/MEDIA/weatherstar/index.m3u8` | HLS playlist |
| `/mnt/media-raid/MEDIA/weatherstar/segment_*.ts` | HLS segments |
| `/tmp/.X94-lock` | Xvfb display lock file |
| `/tmp/chrome_weather_profile/` | Chromium profile (temp) |
| `/tmp/xvfb_weather.log` | Xvfb log output |

### On StarliteTV (Pi, 192.168.0.121)
| Path | Description |
|------|-------------|
| `/home/dratsum/git/FieldStation42/confs/weather.json` | Channel configuration |
| `/mnt/media/weatherstar/index.m3u8` | HLS playlist (via NFS) |

---

## The start.sh Script

Full script at `/home/dratsum/weatherstar/start.sh`:

```bash
#!/bin/bash
# WeatherStar streaming pipeline for FieldStation42
# Runs on starlite, HLS output to NFS share for Pi access
# Video: WeatherStar webpage capture via Chromium
# Audio: SomaFM Vaporwaves stream

set -euo pipefail

URL="https://weatherstar.netbymatt.com/?latLonQuery=New+York+City,+New+York&kiosk=true"
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
  kill "${CHROMIUM_PID:-}" "${FFMPEG_PID:-}" "${XVFB_PID:-}" 2>/dev/null || true
  rm -rf "$PROFILE_DIR" "/tmp/.X${DISPLAY_NUM}-lock"
  echo "[CLEANUP] Done."
}
trap cleanup EXIT

rm -rf "$PROFILE_DIR" "$HLS_DIR"/*.ts "$HLS_DIR"/index.m3u8
mkdir -p "$HLS_DIR"

# 1. Launch virtual framebuffer
Xvfb "$XVFB_DISPLAY" -screen 0 ${WIDTH}x${HEIGHT}x24 > /tmp/xvfb_weather.log 2>&1 &
XVFB_PID=$!
sleep 2

# 2. Launch Chromium in kiosk mode
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

# 3. Start FFmpeg capturing display + audio stream → HLS
ffmpeg -y \
  -f x11grab -draw_mouse 0 -video_size ${WIDTH}x${HEIGHT} -i "${XVFB_DISPLAY}.0" \
  -i "$AUDIO_URL" \
  -r $FRAMERATE \
  -c:v libx264 -preset ultrafast -tune zerolatency -b:v 1500k \
  -c:a aac -b:a 128k \
  -pix_fmt yuv420p \
  -map 0:v -map 1:a \
  -shortest \
  -f hls \
  -hls_time 4 -hls_list_size 6 -hls_flags delete_segments+append_list \
  -hls_segment_filename "$HLS_DIR/segment_%03d.ts" \
  "$HLS_DIR/index.m3u8" &
FFMPEG_PID=$!

sleep 10
wait
```

---

## Channel Config (weather.json)

```json
{"station_conf": {
    "network_name": "WEATHER",
    "network_type": "streaming",
    "channel_number": 4,
    "network_long_name": "WeatherStar",
    "streams": [
        {"url": "/mnt/media/weatherstar/index.m3u8", "duration": 0, "title": "WeatherStar NYC"}
    ]
}}
```

Note: The URL is a local NFS path, not HTTP. The Pi reads HLS segments directly from the NFS mount. Duration 0 means continuous/indefinite playback.

---

## Starting / Stopping

### Starting the WeatherStar stream (on starlite):
```bash
ssh dratsum@Starlite
cd ~/weatherstar
nohup bash start.sh > /tmp/weatherstar.log 2>&1 &
```

### Checking if it's running:
```bash
# Check for running processes
ssh dratsum@Starlite "ps aux | grep -E 'weatherstar|Xvfb.*:94|chromium.*kiosk' | grep -v grep"

# Check HLS output is fresh (segments should be updating every few seconds)
ssh dratsum@Starlite "ls -la /mnt/media-raid/MEDIA/weatherstar/"

# Check from Pi side
ssh dratsum@192.168.0.121 "ls -la /mnt/media/weatherstar/"
```

### Stopping:
```bash
# The script has a cleanup trap, so killing the main process cleans up children
ssh dratsum@Starlite "pkill -f 'bash.*start.sh'"
# Or kill the ffmpeg process specifically:
ssh dratsum@Starlite "pkill -f 'ffmpeg.*weatherstar'"
```

---

## Dependencies on Starlite

Installed for this pipeline:
- `chromium-browser` (apt)
- `xvfb` (apt) — provides the `Xvfb` command
- `ffmpeg` (already present)

---

## Important Notes & Gotchas

### Docker iptables
Docker on starlite modifies iptables rules and blocks LAN access to arbitrary ports. This is why the initial HTTP-based approach (Python HTTP server on port 8004) failed. The NFS approach bypasses this entirely since the MEDIA directory is already exported via NFS.

### Not a systemd service
The WeatherStar pipeline is NOT currently a systemd service. It must be manually restarted after starlite reboots. Creating a systemd service is a TODO item.

Proposed service file (not yet created):
```ini
[Unit]
Description=WeatherStar HLS Stream for FieldStation42
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=dratsum
WorkingDirectory=/home/dratsum/weatherstar
ExecStart=/bin/bash /home/dratsum/weatherstar/start.sh
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

### Chromium Memory
Chromium is memory-hungry. On starlite (with 16GB+ RAM and swap), this isn't a problem. On the Pi 3 (905MB RAM), it would be impossible to run alongside FieldStation42.

### SomaFM Stream Reliability
The SomaFM stream URLs occasionally change. If audio stops:
1. Check https://somafm.com/vaporwaves/ for current stream URLs
2. Download the PLS file: `curl https://somafm.com/nossl/vaporwaves130.pls`
3. Update `AUDIO_URL` in start.sh

### HLS Segment Cleanup
The `delete_segments` flag in FFmpeg automatically removes old .ts segments. Only the most recent 6 segments are kept on disk at any time (~24 seconds of video). This prevents disk space from growing indefinitely.

### Changing the Location
To change the weather location, modify the `latLonQuery` parameter in the URL. Examples:
- NYC: `?latLonQuery=New+York+City,+New+York&kiosk=true`
- LA: `?latLonQuery=Los+Angeles,+California&kiosk=true`
- Chicago: `?latLonQuery=Chicago,+Illinois&kiosk=true`

Or use latitude/longitude directly: `?lat=40.7128&lon=-74.0060&kiosk=true`

### Tuning Video Quality
Current settings (1280x720, 15fps, 1500kbps) are a balance between quality and Pi 3 decode capability. To adjust:
- **Resolution**: Change `WIDTH` and `HEIGHT` in start.sh (and Chromium `--window-size`)
- **Framerate**: Change `FRAMERATE` (15fps is fine for weather display, 30fps for smoother transitions)
- **Bitrate**: Change `-b:v 1500k` (higher = better quality, more NFS bandwidth)
