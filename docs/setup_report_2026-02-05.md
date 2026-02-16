# FieldStation42 Setup Report — 2026-02-05

## Network Overview

| Machine | IP | Role |
|---------|-----|------|
| **Starlite** | 192.168.0.51 | Media server (Linux Mint 20.3, GTX 1660 SUPER). Hosts RAID at `/mnt/media-raid` (7.3TB). Runs Docker, WeatherStar pipeline. |
| **StarliteTV** | 192.168.0.121 | Raspberry Pi 3 (Debian trixie/aarch64). FieldStation42 player device. |

**NFS Mount**: Pi mounts `192.168.0.51:/mnt/media-raid/MEDIA` at `/mnt/media` (NFSv4.2, rw, hard mount).

**SSH**: `dratsum@Starlite` (by hostname), `dratsum@192.168.0.121` (Pi — hostname `starlitetv` not in DNS on workstation).

---

## Channel Lineup (as of 2026-02-05)

| Ch | Name | Type | Content Source |
|----|------|------|----------------|
| 2 | GUIDE | guide | On-screen TV guide with rotating FS42 logos |
| 3 | STARLITE 24 | standard | Drive-In Grindhouse films |
| 4 | WEATHER | streaming | WeatherStar NYC + SomaFM Vaporwaves audio (HLS from starlite) |
| 5 | TV PARTY | standard | Music videos |
| 7 | VIDEODROME | standard | Cyberpunk films & shows |
| 9 | VHS TV | standard | VHS rips |
| 11 | MNN | standard | Public access content |
| 13 | MST3K | streaming | YouTube forever-a-thon (capped 720p) |
| 15 | OTCS | streaming | HLS from otcs.minuspoint.com |

---

## Services on StarliteTV (Pi)

Both are systemd services with `Restart=on-failure`, `RestartSec=5`.

### fieldstation.service
```ini
[Service]
Type=simple
User=dratsum
Environment=DISPLAY=:0
WorkingDirectory=/home/dratsum/git/FieldStation42
ExecStart=/home/dratsum/git/FieldStation42/venv/bin/python field_player.py
```
Runs the TV player (field_player.py). Outputs video to HDMI via mpv/ffplay.

### fieldstation-web.service
```ini
[Service]
Type=simple
User=dratsum
WorkingDirectory=/home/dratsum/git/FieldStation42
ExecStart=/home/dratsum/git/FieldStation42/venv/bin/python station_42.py --server --limit_memory 0.8
```
Runs the web remote/API server on port 4242. Memory limited to 80% of system RAM.

### Pi Boot Config
`/boot/firmware/cmdline.txt` includes `video=HDMI-A-1:1920x1080@60` for forced 1080p output.

---

## Content Directory Structure on Pi

All content lives under `/home/dratsum/git/FieldStation42/content/`. Tag subdirectories are symlinks into the NFS mount at `/mnt/media/`.

### content/starlite24/
```
movies       → /mnt/media/Movies/Drive-In Grindhouse
commercials  → /mnt/media/Pre Movie
bumps/         (real dir with bump symlinks)
```

### content/tv_party/
```
music        → /mnt/media/Pre Movie/Music
commercials  → /mnt/media/Pre Movie
bumps/         (real dir with bump symlinks)
```

### content/videodrome/ (REBUILT 2026-02-05)
```
movies/        (real dir with 16 individual film symlinks + 3 SRT symlinks)
shows        → /mnt/media/Movies/Cyberpunk/shows
music        → /mnt/media/Movies/Cyberpunk/music
commercials  → /mnt/media/Movies/Cyberpunk/commercials
bumps/         (real dir with 3 generic bump symlinks)
```

**Why movies/ is a real directory**: FieldStation42 standard networks recurse subdirectories when scanning tag directories. Previously `movies` was a symlink to the entire Cyberpunk root, which caused films, shows, music, and commercials to all be scanned as "movies" content. The fix was to create a real directory with individual symlinks to each film file.

