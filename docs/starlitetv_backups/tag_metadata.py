#!/usr/bin/env python3
"""Tag video files with metadata from IMDB (via RapidAPI).

Reads video files, checks for existing metadata (title, date, artist, comment),
queries IMDB for missing data, and writes it back with ffmpeg.

Metadata convention:
  title   = Clean movie/show title
  date    = Release year (e.g. "1993")
  artist  = URL to movie poster / cover art
  comment = 1-2 sentence description

File naming convention:
  film-Title_Name_YYYY.ext
  show-Show_Name_YYYY-s01e01.ext
  short-Title_Name_YYYY.ext
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

RAPIDAPI_KEY = "e29a1f30b2msh3b4e6e9e61f285dp16e656jsne6f6a81c6cb7"
RAPIDAPI_HOST = "imdb236.p.rapidapi.com"
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".m4v", ".mov", ".webm", ".flv", ".ts"}


def get_existing_metadata(filepath):
    """Read existing metadata from a video file."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", filepath],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(result.stdout)
        tags = data.get("format", {}).get("tags", {})
        return {
            "title": tags.get("title", ""),
            "date": tags.get("date", ""),
            "artist": tags.get("artist", ""),
            "comment": tags.get("comment", ""),
        }
    except Exception as e:
        print(f"  ERROR reading metadata: {e}")
        return {}


def is_metadata_complete(meta):
    """Check if all required metadata fields are properly set."""
    if not meta.get("title"):
        return False
    if not meta.get("date"):
        return False
    if not meta.get("artist") or not meta["artist"].startswith("http"):
        return False
    if not meta.get("comment") or len(meta["comment"]) < 10:
        return False
    return True


def parse_title_year(filename):
    """Extract a clean title and year from a filename.

    Handles many naming conventions:
      film-The_Lawnmower_Man_1992.mp4
      show-Lain_1998-s01e01.mkv
      Ash.vs.Evil.Dead.S01E01.El.Jefe.1080p.AMZN.WEB-DL.mkv
      [Moozzi2] Serial Experiments Lain - 01 (BD 1520x1080).mkv
      Angel.Cop.1989.1080p.BluRay.x264-OFT.mkv
      Fallout.S02E03.1080p.AMZN.WEB-DL.mkv
    """
    name = os.path.splitext(filename)[0]

    # Strip prefix (film-, show-, short-, tv-, film_)
    name = re.sub(r'^(film|show|short|tv)[_-]', '', name)

    # Handle [Group] prefix: "[Moozzi2] Serial Experiments Lain - 01 (...)"
    bracket_match = re.match(r'^\[.*?\]\s*(.+)', name)
    if bracket_match:
        name = bracket_match.group(1)
        # Strip episode number and everything after: " - 01 (..." or " - 13 END (...)"
        name = re.sub(r'\s*-\s*\d+\s*(END\s*)?\(.*$', '', name, flags=re.IGNORECASE)
        # Also strip just " - 01" at end
        name = re.sub(r'\s*-\s*\d+\s*$', '', name)

    # Handle dot-separated names: "Ash.vs.Evil.Dead.S01E01.El.Jefe.1080p..."
    # or "Angel.Cop.1989.1080p.BluRay.x264-OFT"
    dot_year_val = None
    if '.' in name and '_' not in name:
        # Strip from SxxExx onward
        name = re.sub(r'\.S\d+E\d+.*$', '', name, flags=re.IGNORECASE)
        # Strip from resolution/quality markers onward
        name = re.sub(r'\.(1080p|720p|480p|2160p|BluRay|BDRip|WEB-DL|AMZN|HDTV|DVDRip).*$', '', name, flags=re.IGNORECASE)
        # Try to extract 4-digit year at end after dots
        dot_year = re.search(r'\.(\d{4})$', name)
        if dot_year:
            candidate = dot_year.group(1)
            if 1920 <= int(candidate) <= 2030:
                dot_year_val = candidate
                name = name[:dot_year.start()]
        # Convert dots to spaces
        name = name.replace('.', ' ')

    # Strip season/episode suffix (underscore-separated names)
    name = re.sub(r'-?s\d+e\d+.*$', '', name, flags=re.IGNORECASE)

    # Strip multi-episode markers like "-02" at end
    name = re.sub(r'-\d+$', '', name)

    # Try to extract year
    year_match = re.search(r'[_\-\s](\d{4})$', name)
    year = None
    if year_match:
        candidate = int(year_match.group(1))
        if 1920 <= candidate <= 2030:
            year = year_match.group(1)
            name = name[:year_match.start()]

    # Also try (YYYY) format
    if not year:
        year_match = re.search(r'\((\d{4})\)', name)
        if year_match:
            candidate = int(year_match.group(1))
            if 1920 <= candidate <= 2030:
                year = year_match.group(1)
                name = re.sub(r'\s*\(\d{4}\)', '', name)

    # Check if we extracted a year from the dot-name path above
    if not year and dot_year_val:
        year = dot_year_val

    # Convert underscores to spaces
    title = name.replace('_', ' ').strip()

    # Strip "Part1", "Part2" etc for cleaner search
    title = re.sub(r'\s*Part\s*\d+\s*$', '', title, flags=re.IGNORECASE).strip()

    # Strip trailing brackets, quality info, group tags
    title = re.sub(r'\s*\[.*?\]\s*$', '', title).strip()
    title = re.sub(r'\s*\(.*?(DVD|BD|BDRip|1080p|720p|x264|x265|HEVC|FLAC|Opus|Dual).*?\)\s*$', '', title, flags=re.IGNORECASE).strip()

    # Strip "Dual-Audio", codec info, etc from end
    title = re.sub(r'\s*(DVD|BD|BDRip|Dual.Audio|x264|x265|HEVC)\b.*$', '', title, flags=re.IGNORECASE).strip()

    return title, year


