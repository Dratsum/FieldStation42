#!/usr/bin/env python3
"""VJ/DJ Pipeline — music + random video clips + effects → HLS stream.

Architecture:
  - Music thread: decodes tracks one-by-one to raw PCM, writes to a named pipe
    (FIFO). Tracks play continuously as a playlist, independent of video clips.
  - Main thread: renders random video clips (video-only, no audio) with VJ
    effects to staging .ts files, queues them for the feeder thread.
  - Feeder thread: reads .ts bytes from staging, writes to the streamer's stdin
    pipe, then deletes the staging file.
  - Streamer FFmpeg: reads video from stdin pipe + audio from the FIFO, muxes
    them into HLS output. Video is copied (already NVENC-encoded), audio is
    encoded to AAC from raw PCM.

Runs on starlite, outputs HLS to NFS share for FieldStation42 Pi playback.
"""

import datetime
import glob as globmod
import json
import logging
import logging.handlers
import os
import queue
import random
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import vj_effects

log = logging.getLogger("vjdj")
log.setLevel(logging.INFO)
_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                               datefmt="%Y-%m-%d %H:%M:%S")
# Rotate at 10 MB, keep 3 old logs
_handler = logging.handlers.RotatingFileHandler(
    Path(__file__).parent / "pipeline.log",
    maxBytes=10 * 1024 * 1024, backupCount=3)
_handler.setFormatter(_formatter)
log.addHandler(_handler)
# Also log to stderr so nohup/journal can capture it
_stderr = logging.StreamHandler()
_stderr.setFormatter(_formatter)
log.addHandler(_stderr)

SCRIPT_DIR = Path(__file__).parent
MEDIA_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".ts", ".m4v"}
AUDIO_EXTENSIONS = {".mp3", ".flac", ".ogg", ".m4a", ".wav", ".aac", ".opus", ".wma"}

MIN_FREE_BYTES = 1024 * 1024 * 1024  # 1 GB minimum free space
MAX_STAGING_FILES = 30               # Max .ts files allowed in staging dir
DISK_CHECK_INTERVAL = 30             # Seconds between disk-full sleeps


def load_config():
    config_path = SCRIPT_DIR / "vj_config.json"
    with open(config_path) as f:
        return json.load(f)


def probe_duration(filepath):
    """Get media file duration in seconds using ffprobe."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                str(filepath),
            ],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(result.stdout)
        return float(data["format"]["duration"])
    except Exception as e:
        log.warning("Could not probe duration of %s: %s", filepath, e)
        return None


def scan_media_files(directory, extensions):
    """Scan a directory recursively for media files, return list of (path, duration)."""
    files = []
    dirpath = Path(directory)
    if not dirpath.exists():
        log.warning("Directory does not exist: %s", directory)
        return files
    for f in sorted(dirpath.rglob("*")):
        if f.is_file() and f.suffix.lower() in extensions:
            dur = probe_duration(f)
            if dur and dur > 0:
                files.append((str(f), dur))
                log.debug("Found: %s (%.1fs)", f.name, dur)
    return files


def wait_for_disk_space(*paths):
    """Block until all paths have at least MIN_FREE_BYTES available."""
    while True:
        low = []
        for p in paths:
            try:
                usage = shutil.disk_usage(p)
                if usage.free < MIN_FREE_BYTES:
                    low.append((p, usage.free))
            except OSError:
                pass
        if not low:
            return
        for p, free in low:
            log.warning("Low disk space on %s: %.1f MB free (need %.0f MB) — pausing",
                        p, free / (1024 * 1024), MIN_FREE_BYTES / (1024 * 1024))
        time.sleep(DISK_CHECK_INTERVAL)


def count_staging_files(staging_dir):
    """Return the number of .ts files in the staging directory."""
    try:
        return len(globmod.glob(os.path.join(staging_dir, "*.ts")))
    except OSError:
        return 0


BROADCAST_START = 10  # 10am
BROADCAST_END = 2     # 2am (next day)


def is_on_air():
    """Return True if current time is within broadcast hours (10am-2am)."""
    hour = datetime.datetime.now().hour
    return hour >= BROADCAST_START or hour < BROADCAST_END


def wait_for_broadcast():
    """Sleep until broadcast hours begin. Returns when it's time to go on air."""
    now = datetime.datetime.now()
    sign_on = now.replace(hour=BROADCAST_START, minute=0, second=0, microsecond=0)
    if now.hour >= BROADCAST_END:
        pass
    else:
        sign_on += datetime.timedelta(days=1)
    wait_secs = (sign_on - now).total_seconds()
    if wait_secs > 0:
        log.info("Off air until %s (sleeping %.0f minutes)",
                 sign_on.strftime("%I:%M %p"), wait_secs / 60)
        time.sleep(wait_secs)