**Films included** (16 total):
- film-Akira_1988.mp4
- film-Appleseed_1988.mkv (+.srt)
- film_Armitage III Dual Matrix (DVD x264 Dual-Audio Opus) [eva].mkv
- film-Black_Magic_M-66_1987.mkv
- film-Brainstorm_1983.mkv
- film-Cherry_2000_1987.mkv
- film-Ghost_in_the_Shell_1995.mp4
- film-Hardware_1990.mkv
- film-Metal_Skin_Panic_MADOX-01_1987.mkv (+.srt)
- film-RoboCop_1987.mkv
- film-RoboCop_2_1990.mkv
- film-Rollerball_1975.mkv
- film-Strange_Days_1995.mp4
- film-The_Lawnmower_Man_1992.mp4
- film-Wicked_City_1987.mp4 (+.srt)

**Excluded**: Armitage III Poly Matrix — HEVC (x265), Pi 3 cannot hardware decode it.

**Bumps** (3 generic from Pre Movie, shared):
- Bubblicious Bubble Gum Ad.mp4 → /mnt/media/Pre Movie/Trailers/Starlite/
- I have a bad case of Diarrhea - Japanese learning English.mp4 → /mnt/media/Pre Movie/Music/
- Pure Moods CD Commercial.mp4 → /mnt/media/Pre Movie/Music/

### content/vhs_tv/
```
movies       → /mnt/media/Movies/VHS
commercials  → /mnt/media/Pre Movie
bumps/         (real dir with bump symlinks)
```

VHS content (7 files): Annihilator (1986), Draghoula (1987), Tales from the Quadead Zone (1987), The Ice Pirates (1984), Licenced to Terminate, Millennium (1989), Ninja Showdown.

### content/mnn/
```
content      → /mnt/media/Public Access
commercials  → /mnt/media/Pre Movie
bumps/         (real dir with bump symlinks)
```

---

## Videodrome Schedule Design

The schedule uses two tags: `movies` and `shows`. Multi-tag arrays alternate by half-hour within each hour slot.

- **Overnight (midnight-7am)**: Movies only
- **Morning (8-11am)**: Movies + Shows (movies priority)
- **Midday (noon-2pm)**: Movies only
- **Afternoon (3-5pm)**: Movies + Shows (movies priority)
- **Prime Time (6-9pm)**: Shows + Movies (shows priority)
- **Late Night (10-11pm)**: Movies only

Shows content lives in `/mnt/media/Movies/Cyberpunk/shows/` and includes: Serial Experiments Lain, Angel Cop, Armitage III (OVA), Megazone 23.

---

## MPV Configuration on Pi

`~/.config/mpv/mpv.conf`:
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

The `ytdl-format` line caps YouTube (yt-dlp) streams to 720p H.264. This is critical for MST3K (ch 13) — without it, mpv defaults to 1080p@4.5Mbps which overwhelms the Pi 3's memory and causes stuttering/dropped frames. H.264 (avc1) is required for Pi 3 hardware decode (v4l2m2m).

---

## Cyberpunk Content on Starlite

Location: `/mnt/media-raid/MEDIA/Movies/Cyberpunk/`

### Directory Structure
```
Cyberpunk/
├── film-*.mp4/mkv          (15 film files)
├── film-*.srt              (3 subtitle files)
├── bumpers/                (empty — cyberpunk-specific bumpers not yet added)
├── commercials/            (1 file: Videodrome theatrical trailer)
├── music/                  (8 music videos — anime/J-pop)
└── shows/
    ├── Lain/               (Serial Experiments Lain)
    ├── tv-Angel_Cop/       (Angel Cop episodes — English audio, no subs needed)
    ├── tv-Armitage_III/    (Armitage III OVA episodes + 1 subtitle)
    └── tv-Megazone_23/     (Megazone 23 episodes + 1 subtitle)
```