def imdb_autocomplete(query):
    """Search IMDB via RapidAPI autocomplete endpoint."""
    url = f"https://{RAPIDAPI_HOST}/api/imdb/autocomplete?query={urllib.parse.quote(query)}"
    req = urllib.request.Request(url, headers={
        "x-rapidapi-host": RAPIDAPI_HOST,
        "x-rapidapi-key": RAPIDAPI_KEY,
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return data if isinstance(data, list) else []
    except Exception as e:
        print(f"  API error: {e}")
        return []


def find_best_match(results, title, year=None):
    """Find the best IMDB match from autocomplete results."""
    if not results:
        return None

    title_lower = title.lower()
    # Also match without common suffixes
    title_words = set(title_lower.split())

    # First pass: exact title + year match
    if year:
        for r in results:
            r_title = r.get("primaryTitle", "").lower()
            r_year = str(r.get("startYear", ""))
            if r_title == title_lower and r_year == year:
                return r

    # Second pass: title contains or is contained in result + year match
    if year:
        for r in results:
            r_title = r.get("primaryTitle", "").lower()
            r_year = str(r.get("startYear", ""))
            if r_year == year and (title_lower in r_title or r_title in title_lower):
                return r

    # Third pass: year match + significant word overlap
    if year:
        for r in results:
            r_title = r.get("primaryTitle", "").lower()
            r_year = str(r.get("startYear", ""))
            r_words = set(r_title.split())
            overlap = title_words & r_words
            if r_year == year and len(overlap) >= min(2, len(title_words)):
                return r

    # Fourth pass: close year (within 2 years) + title match
    if year:
        y = int(year)
        for r in results:
            r_title = r.get("primaryTitle", "").lower()
            r_year_val = r.get("startYear")
            if r_year_val and abs(int(r_year_val) - y) <= 2:
                if title_lower in r_title or r_title in title_lower:
                    return r

    # Fallback: first result only if year matches or no year specified
    if results:
        if not year:
            return results[0]
        r_year = str(results[0].get("startYear", ""))
        if r_year == year:
            return results[0]

    return None


def write_metadata(filepath, title, date, artist, comment):
    """Write metadata to a video file using ffmpeg."""
    ext = os.path.splitext(filepath)[1].lower()
    tmp = filepath + ".tmp" + ext

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-i", filepath,
        "-metadata", f"title={title}",
        "-metadata", f"date={date}",
        "-metadata", f"artist={artist}",
        "-metadata", f"comment={comment}",
        "-c", "copy",
        tmp,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            os.replace(tmp, filepath)
            return True
        else:
            print(f"  ffmpeg error: {result.stderr[-200:]}")
            if os.path.exists(tmp):
                os.unlink(tmp)
            return False
    except Exception as e:
        print(f"  Write error: {e}")
        if os.path.exists(tmp):
            os.unlink(tmp)
        return False


def get_codec(filepath):
    """Get the video codec of a file."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", filepath],
            capture_output=True, text=True, timeout=30,
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def process_file(filepath, dry_run=False):
    """Process a single video file: check metadata, query IMDB if needed, write."""
    filename = os.path.basename(filepath)
    print(f"\n--- {filename} ---")

    meta = get_existing_metadata(filepath)
    if is_metadata_complete(meta):
        print(f"  OK: \"{meta['title']}\" ({meta['date']})")
        return "ok"

    # Parse title/year from filename
    title, year = parse_title_year(filename)
    if not title:
        print(f"  SKIP: Cannot parse title from filename")
        return "skip"

    print(f"  Parsed: \"{title}\" year={year or '?'}")

    # Query IMDB
    query = f"{title} {year}" if year else title
    time.sleep(0.3)  # rate limit
    results = imdb_autocomplete(query)

    if not results:
        # Try without year
        results = imdb_autocomplete(title)

    match = find_best_match(results, title, year)
    if not match:
        print(f"  NO MATCH found on IMDB")
        return "no_match"

    # Extract metadata
    new_title = match.get("primaryTitle", title) or title
    new_date = str(match.get("startYear", year or "") or "")
    new_artist = match.get("primaryImage", "") or ""
    new_comment = match.get("description", "") or ""

    # Truncate comment to 1-2 sentences if too long
    if new_comment and len(new_comment) > 300:
        # Find second period
        first_dot = new_comment.find('.', 20)
        if first_dot > 0:
            second_dot = new_comment.find('.', first_dot + 1)
            if second_dot > 0 and second_dot < 300:
                new_comment = new_comment[:second_dot + 1]
            else:
                new_comment = new_comment[:first_dot + 1]

    print(f"  IMDB: \"{new_title}\" ({new_date})")
    print(f"  Poster: {new_artist[:60]}..." if new_artist else "  Poster: none")
    print(f"  Desc: {new_comment[:80]}..." if len(new_comment) > 80 else f"  Desc: {new_comment}")

    if dry_run:
        print(f"  DRY RUN: would write metadata")
        return "would_tag"

    if write_metadata(filepath, new_title, new_date, new_artist, new_comment):
        print(f"  TAGGED successfully")
        return "tagged"
    else:
        print(f"  FAILED to write metadata")
        return "failed"


def scan_directory(dirpath, dry_run=False):
    """Scan a directory recursively for video files and process them."""
    stats = {"ok": 0, "tagged": 0, "no_match": 0, "failed": 0, "skip": 0,
             "would_tag": 0, "needs_transcode": []}

    for root, dirs, files in sorted(os.walk(dirpath)):
        for filename in sorted(files):
            ext = os.path.splitext(filename)[1].lower()
            if ext not in VIDEO_EXTENSIONS:
                continue
            # Skip subtitle files, staging files, etc.
            if filename.startswith('.'):
                continue

            filepath = os.path.join(root, filename)

            # Skip symlinks — process the target instead
            if os.path.islink(filepath):
                continue

            result = process_file(filepath, dry_run=dry_run)
            if result in stats:
                stats[result] += 1

            # Check codec
            codec = get_codec(filepath)
            if codec and codec not in ("h264", "unknown", ""):
                stats["needs_transcode"].append((filepath, codec))

    return stats


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Tag video files with IMDB metadata")
    parser.add_argument("dirs", nargs="+", help="Directories to scan")
    parser.add_argument("--dry-run", action="store_true", help="Don't write, just report")
    args = parser.parse_args()

    all_stats = {"ok": 0, "tagged": 0, "no_match": 0, "failed": 0, "skip": 0,
                 "would_tag": 0, "needs_transcode": []}

    for d in args.dirs:
        if not os.path.isdir(d):
            print(f"Not a directory: {d}")
            continue
        print(f"\n{'='*60}")
        print(f"Scanning: {d}")
        print(f"{'='*60}")
        stats = scan_directory(d, dry_run=args.dry_run)
        for k in all_stats:
            if k == "needs_transcode":
                all_stats[k].extend(stats[k])
            else:
                all_stats[k] += stats[k]

    print(f"\n{'='*60}")
    print(f"SUMMARY")
    print(f"{'='*60}")
    print(f"  Already tagged: {all_stats['ok']}")
    if args.dry_run:
        print(f"  Would tag:      {all_stats['would_tag']}")
    else:
        print(f"  Newly tagged:   {all_stats['tagged']}")
    print(f"  No IMDB match:  {all_stats['no_match']}")
    print(f"  Failed:         {all_stats['failed']}")
    print(f"  Skipped:        {all_stats['skip']}")

    if all_stats["needs_transcode"]:
        print(f"\n  NEEDS TRANSCODE (not H.264):")
        for filepath, codec in all_stats["needs_transcode"]:
            print(f"    {codec}: {filepath}")


if __name__ == "__main__":
    main()