def get_current_daypart(cfg):
    """Return the current daypart config based on time of day."""
    hour = datetime.datetime.now().hour
    for dp in cfg.get("dayparts", []):
        start = dp["start_hour"]
        end = dp["end_hour"]
        if start < end:
            if start <= hour < end:
                return dp
        else:
            if hour >= start or hour < end:
                return dp
    return None


def get_daypart_music(cfg):
    """Get music files for the current daypart, falling back to all music."""
    dp = get_current_daypart(cfg)
    if dp:
        subdir = os.path.join(cfg["music_dir"], dp["subdir"])
        files = scan_media_files(subdir, AUDIO_EXTENSIONS)
        if files:
            log.info("Daypart '%s' — %d tracks from %s",
                     dp["name"], len(files), dp["subdir"])
            return files, dp["name"]
        log.warning("Daypart '%s' subdir empty, falling back to all music",
                    dp["name"])

    files = scan_media_files(cfg["music_dir"], AUDIO_EXTENSIONS)
    return files, "all"


_clips_cache = {"daypart": None, "files": None}


def get_daypart_clips(cfg, default_clips):
    """Get video clips for the current daypart.

    If clips_dayparts maps the current daypart name to a directory,
    scan that directory instead of the default clips list.
    Falls back to default_clips if no override or if the override dir is empty.
    Caches scan results to avoid re-probing every clip each iteration.
    """
    dp = get_current_daypart(cfg)
    clips_dayparts = cfg.get("clips_dayparts", {})
    dp_name = dp["name"] if dp else None

    # Return cached result if daypart hasn't changed
    if dp_name == _clips_cache["daypart"] and _clips_cache["files"] is not None:
        return _clips_cache["files"], dp_name or "default"

    if dp and dp_name in clips_dayparts:
        override_dir = clips_dayparts[dp_name]
        files = scan_media_files(override_dir, MEDIA_EXTENSIONS)
        if files:
            log.info("Clips daypart '%s' — %d clips from %s",
                     dp_name, len(files), override_dir)
            _clips_cache["daypart"] = dp_name
            _clips_cache["files"] = files
            return files, dp_name
        log.warning("Clips daypart '%s' dir empty, falling back to default",
                    dp_name)

    _clips_cache["daypart"] = dp_name
    _clips_cache["files"] = default_clips
    return default_clips, "default"


def pick_clip(clips, min_dur, max_dur):
    """Pick a random clip and return (path, seek_start, use_duration, needs_loop)."""
    clip_path, clip_dur = random.choice(clips)
    use_dur = random.uniform(min_dur, max_dur)

    if clip_dur <= use_dur:
        return clip_path, 0, clip_dur, True

    max_start = clip_dur - use_dur
    start = random.uniform(0, max_start) if max_start > 1 else 0
    return clip_path, start, use_dur, False


# ---------------------------------------------------------------------------
# Music player thread — plays tracks continuously, independent of video clips
# ---------------------------------------------------------------------------

