# FieldStation42 - Claude Context

## Project Overview
FieldStation42 is a cable/broadcast TV simulator that recreates the authentic experience of watching OTA (over-the-air) television. When you turn on the TV, a believable show for the time slot and network is playing. When switching channels, shows continue as if they had been broadcasting the whole time.

**Repo:** https://github.com/shane-mason/FieldStation42

## Network Architecture

```
starlite (192.168.0.51)                    starlitetv (192.168.0.121)
Linux Mint 20.3, GTX 1660 SUPER           Raspberry Pi 3 (Debian trixie/aarch64)
┌──────────────────────────────┐           ┌──────────────────────────────┐
│ Media Server + Pipelines     │   NFS     │ FieldStation42 Player        │
│ /mnt/media-raid/MEDIA/       ├──────────►│ /mnt/media/                  │
│                              │           │                              │
│ Runs:                        │           │ Runs:                        │
│  - Docker (HA, Plex, Nginx)  │           │  - fieldstation.service      │
│  - WeatherStar pipeline      │           │  - fieldstation-web.service  │
│  - VJ/DJ pipeline            │           │  - HDMI out to TV            │
└──────────────────────────────┘           └──────────────────────────────┘
```

### Key Facts
- **All media lives on starlite** — the Pi is just a player
- SSH to Pi: `ssh dratsum@192.168.0.121` (hostname `starlitetv` not reliably in DNS)
- SSH to starlite: `ssh dratsum@Starlite`
- FieldStation42 code on Pi: `/home/dratsum/git/FieldStation42/`
- Local workspace (this machine): `/home/dratsum/git/Fieldstation42/`
- Pi venv: `/home/dratsum/git/FieldStation42/venv` (must use `venv/bin/python3`)
- Media mount on Pi: `/mnt/media/` (NFS from starlite `/mnt/media-raid/MEDIA/`)
- Docker on starlite blocks LAN access to arbitrary ports — use NFS paths, not HTTP
- RAID is 7.3TB, 91% full

## Channel Lineup (current)

| Ch | Name | Type | Source | Notes |
|----|------|------|--------|-------|
| 2 | GUIDE | guide | Built-in | On-screen TV guide with rotating FS42 logos, no audio |
| 3 | STARLITE 24 | standard | `/content/starlite24` | Drive-In Grindhouse films |
| 4 | WEATHER | streaming | `/mnt/media/weatherstar/index.m3u8` | WeatherStar NYC + SomaFM Vaporwaves, HLS from starlite |
| 5 | TV PARTY | standard | `/content/tv_party` | Music content |
| 6 | VJ/DJ | streaming | `/mnt/media/vjdj/hls/index.m3u8` | Music + random video clips + VJ effects, HLS from starlite |
| 7 | VIDEODROME | standard | `/content/videodrome` | Cyberpunk films/shows, shows in primetime |
| 9 | VHS TV | standard | `/content/vhs_tv` | VHS rips |
| 11 | MNN | standard | `/content/mnn` | Public access |
| 13 | MST3K | streaming | YouTube live stream | Capped at 720p H.264 via mpv.conf |
| 15 | OTCS | streaming | `otcs.minuspoint.com` HLS | External stream |

Channel configs on Pi: `/home/dratsum/git/FieldStation42/confs/*.json`
Local backups: `confs/vjdj.json`

### Streaming channels (duration=0)
Channels 4, 6, 13, 15 are all `"duration": 0` streaming channels. The player monitors them indefinitely via a fix in `station_player.py` (added `elif is_stream:` branch around line 656-705).

### Standard channels
Channels 3, 5, 7, 9, 11 use FieldStation42's built-in catalog/schedule system. They have commercial breaks from a `commercials` dir and bumps from a `bumps` dir. Empty bump/commercial dirs cause `KeyError` crashes.

## Services on Pi (StarliteTV)

### `fieldstation.service` — the TV player
```
ExecStart=/home/dratsum/git/FieldStation42/venv/bin/python field_player.py
Environment=DISPLAY=:0
Restart=on-failure
```

### `fieldstation-web.service` — web UI/API
```
ExecStart=/home/dratsum/git/FieldStation42/venv/bin/python station_42.py --server --limit_memory 0.8
Restart=on-failure
```

Both at `/etc/systemd/system/` on Pi.

## Pipelines on Starlite

### WeatherStar (Channel 4)
- Script: `starlite:~/weatherstar/start.sh`
- Architecture: Xvfb :94 (1280x720) -> Chromium kiosk -> FFmpeg x11grab + SomaFM audio -> HLS
- HLS output: `/mnt/media-raid/MEDIA/weatherstar/`
- Pi reads: `/mnt/media/weatherstar/index.m3u8`
- Video: libx264 ultrafast/zerolatency, 1500kbps, 15fps
- Audio: SomaFM Vaporwaves `http://ice1.somafm.com/vaporwaves-128-aac`
- **NOT a systemd service** — must manually restart after starlite reboot
- Full docs: `docs/weatherstar_setup.md`