### Subtitles Status
| File | Status |
|------|--------|
| film-Appleseed_1988.srt | Done (SubtitleCat) |
| film-Wicked_City_1987.srt | Done (SubtitleCat) |
| film-Metal_Skin_Panic_MADOX-01_1987.srt | Done (SubtitleCat) |
| tv-Megazone_23_s01e04.srt | Done (SubtitleCat, VTT→SRT) |
| tv-Armitage_III_s01e04.srt | Done (Kitsunekko) |
| film-Black_Magic_M-66_1987 | NO SUBS AVAILABLE |

### Changes Made 2026-02-05
1. Renamed `bumbers/` → `bumpers/` (typo fix)
2. Removed `Annihilator (1986) VHS rip.mp4` from `music/` (was a duplicate of the VHS copy)

---

## Schedule Generation

Schedules are stored in the SQLite database at `runtime/fs42_fluid.db` on the Pi. The schedule system works as follows:

1. **Catalog** (`ShowCatalog`): Scans content directories, indexes all media files with duration/tags. Stored in DB via `CatalogAPI`.
2. **LiquidSchedule**: Builds time-slot blocks by selecting content from the catalog based on the channel's `day_templates` config.
3. **Player**: When playing, if the schedule doesn't cover the current time, triggers `schedule_panic()` which generates 1 more day.
4. **Guide**: Queries existing schedules via `ScheduleQuery.query_slot()`. Does NOT trigger schedule generation — shows "No programming data" if schedule doesn't cover the queried time.

### Rebuilding
```bash
# On Pi — rebuild catalog for all channels:
cd /home/dratsum/git/FieldStation42
venv/bin/python station_42.py --rebuild_catalog

# Generate schedules for all standard channels (2 days):
venv/bin/python3 -c "
import logging; logging.basicConfig(level=logging.INFO)
from fs42.station_manager import StationManager
from fs42.liquid_schedule import LiquidSchedule
sm = StationManager()
for station in sm.stations:
    if station['network_type'] == 'standard':
        print(f'Generating: {station[\"network_name\"]}')
        LiquidSchedule(station).add_days(2)
"

# Restart services:
sudo systemctl restart fieldstation.service fieldstation-web.service
```

**Important**: `--rebuild_catalog` resets all schedules to None. You MUST regenerate schedules afterward or all channels will show "no programming" in the guide. The player itself will recover via schedule_panic, but the guide won't.

---

## Bugs Encountered and Fixes

### Empty bumps directory causes crash
**Symptom**: `KeyError: 'bumps'` in catalog.py crashes ALL channels.
**Cause**: Empty bumpers directory means no bump files are cataloged. When the schedule builder tries `self.clip_index[bump_tag]`, the key doesn't exist.
**Fix**: Always populate bump directories with at least some content. Generic bumps from `/mnt/media/Pre Movie/` work as fallbacks.

### Docker iptables blocks LAN ports on starlite
**Symptom**: Services on custom ports (e.g., 8004) are unreachable from LAN but work locally.
**Cause**: Docker's iptables rules block arbitrary inbound connections from LAN.
**Fix**: Don't expose services via HTTP on starlite. Use NFS share paths instead. WeatherStar writes HLS to NFS, Pi reads it directly.

### Guide "no programming data"
**Symptom**: Guide channel shows "No programming data available" for all channels.
**Cause**: `ScheduleQueryNotInBounds` — schedules don't cover current time. The guide's `query_slot()` catches exceptions silently and shows placeholder text.
**Fix**: Generate schedules for all standard channels using the script above. The player auto-extends but the guide does not.

---

## Outstanding / TODO

- [ ] **WeatherStar as systemd service** on starlite (currently manual start, dies on reboot)
- [ ] **Cyberpunk bumpers directory is empty** on starlite — channel-specific bumpers not yet created. Currently using generic Pre Movie bumps.
- [ ] **Black Magic M-66 subtitles** — not available on any public DB. May need DVD extraction.
- [ ] **Subtitle timing sync** not verified against actual video files
- [ ] **remote.starlitetv.local** doesn't load — web server is on port 4242, no nginx reverse proxy configured on Pi
- [ ] **MST3K YouTube URL** may need periodic updating if the stream goes down (`https://www.youtube.com/watch?v=B7qOZraAIlw`)