def music_worker(cfg, audio_fifo_path, stop_event):
    """Decode music tracks to raw PCM and write to the audio FIFO.

    Runs as a daemon thread. Plays tracks sequentially from the current
    daypart playlist, reshuffling when exhausted or when daypart changes.
    The FIFO stays open the entire time so the streamer sees a continuous
    audio stream with no EOF between tracks.
    """
    current_daypart = None

    log.info("[Music] Opening audio FIFO (waiting for streamer)...")
    try:
        fifo_fd = open(audio_fifo_path, "wb")
    except OSError as e:
        log.error("[Music] Failed to open FIFO: %s", e)
        return
    log.info("[Music] FIFO connected")

    try:
        while not stop_event.is_set():
            # Get playlist for current daypart
            music_files, daypart_name = get_daypart_music(cfg)
            if not music_files:
                log.warning("[Music] No music files, sleeping 30s...")
                if stop_event.wait(30):
                    break
                continue

            if daypart_name != current_daypart:
                log.info("[Music] Daypart: %s", daypart_name)
                current_daypart = daypart_name

            playlist = list(music_files)
            random.shuffle(playlist)
            log.info("[Music] Shuffled playlist: %d tracks (%s)",
                     len(playlist), daypart_name)

            for track_idx, (track_path, track_dur) in enumerate(playlist):
                if stop_event.is_set():
                    break

                # Check for daypart change between tracks
                _, new_daypart = get_daypart_music(cfg)
                if new_daypart != current_daypart:
                    log.info("[Music] Daypart changed, reshuffling...")
                    break

                track_name = os.path.basename(track_path)
                log.info("[Music] Track %d/%d: %s (%.0fs)",
                         track_idx + 1, len(playlist), track_name, track_dur)

                # Decode track to raw PCM via FFmpeg, pipe output to our FIFO
                proc = subprocess.Popen(
                    [
                        "ffmpeg", "-v", "quiet",
                        "-i", track_path,
                        "-f", "s16le", "-ar", "44100", "-ac", "2",
                        "pipe:1",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )

                try:
                    while not stop_event.is_set():
                        chunk = proc.stdout.read(65536)
                        if not chunk:
                            break
                        try:
                            fifo_fd.write(chunk)
                            fifo_fd.flush()
                        except (BrokenPipeError, OSError):
                            log.warning("[Music] FIFO broken, stopping")
                            proc.kill()
                            return
                finally:
                    proc.stdout.close()
                    proc.wait()

            log.info("[Music] Playlist done, reshuffling...")

    finally:
        try:
            fifo_fd.close()
        except OSError:
            pass
        log.info("[Music] Thread exiting")


# ---------------------------------------------------------------------------
# Video rendering — video-only clips, no audio
# ---------------------------------------------------------------------------

def _build_scale_filter(video):
    """Build the standard scale/pad/setsar filter prefix."""
    return (f"scale={video['width']}:{video['height']}"
            f":force_original_aspect_ratio=decrease,"
            f"pad={video['width']}:{video['height']}:(ow-iw)/2:(oh-ih)/2,"
            f"setsar=1")


def render_clip(clip_path, clip_start, clip_dur, needs_loop,
                effects, cfg, output_path, ts_offset=0.0, speed=1.0):
    """Pre-render a single video clip with effects to a video-only .ts file."""
    video = cfg["video"]
    bug_path = cfg.get("bug_path")

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning"]

    if needs_loop:
        cmd += ["-stream_loop", "-1"]
    cmd += ["-ss", f"{clip_start:.2f}", "-t", f"{clip_dur:.2f}", "-i", clip_path]

    scale = _build_scale_filter(video)
    effect_str = vj_effects.build_filter_string(effects)

    if bug_path and os.path.exists(bug_path):
        # Use filter_complex for clip + logo overlay
        cmd += ["-i", bug_path]

        vf = f"{scale},setpts={speed}*PTS,fps={video['fps']}"
        if effect_str:
            vf += f",{effect_str}"

        filter_complex = (
            f"[0:v]{vf}[vid];"
            f"[1:v]colorchannelmixer=aa=0.5[bug];[vid][bug]overlay=W-w-45:H-h-40[out]"
        )
        cmd += ["-filter_complex", filter_complex, "-map", "[out]"]
    else:
        # Simple filter, no logo
        vf = f"{scale},setpts={speed}*PTS,fps={video['fps']}"
        if effect_str:
            vf += f",{effect_str}"
        cmd += ["-vf", vf]

    cmd += [
        "-an",
        "-c:v", video["codec"],
        "-preset", video["preset"],
        "-b:v", video["bitrate"],
        "-g", str(video["fps"] * 4),
        "-pix_fmt", video["pix_fmt"],
        "-output_ts_offset", f"{ts_offset:.3f}",
        "-f", "mpegts",
        output_path,
    ]

    clip_name = os.path.basename(clip_path)
    effect_names = vj_effects.effect_names(effects)
    speed_label = f" speed={speed}x" if speed != 1.0 else ""
    log.info("  Render: %s [%.0fs%s] fx=%s",
             clip_name, clip_dur, speed_label, effect_names)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            log.error("Render failed (rc=%d): %s",
                      result.returncode,
                      result.stderr[-500:] if result.stderr else "")
            try:
                os.unlink(output_path)
            except OSError:
                pass
            return False
        return True
    except Exception as e:
        log.error("Render exception: %s", e)
        try:
            os.unlink(output_path)
        except OSError:
            pass
        return False


def render_overlay_clip(clip1_path, clip1_start, clip2_path, clip2_start,
                        clip_dur, effects, blend_mode, cfg, output_path,
                        ts_offset=0.0, speed=1.0):
    """Render two clips composited together with a blend mode."""
    video = cfg["video"]
    bug_path = cfg.get("bug_path")
    scale = _build_scale_filter(video)
    effect_str = vj_effects.build_filter_string(effects)

    # Base clip: scale → speed → fps → effects
    base_filters = f"{scale},setpts={speed}*PTS,fps={video['fps']}"
    if effect_str:
        base_filters += f",{effect_str}"

    # Overlay clip: scale → speed → fps (clean, no effects)
    top_filters = f"{scale},setpts={speed}*PTS,fps={video['fps']}"

    if bug_path and os.path.exists(bug_path):
        filter_complex = (
            f"[0:v]{base_filters}[base];"
            f"[1:v]{top_filters}[top];"
            f"[base][top]blend=all_mode={blend_mode}[blended];"
            f"[2:v]colorchannelmixer=aa=0.5[bug];[blended][bug]overlay=W-w-45:H-h-40[out]"
        )
    else:
        filter_complex = (
            f"[0:v]{base_filters}[base];"
            f"[1:v]{top_filters}[top];"
            f"[base][top]blend=all_mode={blend_mode}[out]"
        )

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-ss", f"{clip1_start:.2f}", "-t", f"{clip_dur:.2f}", "-i", clip1_path,
        "-ss", f"{clip2_start:.2f}", "-t", f"{clip_dur:.2f}", "-i", clip2_path,
    ]
    if bug_path and os.path.exists(bug_path):
        cmd += ["-i", bug_path]
    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-an",
        "-c:v", video["codec"],
        "-preset", video["preset"],
        "-b:v", video["bitrate"],
        "-pix_fmt", video["pix_fmt"],
        "-output_ts_offset", f"{ts_offset:.3f}",
        "-f", "mpegts",
        output_path,
    ]

    clip1_name = os.path.basename(clip1_path)
    clip2_name = os.path.basename(clip2_path)
    effect_names = vj_effects.effect_names(effects)
    speed_label = f" speed={speed}x" if speed != 1.0 else ""
    log.info("  Overlay: %s + %s [%.0fs%s] blend=%s fx=%s",
             clip1_name, clip2_name, clip_dur, speed_label,
             blend_mode, effect_names)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            log.error("Overlay render failed (rc=%d): %s",
                      result.returncode,
                      result.stderr[-500:] if result.stderr else "")
            try:
                os.unlink(output_path)
            except OSError:
                pass
            return False
        return True
    except Exception as e:
        log.error("Overlay render exception: %s", e)
        try:
            os.unlink(output_path)
        except OSError:
            pass
        return False