### VJ/DJ Pipeline (Channel 6)
- Files: `starlite:~/vjdj/vj_pipeline.py`, `vj_effects.py`, `vj_config.json`
- Local backups: `docs/starlitetv_backups/vj_pipeline.py`, `vj_effects.py`, `vj_config.json`
- HLS output: `/mnt/media-raid/MEDIA/vjdj/hls/`
- Pi reads: `/mnt/media/vjdj/hls/index.m3u8`
- Broadcast hours: **10am to 2am** (sleeps 2am-10am)
- Content dirs on starlite: `/mnt/media-raid/MEDIA/vjdj/{music,clips,bumpers}/`
- Music has daypart subdirs: `overnight/` (2-8am), `daytime/` (8am-6pm), `nighttime/` (6pm-2am)
- Service file exists at `starlite:~/vjdj/vjdj.service` but **NOT installed yet**

#### VJ/DJ Architecture (important — hard-won knowledge)
```
Main Thread                    Feeder Thread              FFmpeg Streamer
┌─────────────┐               ┌──────────────┐           ┌──────────────┐
│ Pick track  │               │              │           │              │
│ Pick clip   │  queue.Queue  │ Read .ts     │  stdin    │ -re pacing   │
│ Pick effects├──────────────►│ Write bytes  ├──────────►│ -c copy      │
│ FFmpeg render│  (maxsize=20) │ Delete .ts   │  pipe     │ -f hls       │
│ to staging/ │               │              │           │ → segments   │
└─────────────┘               └──────────────┘           └──────────────┘
```

**How it works:**
1. Main thread renders clips (video clip + music track + VJ effects) to staging `.ts` files using FFmpeg with h264_nvenc (GPU encoding on GTX 1660 SUPER)
2. Each clip gets `-output_ts_offset` = cumulative duration of all prior clips, ensuring continuous MPEG-TS timestamps across concatenated clips
3. Rendered `.ts` paths are queued to a feeder thread
4. Feeder thread reads bytes from `.ts` file, writes to FFmpeg streamer's stdin pipe, then deletes the staging file
5. Streamer FFmpeg process reads MPEG-TS from stdin with `-re` (realtime pacing) and outputs HLS segments

**Critical design decisions:**
- Feeder thread deletes staging files (NOT main thread) — avoids race condition where main thread deletes files before feeder reads them
- `-output_ts_offset` on each render gives continuous timestamps — without this, `-re` gets confused when timestamps restart at 0 for each clip and causes burst/stall behavior
- `-re` on the streamer provides realtime pacing — the pipe blocks naturally
- h264_nvenc instead of libx264 — much faster rendering, lower power consumption
- `+genpts` on streamer input helps with timestamp handling

#### VJ Effects System (`vj_effects.py`)
Three tiers with weighted random selection (50% light, 35% medium, 15% heavy):
- **Light** (stack freely): color shifts, saturation, vignette, blur, brightness
- **Medium** (max 2/clip): frame blending, rgba shift, film grain, posterize, chromatic aberration
- **Heavy** (max 1/clip): edge glow, pixelate, psychedelic hue, zoompan pulse, quad mirror, solarize

**Known issue:** `zoompan` filter doesn't support `t` variable — must use `on` (frame number). Current filter uses `on/120` for 4-second cycle at 30fps.

#### VJ/DJ Config (`vj_config.json`)
```json
{
  "video": {"width": 1280, "height": 720, "fps": 30, "bitrate": "2500k",
            "codec": "h264_nvenc", "preset": "medium", "pix_fmt": "yuv420p"},
  "audio": {"codec": "aac", "bitrate": "192k", "sample_rate": 44100},
  "hls": {"segment_duration": 4, "list_size": 30, "flags": "delete_segments+omit_endlist"},
  "mixing": {"clip_min_duration": 10, "clip_max_duration": 45},
  "bumpers": {"interval_tracks": 4, "min_interval_minutes": 10}
}
```

#### Running the VJ/DJ pipeline manually
```bash
ssh dratsum@Starlite
cd ~/vjdj && python3 vj_pipeline.py
```

## Raspberry Pi 3 Hardware Constraints
- **CPU:** Quad-core ARM Cortex-A53 @ 1.2GHz, 1GB RAM
- **Hardware decode:** H.264 only via v4l2m2m — **HEVC (x265) is NOT supported** (causes 225% CPU and stutter)
- **Resolution:** 1080p (`video=HDMI-A-1:1920x1080@60` in `/boot/firmware/cmdline.txt`)
- **Display server:** labwc (Wayland) with Xwayland
- **Power:** 5V 2.5A, cable quality matters (`vcgencmd get_throttled` = 0x0 is good)
- WiFi disabled, ethernet only, static IP 192.168.0.121