def render_bumper(bumper_path, cfg, output_path, ts_offset=0.0):
    """Pre-render a bumper to a video-only .ts file (no logo bug)."""
    video = cfg["video"]
    scale = _build_scale_filter(video)

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
           "-i", bumper_path]
    cmd += ["-vf", f"{scale},fps={video['fps']}"]

    cmd += [
        "-an",
        "-c:v", video["codec"],
        "-preset", video["preset"],
        "-b:v", video["bitrate"],
        "-g", str(video["fps"] * 4),
        "-pix_fmt", video["pix_fmt"],
        "-output_ts_offset", f"{ts_offset:.3f}",
        "-f", "mpegts",
        output_path,
    ]

    log.info("  Render bumper: %s", os.path.basename(bumper_path))

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            try:
                os.unlink(output_path)
            except OSError:
                pass
            return False
        return True
    except Exception as e:
        log.error("Bumper render failed: %s", e)
        try:
            os.unlink(output_path)
        except OSError:
            pass
        return False


# ---------------------------------------------------------------------------
# Streamer — muxes video pipe + audio FIFO into HLS
# ---------------------------------------------------------------------------

def start_streamer(cfg, audio_fifo_path):
    """Start FFmpeg streamer that reads video from stdin pipe and audio from
    the named FIFO, muxing them into HLS output."""
    hls = cfg["hls"]
    audio = cfg["audio"]
    hls_dir = cfg["hls_dir"]
    hls_output = os.path.join(hls_dir, "index.m3u8")
    segment_pattern = os.path.join(hls_dir, "segment_%05d.ts")

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        # Video input: MPEG-TS from stdin
        "-re",
        "-fflags", "+genpts",
        "-f", "mpegts", "-i", "pipe:0",
        # Audio input: raw PCM from FIFO
        "-f", "s16le", "-ar", str(audio["sample_rate"]), "-ac", "2",
        "-thread_queue_size", "4096",
        "-i", audio_fifo_path,
        # Mapping
        "-map", "0:v", "-map", "1:a",
        # Video: copy (already encoded by NVENC)
        "-c:v", "copy",
        # Audio: normalize levels and encode to AAC
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
        "-c:a", audio["codec"],
        "-b:a", audio["bitrate"],
        "-ar", str(audio["sample_rate"]),
        # HLS output
        "-f", "hls",
        "-hls_time", str(hls["segment_duration"]),
        "-hls_list_size", str(hls["list_size"]),
        "-hls_flags", hls["flags"],
        "-hls_segment_filename", segment_pattern,
        hls_output,
    ]

    log.info("Starting HLS streamer (video pipe + audio FIFO → HLS)")
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return proc


def feed_streamer(proc, ts_path):
    """Write raw .ts bytes into the streamer's stdin."""
    try:
        with open(ts_path, "rb") as f:
            data = f.read()
        proc.stdin.write(data)
        proc.stdin.flush()
        size_mb = len(data) / (1024 * 1024)
        log.info("  Fed %.1f MB to streamer", size_mb)
        return True
    except (BrokenPipeError, OSError, ValueError) as e:
        log.warning("Streamer pipe broken: %s", e)
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    cfg = load_config()
    log.info("VJ/DJ Pipeline starting")
    log.info("Music: %s", cfg["music_dir"])
    log.info("Clips: %s", cfg["clips_dir"])
    log.info("Bumpers: %s", cfg["bumpers_dir"])
    log.info("HLS output: %s", cfg["hls_dir"])

    os.makedirs(cfg["hls_dir"], exist_ok=True)

    # Clean stale HLS segments from previous runs
    for old in globmod.glob(os.path.join(cfg["hls_dir"], "*.ts")):
        try:
            os.unlink(old)
        except OSError:
            pass
    for old in globmod.glob(os.path.join(cfg["hls_dir"], "*.m3u8")):
        try:
            os.unlink(old)
        except OSError:
            pass
    # Remove leftover subdirs from split audio/video approach
    for subdir in ["video", "audio"]:
        subpath = os.path.join(cfg["hls_dir"], subdir)
        if os.path.isdir(subpath):
            shutil.rmtree(subpath, ignore_errors=True)

    # Staging directory for pre-rendered .ts clips
    staging_dir = os.path.join(SCRIPT_DIR, "staging")
    os.makedirs(staging_dir, exist_ok=True)

    # Audio FIFO for music thread → streamer
    audio_fifo_path = os.path.join(staging_dir, "audio_pipe")
    if os.path.exists(audio_fifo_path):
        os.unlink(audio_fifo_path)
    os.mkfifo(audio_fifo_path)

    # Scan content
    log.info("Scanning video clips...")
    clip_files = scan_media_files(cfg["clips_dir"], MEDIA_EXTENSIONS)
    if not clip_files:
        log.error("No video clips found — exiting")
        sys.exit(1)
    log.info("Found %d video clips", len(clip_files))

    log.info("Scanning bumpers...")
    bumper_files = scan_media_files(cfg["bumpers_dir"], MEDIA_EXTENSIONS)
    if bumper_files:
        log.info("Found %d bumpers", len(bumper_files))
    else:
        log.warning("No bumpers found — bumper insertion disabled")

    # Config
    clip_min = cfg["mixing"]["clip_min_duration"]
    clip_max = cfg["mixing"]["clip_max_duration"]
    fx_min = cfg["mixing"]["effects_per_clip_min"]
    fx_max = cfg["mixing"]["effects_per_clip_max"]
    bumper_cfg = cfg["bumpers"]
    last_bumper_time = 0
    cumulative_ts = 0.0  # running timestamp offset for continuous MPEG-TS

    seq = 0
    streamer_proc = None
    music_stop_event = threading.Event()
    music_thread = None
    feed_queue = queue.Queue(maxsize=20)
    last_feed_time = time.time()  # watchdog: tracks last successful feed
    WATCHDOG_TIMEOUT = 90  # seconds with no feed before recovery

    def feeder_worker():
        """Thread that feeds rendered clips to the video streamer."""
        nonlocal last_feed_time
        while True:
            item = feed_queue.get()
            if item is None:
                break
            ts_path, proc = item
            if feed_streamer(proc, ts_path):
                last_feed_time = time.time()
            try:
                os.unlink(ts_path)
            except OSError:
                pass
            feed_queue.task_done()

    feeder_thread = threading.Thread(target=feeder_worker, daemon=True)
    feeder_thread.start()

    def start_music():
        """Start the music player thread."""
        nonlocal music_thread, music_stop_event
        music_stop_event = threading.Event()
        music_thread = threading.Thread(
            target=music_worker,
            args=(cfg, audio_fifo_path, music_stop_event),
            daemon=True,
        )
        music_thread.start()

    def stop_music():
        """Stop the music player thread."""
        if music_thread and music_thread.is_alive():
            music_stop_event.set()
            music_thread.join(timeout=10)

    def recover_streamer():
        """Watchdog recovery: tear down stuck streamer and restart fresh."""
        nonlocal streamer_proc, cumulative_ts, last_feed_time, prebuffer
        log.warning("WATCHDOG: No feed in %d seconds — recovering streamer",
                    WATCHDOG_TIMEOUT)

        try:
            # Kill streamer FFmpeg — do NOT close stdin from main thread
            # while the feeder thread may be writing to it (unsafe concurrent
            # fd access causes SIGPIPE). Just kill the process; the feeder
            # will get BrokenPipeError when the read end of the pipe closes.
            if streamer_proc and streamer_proc.poll() is None:
                streamer_proc.kill()
                try:
                    streamer_proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    log.warning("WATCHDOG: Streamer didn't exit after SIGKILL, "
                                "moving on")
            streamer_proc = None

            # Stop music thread (it's connected to the FIFO)
            stop_music()

            # Give the feeder thread a moment to notice the broken pipe
            # and finish its current item before we drain the queue
            time.sleep(1)

            # Drain the feed queue
            while not feed_queue.empty():
                try:
                    item = feed_queue.get_nowait()
                    if item is not None:
                        ts_path, _ = item
                        try:
                            os.unlink(ts_path)
                        except OSError:
                            pass
                    feed_queue.task_done()
                except queue.Empty:
                    break

            # Clean staging files
            for f in globmod.glob(os.path.join(staging_dir, "*.ts")):
                try:
                    os.unlink(f)
                except OSError:
                    pass

            # Recreate audio FIFO
            try:
                os.unlink(audio_fifo_path)
            except OSError:
                pass
            os.mkfifo(audio_fifo_path)

        except Exception:
            log.exception("WATCHDOG: Error during recovery")
            # Even if recovery partially failed, reset state so the
            # main loop can try again from prebuffer mode
            streamer_proc = None
            # Ensure the FIFO exists
            if not os.path.exists(audio_fifo_path):
                try:
                    os.mkfifo(audio_fifo_path)
                except OSError:
                    pass

        # Reset state — prebuffer mode will restart streamer + music
        cumulative_ts = 0.0
        prebuffer = []
        last_feed_time = time.time()
        log.info("WATCHDOG: Recovery complete, re-entering prebuffer phase")

    prebuffer_size = 4  # render this many clips before starting streamer
    prebuffer = []

    def start_streamer_and_flush():
        """Start the streamer and flush the pre-buffer to the feeder."""
        nonlocal streamer_proc, last_feed_time
        log.info("Pre-buffer full (%d clips), starting streamer...",
                 len(prebuffer))
        start_music()
        time.sleep(0.5)
        streamer_proc = start_streamer(cfg, audio_fifo_path)
        last_feed_time = time.time()  # reset watchdog so it doesn't fire immediately
        # Flush all pre-buffered clips to the feeder
        for ts in prebuffer:
            feed_queue.put((ts, streamer_proc))
        prebuffer.clear()
        log.info("Pre-buffer flushed, streaming live")

    def queue_clip(ts_path):
        """Queue a clip for the feeder thread, pre-buffering at startup."""
        nonlocal streamer_proc
        if streamer_proc is None:
            prebuffer.append(ts_path)
            log.info("  Pre-buffering clip %d/%d", len(prebuffer), prebuffer_size)
            if len(prebuffer) >= prebuffer_size:
                start_streamer_and_flush()
            return
        try:
            feed_queue.put((ts_path, streamer_proc), timeout=WATCHDOG_TIMEOUT)
        except queue.Full:
            log.warning("WATCHDOG: Queue full for %ds, triggering recovery",
                        WATCHDOG_TIMEOUT)
            recover_streamer()
            return
        qsize = feed_queue.qsize()
        log.info("  Queued for streamer (buffer: %d clips)", qsize)

    def cleanup(signum=None, frame=None):
        log.info("Shutting down...")
        stop_music()
        feed_queue.put(None)
        if streamer_proc and streamer_proc.poll() is None:
            try:
                streamer_proc.stdin.close()
            except OSError:
                pass
            streamer_proc.terminate()
            streamer_proc.wait(timeout=10)
        for f in globmod.glob(os.path.join(staging_dir, "*.ts")):
            try:
                os.unlink(f)
            except OSError:
                pass
        try:
            os.unlink(audio_fifo_path)
        except OSError:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)
    # Prevent SIGPIPE from killing the process when writing to broken pipes
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)

    # Main loop — continuously render random video clips
    while True:
      try:
        if not is_on_air():
            # Sign off: stop everything, sleep until broadcast
            log.info("Sign off — stopping pipeline")
            stop_music()
            if streamer_proc and streamer_proc.poll() is None:
                streamer_proc.kill()
                try:
                    streamer_proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                streamer_proc = None
            cumulative_ts = 0.0
            # Recreate FIFO for next broadcast
            try:
                os.unlink(audio_fifo_path)
            except OSError:
                pass
            os.mkfifo(audio_fifo_path)
            wait_for_broadcast()
            log.info("Sign on — resuming broadcast")

        # Watchdog: detect stalled feeder
        if (streamer_proc is not None
                and (time.time() - last_feed_time) > WATCHDOG_TIMEOUT):
            recover_streamer()
            continue  # restart loop in prebuffer mode

        # Guard: wait for disk space on staging and HLS partitions
        wait_for_disk_space(staging_dir, cfg["hls_dir"])

        # Guard: throttle if too many staging files are pending
        while count_staging_files(staging_dir) >= MAX_STAGING_FILES:
            log.warning("Staging has %d .ts files (max %d) — waiting for feeder",
                        count_staging_files(staging_dir), MAX_STAGING_FILES)
            time.sleep(5)

        # Check if bumper is due (time-based)
        now = time.time()
        if (bumper_files
                and last_bumper_time > 0
                and (now - last_bumper_time) >= bumper_cfg.get("min_interval_minutes", 10) * 60):
            bumper_path, bumper_dur = random.choice(bumper_files)
            seq += 1
            ts_path = os.path.join(staging_dir, f"clip_{seq:06d}.ts")
            if render_bumper(bumper_path, cfg, ts_path, ts_offset=cumulative_ts):
                cumulative_ts += bumper_dur
                last_bumper_time = time.time()
                queue_clip(ts_path)
        elif last_bumper_time == 0:
            last_bumper_time = time.time()

        # Get clips and daypart info
        active_clips, clips_daypart = get_daypart_clips(cfg, clip_files)
        dp = get_current_daypart(cfg)
        dp_name = dp["name"] if dp else None

        # Daypart-aware speed
        speed = vj_effects.pick_speed(dp_name)

        # Pick primary clip
        clip_path, clip_start, clip_dur, needs_loop = pick_clip(
            active_clips, clip_min, clip_max)
        effects = vj_effects.pick_effects(fx_min, fx_max, daypart=dp_name)

        seq += 1
        ts_path = os.path.join(staging_dir, f"clip_{seq:06d}.ts")

        # Output duration accounts for speed (PTS multiplier > 1 = longer)
        output_dur = clip_dur * speed

        if vj_effects.should_overlay(dp_name) and len(active_clips) >= 2:
            # Overlay: composite two clips together
            clip2_path, clip2_start, _, _ = pick_clip(
                active_clips, clip_min, clip_max)
            blend_mode = vj_effects.pick_blend_mode(dp_name)
            effects = vj_effects.pick_overlay_effects(fx_min, fx_max, daypart=dp_name)
            if render_overlay_clip(
                    clip_path, clip_start, clip2_path, clip2_start,
                    clip_dur, effects, blend_mode, cfg, ts_path,
                    ts_offset=cumulative_ts, speed=speed):
                cumulative_ts += output_dur
                queue_clip(ts_path)
            else:
                log.warning("Overlay render failed, skipping")
        elif render_clip(clip_path, clip_start, clip_dur, needs_loop,
                         effects, cfg, ts_path, ts_offset=cumulative_ts,
                         speed=speed):
            cumulative_ts += output_dur
            queue_clip(ts_path)
        else:
            log.warning("Clip render failed, skipping")

      except Exception:
            log.exception("FATAL: Unhandled exception in main loop — "
                          "attempting recovery")
            # Try to recover rather than crash
            try:
                recover_streamer()
            except Exception:
                log.exception("FATAL: Recovery also failed — restarting "
                              "from clean state")
                streamer_proc = None
                cumulative_ts = 0.0
                prebuffer = []
                last_feed_time = time.time()


if __name__ == "__main__":
    main()