### MPV config (`~/.config/mpv/mpv.conf` on Pi)
```
vo=gpu
hwdec=v4l2m2m
fullscreen=yes
ao=alsa
audio-device=alsa/plughw:1,0
cache=yes
demuxer-max-bytes=50M
demuxer-readahead-secs=10
autosync=30
framedrop=decoder+vo
mc=0.025
ytdl-format=bestvideo[height<=720][vcodec^=avc1]+bestaudio/best[height<=720]
```
The `ytdl-format` line caps YouTube streams (MST3K ch13) to 720p H.264.

## Common Operations

### On Pi (StarliteTV)
```bash
ssh dratsum@192.168.0.121
cd ~/git/FieldStation42
source venv/bin/activate

# Rebuild catalog (after adding content) — WARNING: resets all schedules to None
python3 station_42.py --rebuild_catalog

# Generate schedules (MUST do after catalog rebuild)
python3 station_42.py --schedule

# Restart services
sudo systemctl restart fieldstation fieldstation-web
```

### Schedule generation (on Pi, in venv)
```python
from fs42.liquid_schedule import LiquidSchedule
from fs42.station_io import StationIO
import json
sio = StationIO()
for conf_file in ['starlite_24', 'tv_party', 'videodrome', 'vhs_tv', 'mnn']:
    with open(f'confs/{conf_file}.json') as f:
        raw = json.load(f)
    station_conf = sio._process_single_config(raw, f'confs/{conf_file}.json')
    LiquidSchedule(station_conf).add_days(2)
```
**Important:** Must use `StationIO._process_single_config()` — raw JSON loading skips template resolution and clip_shows normalization, causing `AttributeError` on `.keys()`.

Guide channel does NOT auto-generate schedules — must generate after catalog rebuild.

## Key Lessons Learned

### Content management
- Empty bump/commercial dirs cause `KeyError: 'bumps'` crash in catalog.py — always populate them
- FieldStation42 standard channels recurse subdirectories — don't symlink to dirs with mixed content types
- `--rebuild_catalog` resets ALL schedules — always regenerate after
- Armitage III Poly Matrix excluded — HEVC, Pi can't decode it

### Streaming pipeline gotchas
- `duration=0` streams need the `elif is_stream:` fix in station_player.py or they immediately return FAILED
- MPEG-TS timestamp discontinuities break FFmpeg's `-re` pacing — use `-output_ts_offset` for continuous timestamps
- Staging file cleanup must happen in the consumer thread, not the producer — race condition otherwise
- FFmpeg zoompan filter uses `on` (frame number) not `t` (time) for expressions
- `append_list` HLS flag causes stale m3u8 entries across restarts — use `delete_segments+omit_endlist`
- Docker iptables on starlite blocks LAN access to arbitrary ports — always use NFS paths

### Cyberpunk content (on starlite at `/mnt/media-raid/MEDIA/Movies/Cyberpunk/`)
- Individual film symlinks, not whole-dir symlink (prevents recursive content mixing)
- Separate `shows`, `music`, `commercials` symlinks
- Videodrome schedule: shows in primetime 6-9pm, daytime blocks 8-11am/3-5pm

## File Locations Summary

| What | Where |
|------|-------|
| FieldStation42 code (Pi) | `/home/dratsum/git/FieldStation42/` |
| Local workspace | `/home/dratsum/git/Fieldstation42/` |
| Station configs (Pi) | `/home/dratsum/git/FieldStation42/confs/*.json` |
| Pi systemd services | `/etc/systemd/system/fieldstation*.service` |
| MPV config (Pi) | `~/.config/mpv/mpv.conf` |
| VJ/DJ pipeline (starlite) | `~/vjdj/vj_pipeline.py`, `vj_effects.py`, `vj_config.json` |
| VJ/DJ content (starlite) | `/mnt/media-raid/MEDIA/vjdj/{music,clips,bumpers}/` |
| VJ/DJ HLS output (starlite) | `/mnt/media-raid/MEDIA/vjdj/hls/` |
| WeatherStar script (starlite) | `~/weatherstar/start.sh` |
| WeatherStar HLS (starlite) | `/mnt/media-raid/MEDIA/weatherstar/` |
| Backups of Pi configs | `docs/starlitetv_backups/` |
| Backups of pipeline code | `docs/starlitetv_backups/vj_pipeline.py` etc. |

## TODO / Outstanding
- [ ] Install VJ/DJ as systemd service on starlite (`~/vjdj/vjdj.service` exists, needs `sudo cp` + `enable`)
- [ ] Install WeatherStar as systemd service on starlite
- [ ] Black Magic M-66 still needs subtitles (not on any public subtitle DB)
- [ ] VJ/DJ stream may still have occasional initial freeze on first clip (timestamp ramp-up)
- [ ] `subliminal` installed on starlite at `~/.local/bin/subliminal` — could use with OpenSubtitles API key
