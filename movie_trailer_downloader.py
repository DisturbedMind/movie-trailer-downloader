#!/usr/bin/env python3
"""
Download movie trailers into folders named like:

    C:\\movies\\Movie (2026)\\Movie (2026)-trailer.ext

Existing trailer files are renamed before new downloads, for example:

    Movie (2026)-trailer.mp4 -> Movie (2026)-trailer.mp4.old

On a successful new download, matching .old trailer backups are deleted.

Requires:
    python -m pip install -U yt-dlp

FFmpeg is strongly recommended for best-quality video+audio merges:
    winget install Gyan.FFmpeg
"""

from __future__ import annotations

import argparse
from ast import arguments
import importlib.util
import json
import queue
import random
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

yt_dlp = None


def command_version_at_least(executable: str, args: list[str], minimum: tuple[int, int]) -> bool:
    try:
        result = subprocess.run(
            [executable, *args],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        return False
    if result.returncode != 0:
        return False
    match = re.search(r"(\d+)\.(\d+)", f"{result.stdout} {result.stderr}")
    if not match:
        return False
    version = (int(match.group(1)), int(match.group(2)))
    return version >= minimum


def default_js_runtime_setting() -> str:
    runtimes = []
    deno = shutil.which("deno")
    node = shutil.which("node")
    if deno and command_version_at_least(deno, ["--version"], (2, 3)):
        runtimes.append("deno")
    if node and command_version_at_least(node, ["--version"], (22, 0)):
        runtimes.append("node")
    if shutil.which("qjs"):
        runtimes.append("quickjs")
    elif shutil.which("quickjs"):
        runtimes.append(f"quickjs:{shutil.which('quickjs')}")
    return ",".join(runtimes)


SETTINGS_PATH = Path(__file__).with_suffix(".settings.json")
INSTALLER_PATH = Path(__file__).with_name("install.ps1")
DEFAULT_COOKIES_PATH = Path(__file__).with_name("youtube-cookies.txt")
DEFAULT_RESULTS_PATH = Path(__file__).with_name("trailer-results.json")
DEFAULT_SEARCH_DELAY = 2.0
DEFAULT_MOVIE_DELAY = 5.0
DEFAULT_DOWNLOAD_SLEEP_MIN = 3.0
DEFAULT_DOWNLOAD_SLEEP_MAX = 8.0
DEFAULT_CANDIDATE_ATTEMPTS = 5
MIN_PYTHON_VERSION = (3, 14)
DEFAULT_FFMPEG_THREADS = 2
DEFAULT_FFMPEG_PRESET = "veryfast"
DEFAULT_FFMPEG_CRF = 22
DEFAULT_JS_RUNTIME = default_js_runtime_setting()
DEFAULT_REMOTE_COMPONENTS = "ejs:github"
FFMPEG_PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium")


TRAILER_WORDS = {"trailer", "teaser", "preview", "promo"}
BAD_WORDS = {
    "reaction",
    "review",
    "explained",
    "breakdown",
    "fanmade",
    "fan-made",
    "concept",
    "parody",
    "recap",
    "ending",
    "clip",
    "interview",
    "behind",
    "bts",
    "soundtrack",
    "lyrics",
    "song",
}


@dataclass(frozen=True)
class MovieFolder:
    folder: Path
    display_name: str
    title: str
    year: str | None


@dataclass(frozen=True)
class Candidate:
    url: str
    title: str
    channel: str
    duration: int | None
    score: int


def normalise_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2}


def require_yt_dlp() -> None:
    global yt_dlp
    if yt_dlp is not None:
        return
    try:
        import yt_dlp as yt_dlp_module
    except ImportError:
        print("Missing dependency: yt-dlp", file=sys.stderr)
        print("Install it with: python -m pip install -U yt-dlp", file=sys.stderr)
        raise SystemExit(2)
    yt_dlp = yt_dlp_module


def dependency_status() -> list[tuple[str, bool, str]]:
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    python_ok = sys.version_info >= MIN_PYTHON_VERSION
    status = [("Python 3.14+", python_ok, python_version)]
    status.append(("yt-dlp", importlib.util.find_spec("yt_dlp") is not None, "python package yt-dlp[default]"))

    ffmpeg = shutil.which("ffmpeg")
    status.append(("FFmpeg", bool(ffmpeg), ffmpeg or "required for MP4 conversion and audio normalization"))

    deno = shutil.which("deno")
    node = shutil.which("node")
    deno_ok = bool(deno and command_version_at_least(deno, ["--version"], (2, 3)))
    node_ok = bool(node and command_version_at_least(node, ["--version"], (22, 0)))
    js_detail = []
    if deno:
        js_detail.append(f"Deno {'OK' if deno_ok else 'too old'}")
    if node:
        js_detail.append(f"Node {'OK' if node_ok else 'too old'}")
    status.append(("YouTube EJS runtime", deno_ok or node_ok, ", ".join(js_detail) or "install Deno 2.3+ or Node.js 22+"))
    status.append(("Installer", INSTALLER_PATH.exists(), str(INSTALLER_PATH)))
    return status


def missing_dependency_names() -> list[str]:
    return [name for name, ok, _detail in dependency_status() if not ok and name != "Installer"]


def print_dependency_status() -> None:
    print("Dependency check:")
    for name, ok, detail in dependency_status():
        marker = "OK" if ok else "MISSING"
        print(f"  {marker:7} {name}: {detail}")


def is_cookie_decrypt_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "dpapi" in message or "failed to decrypt" in message


def is_forbidden_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "403" in message or "forbidden" in message


def is_format_unavailable_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "requested format is not available" in message or "only images are available" in message


def is_ejs_challenge_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "n challenge" in message or "challenge solver" in message or "javascript runtime" in message


def without_browser_cookies(opts: dict) -> dict:
    clean_opts = dict(opts)
    clean_opts.pop("cookiesfrombrowser", None)
    return clean_opts


def without_any_cookies(opts: dict) -> dict:
    clean_opts = without_browser_cookies(opts)
    clean_opts.pop("cookiefile", None)
    return clean_opts


def parse_js_runtimes(value: str) -> dict:
    runtimes = {}
    for part in str(value or "").split(","):
        part = part.strip()
        if not part:
            continue
        runtime, _, runtime_path = part.partition(":")
        runtime = runtime.strip().lower()
        runtime_path = runtime_path.strip()
        if not runtime:
            continue
        runtimes[runtime] = {"path": runtime_path} if runtime_path else {}
    return runtimes


def merge_js_runtimes(primary: dict | None, fallback: dict | None = None) -> dict:
    merged = dict(fallback or {})
    for runtime, config in (primary or {}).items():
        merged[runtime] = config
    return merged


def parse_remote_components(value: str) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def with_ejs_solver_options(opts: dict) -> dict:
    tuned_opts = dict(opts)
    detected_runtimes = parse_js_runtimes(DEFAULT_JS_RUNTIME)
    tuned_opts["js_runtimes"] = merge_js_runtimes(tuned_opts.get("js_runtimes"), detected_runtimes)

    remote_components = list(tuned_opts.get("remote_components") or [])
    if "ejs:github" not in remote_components:
        remote_components.append("ejs:github")
    tuned_opts["remote_components"] = remote_components
    return tuned_opts


def ejs_retry_option_sets(opts: dict) -> list[tuple[str, dict]]:
    ejs_opts = with_ejs_solver_options(opts)
    if ejs_opts == opts:
        return []
    return [
        ("detected EJS runtime and GitHub solver components", ejs_opts),
        (
            "detected EJS runtime, GitHub solver components, and alternate YouTube client profile",
            with_youtube_player_clients(ejs_opts, ["default", "web", "web_embedded", "mweb"]),
        ),
    ]


def with_youtube_player_clients(opts: dict, clients: list[str]) -> dict:
    tuned_opts = dict(opts)
    extractor_args = dict(tuned_opts.get("extractor_args") or {})
    youtube_args = dict(extractor_args.get("youtube") or {})
    youtube_args["player_client"] = clients
    extractor_args["youtube"] = youtube_args
    tuned_opts["extractor_args"] = extractor_args
    return tuned_opts


def youtube_retry_option_sets(opts: dict) -> list[tuple[str, dict]]:
    profiles = [
        ("alternate YouTube client profile", with_youtube_player_clients(opts, ["default", "web", "web_embedded", "mweb"])),
        (
            "web-only YouTube client profile",
            with_youtube_player_clients(opts, ["web", "web_embedded"]),
        ),
    ]
    if "cookiefile" in opts or "cookiesfrombrowser" in opts:
        no_cookie_opts = without_any_cookies(opts)
        profiles.extend(
            [
                ("without cookies", no_cookie_opts),
                (
                    "without cookies and alternate YouTube client profile",
                    with_youtube_player_clients(no_cookie_opts, ["default", "web", "web_embedded", "mweb"]),
                ),
            ]
        )
    return profiles


def extract_cookies_file(browser: str, cookies_file: Path) -> Path:
    require_yt_dlp()
    cookies_file = cookies_file.expanduser()
    cookies_file.parent.mkdir(parents=True, exist_ok=True)
    opts = {
        "cookiesfrombrowser": (browser,),
        "quiet": False,
        "no_warnings": False,
    }
    print(f"Extracting cookies from {browser}...")
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.cookiejar.save(str(cookies_file), ignore_discard=True, ignore_expires=True)
    print(f"Saved cookies file: {cookies_file}")
    return cookies_file


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def polite_sleep(seconds: float) -> None:
    if seconds <= 0:
        return
    jitter = min(seconds * 0.25, 2.0)
    time.sleep(max(0.0, seconds + random.uniform(-jitter, jitter)))


def movie_key(movie: MovieFolder) -> str:
    try:
        return str(movie.folder.resolve()).lower()
    except OSError:
        return str(movie.folder.absolute()).lower()


def load_results(results_file: Path) -> dict:
    if not results_file.exists():
        return {"version": 1, "movies": {}}
    try:
        data = json.loads(results_file.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "movies": {}}
    if not isinstance(data, dict):
        return {"version": 1, "movies": {}}
    data.setdefault("version", 1)
    if not isinstance(data.get("movies"), dict):
        data["movies"] = {}
    return data


def save_results(results_file: Path, results: dict) -> None:
    results_file.parent.mkdir(parents=True, exist_ok=True)
    results_file.write_text(json.dumps(results, indent=2, sort_keys=True), encoding="utf-8")


def result_trailer_path(record: dict) -> Path | None:
    trailer_path = record.get("trailer_path")
    if not trailer_path:
        return None
    return Path(str(trailer_path))


def successful_result_for_movie(movie: MovieFolder, results: dict) -> dict | None:
    record = results.get("movies", {}).get(movie_key(movie))
    if not isinstance(record, dict) or record.get("status") != "success":
        return None
    trailer_path = result_trailer_path(record)
    if trailer_path and trailer_path.exists():
        return record
    return None


def record_success(movie: MovieFolder, target: Path, candidate: Candidate, results: dict) -> None:
    results.setdefault("movies", {})[movie_key(movie)] = {
        "status": "success",
        "movie_folder": str(movie.folder),
        "movie_name": movie.display_name,
        "title": movie.title,
        "year": movie.year,
        "trailer_path": str(target),
        "trailer_name": target.name,
        "trailer_size_bytes": target.stat().st_size if target.exists() else None,
        "source_url": candidate.url,
        "source_title": candidate.title,
        "source_channel": candidate.channel,
        "source_duration": candidate.duration,
        "source_score": candidate.score,
        "last_success_utc": now_utc_iso(),
    }


def record_existing_success(movie: MovieFolder, target: Path, results: dict) -> None:
    results.setdefault("movies", {})[movie_key(movie)] = {
        "status": "success",
        "movie_folder": str(movie.folder),
        "movie_name": movie.display_name,
        "title": movie.title,
        "year": movie.year,
        "trailer_path": str(target),
        "trailer_name": target.name,
        "trailer_size_bytes": target.stat().st_size if target.exists() else None,
        "source_url": None,
        "source_title": "Existing local trailer",
        "source_channel": None,
        "source_duration": None,
        "source_score": None,
        "last_success_utc": now_utc_iso(),
    }


def parse_movie_folder(folder: Path) -> MovieFolder:
    display_name = folder.name.strip()
    match = re.match(r"^(?P<title>.+?)\s*\((?P<year>\d{4})\)\s*$", display_name)
    if not match:
        return MovieFolder(folder, display_name, display_name, None)
    return MovieFolder(folder, display_name, match.group("title").strip(), match.group("year"))


def iter_movie_folders(root: Path) -> Iterable[MovieFolder]:
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if child.is_dir():
            yield parse_movie_folder(child)


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    counter = 1
    while True:
        candidate = path.with_name(f"{path.name}.{counter}")
        if not candidate.exists():
            return candidate
        counter += 1


def is_matching_trailer_backup(movie: MovieFolder, item: Path) -> bool:
    prefix = f"{movie.display_name}-trailer".lower()
    lower_name = item.name.lower()
    return item.is_file() and lower_name.startswith(prefix) and (lower_name.endswith(".old") or ".old." in lower_name)


def delete_old_trailer_backups(movie: MovieFolder, dry_run: bool) -> list[Path]:
    deleted: list[Path] = []
    for item in sorted(movie.folder.iterdir(), key=lambda p: p.name.lower()):
        if not is_matching_trailer_backup(movie, item):
            continue
        deleted.append(item)
        if not dry_run:
            item.unlink()
    return deleted


def find_current_trailers(movie: MovieFolder) -> list[Path]:
    prefix = f"{movie.display_name}-trailer".lower()
    current: list[Path] = []
    for item in sorted(movie.folder.iterdir(), key=lambda p: p.name.lower()):
        if not item.is_file():
            continue
        lower_name = item.name.lower()
        if not lower_name.startswith(prefix):
            continue
        if lower_name.endswith(".old") or ".old." in lower_name:
            continue
        if lower_name.endswith((".part", ".ytdl", ".tmp")):
            continue
        current.append(item)
    return current


def restore_renamed_trailers(renamed: list[tuple[Path, Path]]) -> list[tuple[Path, Path]]:
    restored: list[tuple[Path, Path]] = []
    for original, backup in renamed:
        if backup.exists() and not original.exists():
            backup.rename(original)
            restored.append((backup, original))
    return restored


def rename_existing_trailers(movie: MovieFolder, dry_run: bool) -> list[tuple[Path, Path]]:
    prefix = f"{movie.display_name}-trailer".lower()
    renamed: list[tuple[Path, Path]] = []

    for item in sorted(movie.folder.iterdir(), key=lambda p: p.name.lower()):
        if not item.is_file():
            continue
        lower_name = item.name.lower()
        if not lower_name.startswith(prefix):
            continue
        if lower_name.endswith(".old") or ".old." in lower_name:
            continue
        if lower_name.endswith((".part", ".ytdl", ".tmp")):
            continue

        target = unique_path(item.with_name(f"{item.name}.old"))
        renamed.append((item, target))
        if not dry_run:
            item.rename(target)

    return renamed


def build_queries(movie: MovieFolder, include_vevo: bool) -> list[str]:
    base = f"{movie.title} {movie.year}".strip() if movie.year else movie.title
    queries = [
        f"{base} official trailer",
        f"{base} trailer",
        f"{base} teaser trailer",
        f"{base} final trailer",
        f"{base} official movie trailer",
        f"{base} movieclips trailers",
        f"{base} rotten tomatoes trailers",
        f"{base} one media trailer",
        f"{base} kinocheck trailer",
        f"{base} ign trailer",
    ]
    if include_vevo:
        queries.extend(
            [
                f"{base} vevo trailer",
                f"{base} vevo official video trailer",
            ]
        )
    return list(dict.fromkeys(queries))


def score_candidate(movie: MovieFolder, entry: dict, max_duration: int) -> int | None:
    title = str(entry.get("title") or "")
    channel = str(entry.get("channel") or entry.get("uploader") or "")
    duration = entry.get("duration")

    if duration and int(duration) > max_duration:
        return None

    haystack = f"{title} {channel}".lower()
    title_words = normalise_words(movie.title)
    hit_words = title_words & normalise_words(haystack)

    if title_words and len(hit_words) < max(1, min(3, len(title_words))):
        return None
    if movie.year and movie.year not in haystack:
        # Allow missing years, but prefer entries that include them.
        score = -2
    else:
        score = 0

    if not any(word in haystack for word in TRAILER_WORDS):
        return None

    score += 10
    score += 3 * sum(1 for word in TRAILER_WORDS if word in haystack)
    score += 5 if "official" in haystack else 0
    score += 4 if "trailer" in haystack else 0
    score += 3 if movie.year and movie.year in haystack else 0
    score += 2 if "movieclips trailers" in haystack else 0
    score += 2 if "rotten tomatoes trailers" in haystack else 0
    score += 2 if "one media" in haystack else 0
    score += 2 if "kinocheck" in haystack else 0
    score += 1 if "vevo" in haystack else 0
    score -= 8 * sum(1 for word in BAD_WORDS if word in haystack)

    if duration:
        # Most trailers are roughly 60-240 seconds.
        seconds = int(duration)
        if 60 <= seconds <= 240:
            score += 4
        elif seconds < 30:
            score -= 4

    return score


def collect_candidates(
    movie: MovieFolder,
    search_results: int,
    max_duration: int,
    include_vevo: bool,
    ydl_base_opts: dict,
    search_delay: float = DEFAULT_SEARCH_DELAY,
) -> list[Candidate]:
    def search_with_options(search_opts: dict) -> list[Candidate]:
        seen: set[str] = set()
        found: list[Candidate] = []
        queries = build_queries(movie, include_vevo)

        with yt_dlp.YoutubeDL(search_opts) as ydl:
            for query_index, query in enumerate(queries):
                if query_index:
                    polite_sleep(search_delay)
                try:
                    info = ydl.extract_info(f"ytsearch{search_results}:{query}", download=False)
                except Exception as exc:
                    if "cookiesfrombrowser" in search_opts and is_cookie_decrypt_error(exc):
                        raise
                    if is_forbidden_error(exc):
                        raise
                    print(f"    search failed for {query!r}: {exc}")
                    continue

                for entry in info.get("entries") or []:
                    if not entry:
                        continue
                    video_id = str(entry.get("id") or entry.get("url") or "")
                    if not video_id or video_id in seen:
                        continue
                    seen.add(video_id)

                    score = score_candidate(movie, entry, max_duration)
                    if score is None:
                        continue

                    webpage_url = entry.get("webpage_url")
                    url = str(webpage_url or f"https://www.youtube.com/watch?v={video_id}")
                    found.append(
                        Candidate(
                            url=url,
                            title=str(entry.get("title") or video_id),
                            channel=str(entry.get("channel") or entry.get("uploader") or ""),
                            duration=int(entry["duration"]) if entry.get("duration") else None,
                            score=score,
                        )
                    )
        return found

    opts = {
        **ydl_base_opts,
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
    }

    retried_without_cookies = False
    try:
        candidates = search_with_options(opts)
    except Exception as exc:
        if "cookiesfrombrowser" in opts and is_cookie_decrypt_error(exc):
            print("    browser cookie decrypt failed; retrying searches without browser cookies")
            retried_without_cookies = True
            candidates = search_with_options(without_browser_cookies(opts))
        elif is_forbidden_error(exc):
            candidates = []
            retried_without_cookies = "cookiefile" in opts or "cookiesfrombrowser" in opts
            unblocked = False
            for label, retry_opts in youtube_retry_option_sets(opts):
                try:
                    print(f"    YouTube returned 403; retrying searches {label}")
                    candidates = search_with_options(retry_opts)
                    unblocked = True
                    break
                except Exception as retry_exc:
                    if is_forbidden_error(retry_exc) or is_cookie_decrypt_error(retry_exc):
                        continue
                    raise
            if not unblocked:
                print("    YouTube search is still blocked with HTTP 403; refresh cookies or try again later")
        else:
            raise

    if candidates:
        return sorted(candidates, key=lambda c: c.score, reverse=True)

    if "cookiesfrombrowser" not in opts or retried_without_cookies:
        return []

    print("    retrying searches without browser cookies")
    return sorted(search_with_options(without_browser_cookies(opts)), key=lambda c: c.score, reverse=True)


def clean_temp_dir(temp_dir: Path) -> None:
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)


def find_downloaded_file(temp_dir: Path, base_name: str) -> Path | None:
    matches = [
        p
        for p in temp_dir.iterdir()
        if p.is_file()
        and p.stem == base_name
        and not p.name.lower().endswith((".part", ".ytdl", ".tmp"))
    ]
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def run_ffmpeg(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(details[-1200:] if details else f"ffmpeg exited with code {result.returncode}")


def install_dependencies() -> int:
    if not INSTALLER_PATH.exists():
        print(f"Installer not found: {INSTALLER_PATH}")
        return 1
    command = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(INSTALLER_PATH),
    ]
    return subprocess.call(command)


def convert_to_normalized_mp4(
    source: Path,
    output: Path,
    threads: int = DEFAULT_FFMPEG_THREADS,
    preset: str = DEFAULT_FFMPEG_PRESET,
    crf: int = DEFAULT_FFMPEG_CRF,
) -> Path:
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise RuntimeError("FFmpeg was not found on PATH. Install it with: winget install Gyan.FFmpeg")

    threads = max(1, min(int(threads), 16))
    preset = preset if preset in FFMPEG_PRESETS else DEFAULT_FFMPEG_PRESET
    crf = max(16, min(int(crf), 30))

    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()

    base_command = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-threads",
        str(threads),
        "-filter_threads",
        "1",
        "-filter_complex_threads",
        "1",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
    ]
    try:
        run_ffmpeg(base_command + ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11", str(output)])
    except RuntimeError:
        if output.exists():
            output.unlink()
        run_ffmpeg(base_command + [str(output)])

    return output


def download_format_option_sets(base_opts: dict, outtmpl: str) -> list[tuple[str, dict]]:
    common_opts = {
        **base_opts,
        "outtmpl": outtmpl,
        "noplaylist": True,
        "windowsfilenames": True,
        "quiet": True,
        "no_warnings": True,
    }
    return [
        (
            "best available video+audio",
            {
                **common_opts,
                "format": "bestvideo*+bestaudio/best",
            },
        ),
        (
            "yt-dlp automatic format",
            {
                **common_opts,
            },
        ),
        (
            "best single-file format",
            {
                **common_opts,
                "format": "best/b",
            },
        ),
        (
            "compatible single-file fallback",
            {
                **common_opts,
                "format": "best/best[ext=mp4]/b",
            },
        ),
    ]


def run_download_with_retries(opts: dict, url: str) -> None:
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as exc:
        if "cookiesfrombrowser" in opts and is_cookie_decrypt_error(exc):
            print("    browser cookie decrypt failed; retrying download without browser cookies")
            with yt_dlp.YoutubeDL(without_browser_cookies(opts)) as ydl:
                ydl.download([url])
        elif is_forbidden_error(exc):
            last_error: Exception = exc
            downloaded_ok = False
            for label, retry_opts in youtube_retry_option_sets(opts):
                try:
                    print(f"    YouTube returned 403; retrying download {label}")
                    with yt_dlp.YoutubeDL(retry_opts) as ydl:
                        ydl.download([url])
                    downloaded_ok = True
                    break
                except Exception as retry_exc:
                    last_error = retry_exc
                    if is_forbidden_error(retry_exc) or is_cookie_decrypt_error(retry_exc):
                        continue
                    raise
            if not downloaded_ok:
                print("    YouTube download is still blocked with HTTP 403; refresh cookies or try again later")
                raise last_error
        else:
            raise


def download_candidate(
    movie: MovieFolder,
    candidate: Candidate,
    index: int,
    temp_dir: Path,
    ydl_base_opts: dict,
    dry_run: bool,
    ffmpeg_threads: int = DEFAULT_FFMPEG_THREADS,
    ffmpeg_preset: str = DEFAULT_FFMPEG_PRESET,
    ffmpeg_crf: int = DEFAULT_FFMPEG_CRF,
) -> Path | None:
    trailer_stem = f"{movie.display_name}-trailer"
    if dry_run:
        print(f"    would download #{index}: {candidate.title} [{candidate.channel}]")
        print(f"      {candidate.url}")
        return None

    outtmpl = str(temp_dir / f"{trailer_stem}.%(ext)s")
    format_profiles = download_format_option_sets(ydl_base_opts, outtmpl)
    last_error: Exception | None = None
    tried_ejs_recovery = False
    print(f"    trying #{index}: {candidate.title}")
    for profile_index, (label, opts) in enumerate(format_profiles, start=1):
        clean_temp_dir(temp_dir)
        if profile_index > 1:
            print(f"    retrying #{index} with {label}")
        try:
            run_download_with_retries(opts, candidate.url)
            last_error = None
            break
        except Exception as exc:
            last_error = exc
            if is_format_unavailable_error(exc) and not tried_ejs_recovery:
                tried_ejs_recovery = True
                recovered = False
                for ejs_label, ejs_opts in ejs_retry_option_sets(opts):
                    clean_temp_dir(temp_dir)
                    print(f"    retrying #{index} with {ejs_label}")
                    try:
                        run_download_with_retries(ejs_opts, candidate.url)
                        last_error = None
                        recovered = True
                        break
                    except Exception as retry_exc:
                        last_error = retry_exc
                        if (
                            is_format_unavailable_error(retry_exc)
                            or is_forbidden_error(retry_exc)
                            or is_cookie_decrypt_error(retry_exc)
                            or is_ejs_challenge_error(retry_exc)
                        ):
                            continue
                        raise
                if recovered:
                    break
            if is_format_unavailable_error(exc) and profile_index < len(format_profiles):
                print(f"    format unavailable for #{index}; trying another format selector")
                continue
            raise

    if last_error is not None:
        raise last_error

    downloaded = find_downloaded_file(temp_dir, trailer_stem)
    if not downloaded:
        print("    download finished, but the output file was not found")
        return None

    target = unique_path(movie.folder / f"{movie.display_name}-trailer.mp4")
    converted = temp_dir / f"{trailer_stem}-normalized.mp4"
    print("    converting to MP4 and normalizing audio with FFmpeg")
    try:
        convert_to_normalized_mp4(
            downloaded,
            converted,
            threads=ffmpeg_threads,
            preset=ffmpeg_preset,
            crf=ffmpeg_crf,
        )
        shutil.move(str(converted), str(target))
    except Exception as exc:
        print(f"    FFmpeg conversion failed: {exc}")
        print("    saving the original downloaded file instead")
        target = unique_path(movie.folder / downloaded.name)
        shutil.move(str(downloaded), str(target))
    return target


def process_movie(movie: MovieFolder, args: argparse.Namespace, ydl_base_opts: dict, results: dict | None = None) -> bool:
    setattr(args, "_last_movie_used_network", False)
    print(f"\n== {movie.display_name} ==")
    stale_backups = delete_old_trailer_backups(movie, args.dry_run)
    for backup in stale_backups:
        action = "would delete stale backup" if args.dry_run else "deleted stale backup"
        print(f"  {action}: {backup.name}")

    existing_trailers = find_current_trailers(movie)
    redownload_existing = bool(getattr(args, "redownload_existing", False))
    skip_success_history = bool(getattr(args, "skip_success_history", True))
    success_record = successful_result_for_movie(movie, results or {}) if results is not None else None
    if success_record and skip_success_history and not redownload_existing:
        print(f"  skipped: previous success recorded ({success_record.get('trailer_name')})")
        return False
    if existing_trailers and not redownload_existing:
        if results is not None and not args.dry_run:
            record_existing_success(movie, existing_trailers[0], results)
        print(f"  skipped: existing trailer found ({existing_trailers[0].name})")
        return False

    setattr(args, "_last_movie_used_network", True)
    candidates = collect_candidates(
        movie=movie,
        search_results=args.search_results,
        max_duration=args.max_duration,
        include_vevo=args.include_vevo,
        ydl_base_opts=ydl_base_opts,
        search_delay=float(getattr(args, "search_delay", DEFAULT_SEARCH_DELAY)),
    )

    if not candidates:
        print("  no trailer candidates found")
        return False

    renamed: list[tuple[Path, Path]] = []
    if existing_trailers:
        renamed = rename_existing_trailers(movie, args.dry_run)
        for old, new in renamed:
            action = "would rename for re-download" if args.dry_run else "renamed for re-download"
            print(f"  {action}: {old.name} -> {new.name}")

    candidate_attempts = max(1, int(getattr(args, "candidate_attempts", DEFAULT_CANDIDATE_ATTEMPTS)))
    candidate_attempts = min(candidate_attempts, len(candidates))
    print(f"  found {len(candidates)} candidate(s); trying up to {candidate_attempts}, saving first success")
    downloaded = 0
    temp_dir = movie.folder / ".trailer-download-tmp"
    for index, candidate in enumerate(candidates[:candidate_attempts], start=1):
        try:
            target = download_candidate(
                movie,
                candidate,
                index,
                temp_dir,
                ydl_base_opts,
                args.dry_run,
                ffmpeg_threads=int(getattr(args, "ffmpeg_threads", DEFAULT_FFMPEG_THREADS)),
                ffmpeg_preset=str(getattr(args, "ffmpeg_preset", DEFAULT_FFMPEG_PRESET)),
                ffmpeg_crf=int(getattr(args, "ffmpeg_crf", DEFAULT_FFMPEG_CRF)),
            )
        except Exception as exc:
            if is_format_unavailable_error(exc):
                print(f"    skipped #{index}: no downloadable video format; trying next candidate")
                if not ydl_base_opts.get("js_runtimes"):
                    print("      no supported JavaScript runtime was detected; install Deno or Node.js 22+ for YouTube EJS challenges")
            else:
                print(f"    failed #{index}: {candidate.title} - {exc}")
            continue
        if target:
            downloaded += 1
            duration = f", {candidate.duration}s" if candidate.duration else ""
            print(f"    saved: {target.name} ({candidate.title}, score {candidate.score}{duration})")
            if not args.dry_run:
                if results is not None:
                    record_success(movie, target, candidate, results)
                removed_backups = delete_old_trailer_backups(movie, args.dry_run)
                for backup in removed_backups:
                    print(f"    deleted backup after successful download: {backup.name}")
            break

    if renamed and downloaded == 0 and not args.dry_run:
        restored = restore_renamed_trailers(renamed)
        for backup, original in restored:
            print(f"    restored previous trailer after failed re-download: {backup.name} -> {original.name}")

    if temp_dir.exists() and not args.keep_temp and not args.dry_run:
        shutil.rmtree(temp_dir, ignore_errors=True)

    if not args.dry_run:
        print(f"  downloaded {downloaded} trailer(s)")
    return downloaded > 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download best-quality free/public movie trailers into movie folders."
    )
    parser.add_argument("--gui", action="store_true", help="Open the graphical interface.")
    arguments("--check-deps", action="store_true", help="Check required external tools and Python packages, then exit.")
    parser.add_argument("--install-deps", action="store_true", help="Run the bundled Windows install.ps1 dependency bootstrapper, then exit.")

    parser.add_argument("--root", default=r"C:\movies", help=r"Movie library root. Default: C:\movies")
    parser.add_argument(
        "--max-per-movie",
        type=int,
        default=1,
        help="Kept for compatibility; the script now saves one trailer per movie.",
    )
    parser.add_argument("--search-results", type=int, default=10, help="YouTube results to inspect per query.")
    parser.add_argument("--max-duration", type=int, default=600, help="Ignore videos longer than this many seconds.")
    parser.add_argument("--candidate-attempts", type=int, default=DEFAULT_CANDIDATE_ATTEMPTS, help="Candidate videos to try per movie before giving up.")
    parser.add_argument("--search-delay", type=float, default=DEFAULT_SEARCH_DELAY, help="Seconds to wait between search queries.")
    parser.add_argument("--movie-delay", type=float, default=DEFAULT_MOVIE_DELAY, help="Seconds to wait between movie folders.")
    parser.add_argument(
        "--download-sleep-min",
        type=float,
        default=DEFAULT_DOWNLOAD_SLEEP_MIN,
        help="Minimum yt-dlp sleep before downloads.",
    )
    parser.add_argument(
        "--download-sleep-max",
        type=float,
        default=DEFAULT_DOWNLOAD_SLEEP_MAX,
        help="Maximum yt-dlp sleep before downloads.",
    )
    parser.add_argument(
        "--ffmpeg-threads",
        type=int,
        default=DEFAULT_FFMPEG_THREADS,
        help="Maximum FFmpeg threads for MP4 conversion. Lower values reduce CPU/RAM use.",
    )
    parser.add_argument(
        "--ffmpeg-preset",
        choices=FFMPEG_PRESETS,
        default=DEFAULT_FFMPEG_PRESET,
        help="FFmpeg x264 preset for MP4 conversion. Faster presets use less CPU.",
    )
    parser.add_argument(
        "--ffmpeg-crf",
        type=int,
        default=DEFAULT_FFMPEG_CRF,
        help="FFmpeg x264 CRF quality for MP4 conversion. Higher is smaller/lighter; 18-23 is typical.",
    )
    parser.add_argument(
        "--js-runtime",
        default=DEFAULT_JS_RUNTIME,
        help=r"yt-dlp JavaScript runtime for YouTube challenges, e.g. node, deno, or node:C:\path\to\node.exe.",
    )
    parser.add_argument(
        "--remote-components",
        default=DEFAULT_REMOTE_COMPONENTS,
        help="yt-dlp remote components, e.g. ejs:github. Use blank to disable.",
    )
    parser.add_argument("--include-vevo", action="store_true", help="Also search VEVO-flavoured queries.")
    parser.add_argument("--cookies-file", help="Optional Netscape cookies.txt file to use for yt-dlp.")
    parser.add_argument("--results-file", help="JSON file that records successful trailer results.")
    parser.add_argument("--cookies-from-browser", help="Optional yt-dlp browser cookies source, e.g. chrome or edge.")
    parser.add_argument(
        "--extract-cookies-from-browser",
        choices=("edge", "chrome", "chromium", "firefox", "brave", "vivaldi", "opera"),
        help="Extract browser cookies to --cookies-file, then exit.",
    )
    parser.add_argument("--limit", type=int, help="Only process the first N movie folders.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without renaming/downloading.")
    parser.add_argument(
        "--redownload-existing",
        action="store_true",
        help="Re-download trailers even when a current trailer already exists.",
    )
    parser.add_argument(
        "--ignore-success-history",
        action="store_true",
        help="Ignore the saved results file when deciding what to skip.",
    )
    parser.add_argument("--keep-temp", action="store_true", help="Keep per-movie temporary download folders.")
    return parser


def run_cli(args: argparse.Namespace) -> int:
    args.max_per_movie = 1
    cookies_file = Path(args.cookies_file).expanduser() if getattr(args, "cookies_file", None) else DEFAULT_COOKIES_PATH
    results_file = Path(args.results_file).expanduser() if getattr(args, "results_file", None) else DEFAULT_RESULTS_PATH
    args.skip_success_history = not bool(getattr(args, "ignore_success_history", False))

    if getattr(args, "extract_cookies_from_browser", None):
        try:
            extract_cookies_file(args.extract_cookies_from_browser, cookies_file)
        except Exception as exc:
            print(f"Cookie extraction failed: {exc}", file=sys.stderr)
            print("Try closing the browser first, or choose a different browser/profile.", file=sys.stderr)
            return 1
        return 0

    root = Path(args.root)

    if not root.exists() or not root.is_dir():
        print(f"Movie root does not exist or is not a directory: {root}", file=sys.stderr)
        return 1

    require_yt_dlp()

    ydl_base_opts: dict = {}
    if cookies_file.exists():
        ydl_base_opts["cookiefile"] = str(cookies_file)
    elif args.cookies_from_browser:
        ydl_base_opts["cookiesfrombrowser"] = (args.cookies_from_browser,)
    if getattr(args, "download_sleep_min", 0) > 0:
        ydl_base_opts["sleep_interval"] = float(args.download_sleep_min)
        ydl_base_opts["max_sleep_interval"] = max(
            float(getattr(args, "download_sleep_max", args.download_sleep_min)),
            float(args.download_sleep_min),
        )
    js_runtimes = merge_js_runtimes(
        parse_js_runtimes(getattr(args, "js_runtime", "")),
        parse_js_runtimes(DEFAULT_JS_RUNTIME),
    )
    if js_runtimes:
        ydl_base_opts["js_runtimes"] = js_runtimes
    if getattr(args, "remote_components", ""):
        ydl_base_opts["remote_components"] = parse_remote_components(args.remote_components)
    elif DEFAULT_REMOTE_COMPONENTS:
        ydl_base_opts["remote_components"] = parse_remote_components(DEFAULT_REMOTE_COMPONENTS)

    movies = list(iter_movie_folders(root))
    if args.limit:
        movies = movies[: args.limit]
    if not movies:
        print(f"No movie folders found in {root}")
        return 0

    print(f"Scanning {len(movies)} movie folder(s) under {root}")
    results = load_results(results_file)
    progress_callback = getattr(args, "progress_callback", None)
    if progress_callback:
        progress_callback(0, len(movies), "Starting")
    movie_delay = float(getattr(args, "movie_delay", DEFAULT_MOVIE_DELAY))
    for index, movie in enumerate(movies, start=1):
        changed = process_movie(movie, args, ydl_base_opts, results)
        if changed or not args.dry_run:
            save_results(results_file, results)
        if progress_callback:
            progress_callback(index, len(movies), movie.display_name)
        if index < len(movies) and movie_delay > 0 and getattr(args, "_last_movie_used_network", False):
            polite_sleep(movie_delay)

    return 0


def load_gui_settings() -> dict:
    defaults = {
        "root": r"C:\movies",
        "include_vevo": True,
        "cookies_file": str(DEFAULT_COOKIES_PATH),
        "results_file": str(DEFAULT_RESULTS_PATH),
        "extract_cookies_from_browser": "edge",
        "cookies_from_browser": "",
        "search_results": 10,
        "max_duration": 600,
        "candidate_attempts": DEFAULT_CANDIDATE_ATTEMPTS,
        "search_delay": DEFAULT_SEARCH_DELAY,
        "movie_delay": DEFAULT_MOVIE_DELAY,
        "download_sleep_min": DEFAULT_DOWNLOAD_SLEEP_MIN,
        "download_sleep_max": DEFAULT_DOWNLOAD_SLEEP_MAX,
        "ffmpeg_threads": DEFAULT_FFMPEG_THREADS,
        "ffmpeg_preset": DEFAULT_FFMPEG_PRESET,
        "ffmpeg_crf": DEFAULT_FFMPEG_CRF,
        "js_runtime": DEFAULT_JS_RUNTIME,
        "remote_components": DEFAULT_REMOTE_COMPONENTS,
        "limit": "",
        "redownload_existing": False,
        "skip_success_history": True,
        "keep_temp": False,
    }
    if not SETTINGS_PATH.exists():
        return defaults
    try:
        saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return defaults
    defaults.update({key: saved[key] for key in defaults.keys() & saved.keys()})
    return defaults


def save_gui_settings(settings: dict) -> None:
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")


class QueueWriter:
    def __init__(self, output_queue: queue.Queue[object]) -> None:
        self.output_queue = output_queue

    def write(self, text: str) -> int:
        if text:
            self.output_queue.put(text)
        return len(text)

    def flush(self) -> None:
        return None


def launch_gui() -> int:
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except ImportError:
        print("Tkinter is not available in this Python install.", file=sys.stderr)
        return 2

    palette = {
        "bg": "#0f172a",
        "surface": "#111827",
        "panel": "#172033",
        "panel_alt": "#0b1220",
        "border": "#26364f",
        "text": "#e5edf5",
        "muted": "#93a4b7",
        "accent": "#38bdf8",
        "accent_dark": "#0e7490",
        "good": "#22c55e",
        "warn": "#f59e0b",
        "danger": "#ef4444",
        "input": "#0b1220",
    }

    settings = load_gui_settings()
    root = tk.Tk()
    root.title("Trailer Ops Console")
    root.geometry("1040x720")
    root.minsize(900, 620)
    root.configure(bg=palette["bg"])

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure(".", font=("Segoe UI", 10), background=palette["bg"], foreground=palette["text"])
    style.configure("App.TFrame", background=palette["bg"])
    style.configure("Panel.TFrame", background=palette["panel"], relief="flat")
    style.configure("Subtle.TFrame", background=palette["surface"], relief="flat")
    style.configure("Row.TFrame", background=palette["panel"])
    style.configure("TLabel", background=palette["panel"], foreground=palette["text"])
    style.configure("Muted.TLabel", background=palette["panel"], foreground=palette["muted"])
    style.configure("Header.TLabel", background=palette["bg"], foreground=palette["text"], font=("Segoe UI Semibold", 20))
    style.configure("Subheader.TLabel", background=palette["bg"], foreground=palette["muted"], font=("Segoe UI", 10))
    style.configure("CardTitle.TLabel", background=palette["panel"], foreground=palette["text"], font=("Segoe UI Semibold", 11))
    style.configure("Value.TLabel", background=palette["panel"], foreground=palette["text"], font=("Segoe UI Semibold", 13))
    style.configure("Tiny.TLabel", background=palette["panel"], foreground=palette["muted"], font=("Segoe UI", 9))
    style.configure("TButton", padding=(12, 8), borderwidth=0)
    style.map("TButton", background=[("active", palette["border"])], foreground=[("disabled", palette["muted"])])
    style.configure("Accent.TButton", background=palette["accent_dark"], foreground="#ecfeff", padding=(14, 9))
    style.map("Accent.TButton", background=[("active", "#0891b2"), ("disabled", palette["border"])])
    style.configure("Danger.TButton", background="#7f1d1d", foreground="#fee2e2", padding=(12, 8))
    style.map("Danger.TButton", background=[("active", "#991b1b")])
    style.configure("TEntry", fieldbackground=palette["input"], foreground=palette["text"], bordercolor=palette["border"])
    style.configure("TCombobox", fieldbackground=palette["input"], foreground=palette["text"], bordercolor=palette["border"])
    style.configure(
        "Horizontal.TProgressbar",
        background=palette["accent"],
        troughcolor=palette["panel_alt"],
        bordercolor=palette["panel_alt"],
        lightcolor=palette["accent"],
        darkcolor=palette["accent_dark"],
    )
    style.configure("TCheckbutton", background=palette["panel"], foreground=palette["text"])
    style.map("TCheckbutton", background=[("active", palette["panel"])])
    style.configure("TNotebook", background=palette["bg"], borderwidth=0)
    style.configure("TNotebook.Tab", background=palette["surface"], foreground=palette["muted"], padding=(18, 9))
    style.map(
        "TNotebook.Tab",
        background=[("selected", palette["panel"]), ("active", palette["border"])],
        foreground=[("selected", palette["text"]), ("active", palette["text"])],
    )

    root_var = tk.StringVar(value=str(settings["root"]))
    include_vevo_var = tk.BooleanVar(value=bool(settings["include_vevo"]))
    cookies_file_var = tk.StringVar(value=str(settings["cookies_file"]))
    results_file_var = tk.StringVar(value=str(settings["results_file"]))
    extract_browser_var = tk.StringVar(value=str(settings["extract_cookies_from_browser"]))
    cookies_var = tk.StringVar(value=str(settings["cookies_from_browser"]))
    search_results_var = tk.StringVar(value=str(settings["search_results"]))
    max_duration_var = tk.StringVar(value=str(settings["max_duration"]))
    candidate_attempts_var = tk.StringVar(value=str(settings["candidate_attempts"]))
    search_delay_var = tk.StringVar(value=str(settings["search_delay"]))
    movie_delay_var = tk.StringVar(value=str(settings["movie_delay"]))
    download_sleep_min_var = tk.StringVar(value=str(settings["download_sleep_min"]))
    download_sleep_max_var = tk.StringVar(value=str(settings["download_sleep_max"]))
    ffmpeg_threads_var = tk.StringVar(value=str(settings["ffmpeg_threads"]))
    ffmpeg_preset_var = tk.StringVar(value=str(settings["ffmpeg_preset"]))
    ffmpeg_crf_var = tk.StringVar(value=str(settings["ffmpeg_crf"]))
    js_runtime_var = tk.StringVar(value=str(settings["js_runtime"]))
    remote_components_var = tk.StringVar(value=str(settings["remote_components"]))
    limit_var = tk.StringVar(value=str(settings["limit"]))
    redownload_existing_var = tk.BooleanVar(value=bool(settings["redownload_existing"]))
    skip_success_history_var = tk.BooleanVar(value=bool(settings["skip_success_history"]))
    keep_temp_var = tk.BooleanVar(value=bool(settings["keep_temp"]))
    status_var = tk.StringVar(value="Ready")
    progress_var = tk.DoubleVar(value=0)
    progress_text_var = tk.StringVar(value="Idle")
    root_summary_var = tk.StringVar(value=root_var.get())
    cookie_summary_var = tk.StringVar(value="cookies file" if Path(cookies_file_var.get()).exists() else "public search")
    mode_summary_var = tk.StringVar(value="skip existing trailers")
    running_var = tk.BooleanVar(value=False)
    log_queue: queue.Queue[object] = queue.Queue()
    busy_buttons: list[ttk.Button] = []

    def panel(parent, **grid_options):
        frame = ttk.Frame(parent, style="Panel.TFrame", padding=14)
        frame.grid(**grid_options)
        return frame

    def card_title(parent, title: str, subtitle: str | None = None) -> None:
        ttk.Label(parent, text=title, style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        if subtitle:
            ttk.Label(parent, text=subtitle, style="Tiny.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 10))

    def labeled_entry(parent, row: int, label: str, variable: tk.Variable, browse_command=None) -> None:
        ttk.Label(parent, text=label, style="Muted.TLabel").grid(row=row, column=0, sticky="w", pady=6, padx=(0, 12))
        holder = ttk.Frame(parent, style="Row.TFrame")
        holder.grid(row=row, column=1, sticky="ew", pady=6)
        holder.columnconfigure(0, weight=1)
        ttk.Entry(holder, textvariable=variable).grid(row=0, column=0, sticky="ew")
        if browse_command:
            ttk.Button(holder, text="Browse", command=browse_command).grid(row=0, column=1, padx=(8, 0))

    shell = ttk.Frame(root, style="App.TFrame", padding=18)
    shell.pack(fill="both", expand=True)
    shell.columnconfigure(0, weight=1)
    shell.rowconfigure(2, weight=1)

    header = ttk.Frame(shell, style="App.TFrame")
    header.grid(row=0, column=0, sticky="ew")
    header.columnconfigure(0, weight=1)
    ttk.Label(header, text="Trailer Ops Console", style="Header.TLabel").grid(row=0, column=0, sticky="w")
    ttk.Label(
        header,
        text="Search public trailer sources, preserve existing trailers, and keep one clean file per movie.",
        style="Subheader.TLabel",
    ).grid(row=1, column=0, sticky="w", pady=(2, 0))

    status_badge = tk.Label(
        header,
        textvariable=status_var,
        bg=palette["accent_dark"],
        fg="#ecfeff",
        padx=14,
        pady=7,
        font=("Segoe UI Semibold", 10),
    )
    status_badge.grid(row=0, column=1, rowspan=2, sticky="e")

    summary = ttk.Frame(shell, style="App.TFrame")
    summary.grid(row=1, column=0, sticky="ew", pady=(16, 14))
    summary.columnconfigure((0, 1, 2), weight=1)

    def summary_tile(column: int, label: str, value_var: tk.StringVar, accent: str) -> None:
        tile = ttk.Frame(summary, style="Panel.TFrame", padding=12)
        tile.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 8, 0 if column == 2 else 8))
        tile.columnconfigure(1, weight=1)
        marker = tk.Frame(tile, width=4, height=42, bg=accent, highlightthickness=0)
        marker.grid(row=0, column=0, rowspan=2, sticky="nsw", padx=(0, 10))
        ttk.Label(tile, text=label.upper(), style="Tiny.TLabel").grid(row=0, column=1, sticky="w")
        ttk.Label(tile, textvariable=value_var, style="Value.TLabel").grid(row=1, column=1, sticky="w")

    summary_tile(0, "Library", root_summary_var, palette["accent"])
    summary_tile(1, "Cookies", cookie_summary_var, palette["good"])
    summary_tile(2, "Mode", mode_summary_var, palette["warn"])

    notebook = ttk.Notebook(shell)
    notebook.grid(row=2, column=0, sticky="nsew")

    run_tab = ttk.Frame(notebook, padding=12)
    settings_tab = ttk.Frame(notebook, padding=12)
    notebook.add(run_tab, text="Run")
    notebook.add(settings_tab, text="Settings")

    run_tab.columnconfigure(0, weight=1)
    run_tab.rowconfigure(2, weight=1)

    settings_tab.columnconfigure(0, weight=1)
    settings_tab.rowconfigure(0, weight=1)
    settings_canvas = tk.Canvas(
        settings_tab,
        bg=palette["bg"],
        highlightthickness=0,
        borderwidth=0,
        yscrollincrement=24,
    )
    settings_scrollbar = ttk.Scrollbar(settings_tab, orient="vertical", command=settings_canvas.yview)
    settings_canvas.configure(yscrollcommand=settings_scrollbar.set)
    settings_canvas.grid(row=0, column=0, sticky="nsew")
    settings_scrollbar.grid(row=0, column=1, sticky="ns")

    settings_body = ttk.Frame(settings_canvas, style="App.TFrame")
    settings_window = settings_canvas.create_window((0, 0), window=settings_body, anchor="nw")
    settings_body.columnconfigure((0, 1), weight=1, uniform="settings")
    settings_body.rowconfigure(1, weight=1)

    def refresh_settings_scroll_region(_event=None) -> None:
        settings_canvas.configure(scrollregion=settings_canvas.bbox("all"))

    def resize_settings_body(event) -> None:
        settings_canvas.itemconfigure(settings_window, width=event.width)

    def scroll_settings(event) -> str:
        if event.delta:
            settings_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        return "break"

    def bind_settings_wheel(_event) -> None:
        settings_canvas.bind_all("<MouseWheel>", scroll_settings)

    def unbind_settings_wheel(_event) -> None:
        settings_canvas.unbind_all("<MouseWheel>")

    settings_body.bind("<Configure>", refresh_settings_scroll_region)
    settings_canvas.bind("<Configure>", resize_settings_body)
    settings_canvas.bind("<Enter>", bind_settings_wheel)
    settings_canvas.bind("<Leave>", unbind_settings_wheel)

    def browse_root() -> None:
        selected = filedialog.askdirectory(initialdir=root_var.get() or r"C:\movies")
        if selected:
            root_var.set(selected)
            root_summary_var.set(selected)

    library_panel = panel(run_tab, row=0, column=0, sticky="ew")
    library_panel.columnconfigure(1, weight=1)
    card_title(library_panel, "Library target", "Folder names should look like Movie (2026).")
    labeled_entry(library_panel, 2, "Movie folder", root_var, browse_root)

    actions = ttk.Frame(run_tab, style="Panel.TFrame", padding=14)
    actions.grid(row=1, column=0, sticky="ew", pady=(12, 8))
    actions.columnconfigure(3, weight=1)
    ttk.Label(actions, text="Run controls", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 10))

    log_panel = ttk.Frame(run_tab, style="Panel.TFrame", padding=14)
    log_panel.grid(row=2, column=0, sticky="nsew")
    log_panel.columnconfigure(0, weight=1)
    log_panel.rowconfigure(1, weight=1)
    ttk.Label(log_panel, text="Activity log", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 10))
    log_text = tk.Text(
        log_panel,
        wrap="word",
        height=20,
        state="disabled",
        bg=palette["panel_alt"],
        fg=palette["text"],
        insertbackground=palette["text"],
        selectbackground=palette["accent_dark"],
        relief="flat",
        padx=12,
        pady=12,
        font=("Consolas", 10),
    )
    log_text.grid(row=1, column=0, sticky="nsew")
    scrollbar = ttk.Scrollbar(log_panel, orient="vertical", command=log_text.yview)
    scrollbar.grid(row=1, column=1, sticky="ns")
    log_text.configure(yscrollcommand=scrollbar.set)

    status = ttk.Label(run_tab, textvariable=status_var, style="Muted.TLabel", anchor="w")
    status.grid(row=3, column=0, sticky="ew", pady=(8, 0))

    def append_log(text: str) -> None:
        log_text.configure(state="normal")
        log_text.insert("end", text)
        log_text.see("end")
        log_text.configure(state="disabled")

    def update_summary() -> None:
        root_summary_var.set(root_var.get().strip() or r"C:\movies")
        cookie_path = Path(cookies_file_var.get().strip() or str(DEFAULT_COOKIES_PATH))
        if cookie_path.exists():
            cookie_summary_var.set(f"file: {cookie_path.name}")
        elif cookies_var.get().strip():
            cookie_summary_var.set(f"browser: {cookies_var.get().strip()}")
        else:
            cookie_summary_var.set("public search")
        mode_summary_var.set("total re-download" if redownload_existing_var.get() else "skip existing trailers")

    def set_status(text: str, tone: str = "accent") -> None:
        status_var.set(text)
        status_badge.configure(bg=palette.get(tone, palette["accent_dark"]))

    def parse_int_field(value: str, field_name: str, minimum: int, maximum: int) -> int:
        text = value.strip()
        if not text:
            raise ValueError(f"{field_name} cannot be blank.")
        try:
            number = int(text)
        except ValueError:
            raise ValueError(f"{field_name} must be a whole number.") from None
        if number < minimum or number > maximum:
            raise ValueError(f"{field_name} must be between {minimum} and {maximum}.")
        return number

    def parse_float_field(value: str, field_name: str, minimum: float, maximum: float) -> float:
        text = value.strip()
        if not text:
            raise ValueError(f"{field_name} cannot be blank.")
        try:
            number = float(text)
        except ValueError:
            raise ValueError(f"{field_name} must be a number.") from None
        if number < minimum or number > maximum:
            raise ValueError(f"{field_name} must be between {minimum:g} and {maximum:g}.")
        return number

    def read_settings_from_gui() -> dict:
        limit_text = limit_var.get().strip()
        limit = parse_int_field(limit_text, "Process first N folders", 1, 1_000_000) if limit_text else None
        download_sleep_min = parse_float_field(download_sleep_min_var.get(), "Download sleep minimum", 0, 120)
        download_sleep_max = parse_float_field(download_sleep_max_var.get(), "Download sleep maximum", 0, 300)
        if download_sleep_max < download_sleep_min:
            raise ValueError("Download sleep maximum must be greater than or equal to the minimum.")
        ffmpeg_preset = ffmpeg_preset_var.get().strip() or DEFAULT_FFMPEG_PRESET
        if ffmpeg_preset not in FFMPEG_PRESETS:
            raise ValueError(f"FFmpeg preset must be one of: {', '.join(FFMPEG_PRESETS)}.")
        return {
            "root": root_var.get().strip() or r"C:\movies",
            "max_per_movie": 1,
            "search_results": parse_int_field(search_results_var.get(), "Search results per query", 1, 50),
            "max_duration": parse_int_field(max_duration_var.get(), "Maximum trailer length", 30, 3600),
            "candidate_attempts": parse_int_field(candidate_attempts_var.get(), "Candidate attempts", 1, 25),
            "search_delay": parse_float_field(search_delay_var.get(), "Search delay", 0, 120),
            "movie_delay": parse_float_field(movie_delay_var.get(), "Movie delay", 0, 300),
            "download_sleep_min": download_sleep_min,
            "download_sleep_max": download_sleep_max,
            "ffmpeg_threads": parse_int_field(ffmpeg_threads_var.get(), "FFmpeg threads", 1, 16),
            "ffmpeg_preset": ffmpeg_preset,
            "ffmpeg_crf": parse_int_field(ffmpeg_crf_var.get(), "FFmpeg CRF", 16, 30),
            "js_runtime": js_runtime_var.get().strip(),
            "remote_components": remote_components_var.get().strip(),
            "include_vevo": bool(include_vevo_var.get()),
            "cookies_file": cookies_file_var.get().strip() or str(DEFAULT_COOKIES_PATH),
            "results_file": results_file_var.get().strip() or str(DEFAULT_RESULTS_PATH),
            "cookies_from_browser": cookies_var.get().strip() or None,
            "limit": limit,
            "redownload_existing": bool(redownload_existing_var.get()),
            "ignore_success_history": not bool(skip_success_history_var.get()),
            "keep_temp": bool(keep_temp_var.get()),
        }

    def persist_current_settings() -> None:
        current = read_settings_from_gui()
        save_gui_settings(
            {
                "root": current["root"],
                "include_vevo": current["include_vevo"],
                "cookies_file": current["cookies_file"],
                "results_file": current["results_file"],
                "extract_cookies_from_browser": extract_browser_var.get().strip() or "edge",
                "cookies_from_browser": current["cookies_from_browser"] or "",
                "search_results": current["search_results"],
                "max_duration": current["max_duration"],
                "candidate_attempts": current["candidate_attempts"],
                "search_delay": current["search_delay"],
                "movie_delay": current["movie_delay"],
                "download_sleep_min": current["download_sleep_min"],
                "download_sleep_max": current["download_sleep_max"],
                "ffmpeg_threads": current["ffmpeg_threads"],
                "ffmpeg_preset": current["ffmpeg_preset"],
                "ffmpeg_crf": current["ffmpeg_crf"],
                "js_runtime": current["js_runtime"],
                "remote_components": current["remote_components"],
                "limit": "" if current["limit"] is None else str(current["limit"]),
                "redownload_existing": current["redownload_existing"],
                "skip_success_history": not current["ignore_success_history"],
                "keep_temp": current["keep_temp"],
            }
        )
        update_summary()

    def drain_log_queue() -> None:
        while True:
            try:
                item = log_queue.get_nowait()
            except queue.Empty:
                break
            if item == "__DONE__":
                running_var.set(False)
                for button in busy_buttons:
                    button.configure(state="normal")
                progress_var.set(100)
                set_status("Finished", "good")
                update_summary()
            elif isinstance(item, tuple) and len(item) == 4 and item[0] == "__PROGRESS__":
                _kind, current, total, label = item
                percent = 0 if not total else (float(current) / float(total)) * 100
                progress_var.set(percent)
                progress_text_var.set(f"{current} / {total} - {label}")
            else:
                append_log(item)
        root.after(100, drain_log_queue)

    def run_worker(dry_run_override: bool | None = None) -> None:
        if running_var.get():
            return
        try:
            current = read_settings_from_gui()
            if dry_run_override is not None:
                current["dry_run"] = dry_run_override
            else:
                current["dry_run"] = False
            persist_current_settings()
            current["progress_callback"] = lambda current_count, total_count, label: log_queue.put(
                ("__PROGRESS__", current_count, total_count, label)
            )
        except Exception as exc:
            messagebox.showerror("Invalid settings", str(exc))
            return

        log_text.configure(state="normal")
        log_text.delete("1.0", "end")
        log_text.configure(state="disabled")
        progress_var.set(0)
        progress_text_var.set("Preparing")
        set_status("Running", "accent_dark")
        running_var.set(True)
        for button in busy_buttons:
            button.configure(state="disabled")

        def target() -> None:
            old_stdout, old_stderr = sys.stdout, sys.stderr
            writer = QueueWriter(log_queue)
            sys.stdout = writer
            sys.stderr = writer
            try:
                args = argparse.Namespace(**current)
                run_cli(args)
            except SystemExit as exc:
                print(f"\nStopped with exit code {exc.code}")
            except Exception as exc:
                print(f"\nError: {exc}")
            finally:
                sys.stdout = old_stdout
                sys.stderr = old_stderr
                log_queue.put("__DONE__")

        threading.Thread(target=target, daemon=True).start()

    def extract_worker() -> None:
        if running_var.get():
            return
        browser = extract_browser_var.get().strip()
        if not browser:
            messagebox.showerror("Missing browser", "Choose a browser to extract cookies from.")
            return
        try:
            persist_current_settings()
            cookies_file = Path(cookies_file_var.get().strip() or str(DEFAULT_COOKIES_PATH))
        except Exception as exc:
            messagebox.showerror("Invalid settings", str(exc))
            return

        notebook.select(run_tab)
        log_text.configure(state="normal")
        log_text.delete("1.0", "end")
        log_text.configure(state="disabled")
        progress_var.set(0)
        progress_text_var.set("Extracting cookies")
        set_status("Extracting cookies", "warn")
        running_var.set(True)
        for button in busy_buttons:
            button.configure(state="disabled")

        def target() -> None:
            old_stdout, old_stderr = sys.stdout, sys.stderr
            writer = QueueWriter(log_queue)
            sys.stdout = writer
            sys.stderr = writer
            try:
                extract_cookies_file(browser, cookies_file)
            except Exception as exc:
                print(f"\nCookie extraction failed: {exc}")
                print("Try closing the browser first, or choose a different browser/profile.")
            finally:
                sys.stdout = old_stdout
                sys.stderr = old_stderr
                log_queue.put("__DONE__")

        threading.Thread(target=target, daemon=True).start()


    def dependency_worker() -> None:
        if running_var.get():
            return
        missing = missing_dependency_names()
        status_lines = [f"{'OK' if ok else 'MISSING'} - {name}: {detail}" for name, ok, detail in dependency_status()]
        if not missing:
            messagebox.showinfo("Dependencies", "All required dependencies look ready.\n\n" + "\n".join(status_lines))
            return
        if not INSTALLER_PATH.exists():
            messagebox.showerror("Installer missing", f"Could not find {INSTALLER_PATH}")
            return
        answer = messagebox.askyesno(
            "Install dependencies",
            "Missing dependencies were found:\n\n"
            + "\n".join(status_lines)
            + "\n\nRun install.ps1 now? Windows may ask for administrator approval.",
        )
        if not answer:
            return

        notebook.select(run_tab)
        log_text.configure(state="normal")
        log_text.delete("1.0", "end")
        log_text.configure(state="disabled")
        progress_var.set(0)
        progress_text_var.set("Installing dependencies")
        set_status("Installing dependencies", "warn")
        running_var.set(True)
        for button in busy_buttons:
            button.configure(state="disabled")

        def target() -> None:
            old_stdout, old_stderr = sys.stdout, sys.stderr
            writer = QueueWriter(log_queue)
            sys.stdout = writer
            sys.stderr = writer
            try:
                print_dependency_status()
                print("\nStarting dependency installer...")
                exit_code = install_dependencies()
                print(f"\nInstaller finished with exit code {exit_code}")
                print("\nUpdated dependency status:")
                print_dependency_status()
            except Exception as exc:
                print(f"\nDependency install failed: {exc}")
            finally:
                sys.stdout = old_stdout
                sys.stderr = old_stderr
                log_queue.put("__DONE__")

        threading.Thread(target=target, daemon=True).start()

    preview_button = ttk.Button(actions, text="Preview", command=lambda: run_worker(True))
    preview_button.grid(row=1, column=0, sticky="w")
    start_button = ttk.Button(actions, text="Download", style="Accent.TButton", command=lambda: run_worker(False))
    start_button.grid(row=1, column=1, sticky="w", padx=(8, 0))

    busy_buttons.extend([preview_button, start_button])
    deps_button = ttk.Button(actions, text="Install / Repair Dependencies", command=dependency_worker)
    deps_button.grid(row=1, column=2, sticky="w", padx=(8, 0))
    busy_buttons.extend([preview_button, start_button, deps_button])
    ttk.Label(actions, text="Default skips existing trailers; total re-download is in Settings.", style="Muted.TLabel").grid(
        row=1, column=3, sticky="e"
    )
    progress_bar = ttk.Progressbar(actions, variable=progress_var, mode="determinate", maximum=100)
    progress_bar.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(14, 4))
    ttk.Label(actions, textvariable=progress_text_var, style="Muted.TLabel").grid(
        row=3, column=0, columnspan=4, sticky="ew"
    )

    search_panel = panel(settings_body, row=0, column=0, sticky="nsew", padx=(0, 8), pady=(0, 12))
    search_panel.columnconfigure(1, weight=1)
    card_title(search_panel, "Search profile", "Tune source breadth and candidate filtering.")
    ttk.Checkbutton(search_panel, text="Include VEVO searches", variable=include_vevo_var, command=update_summary).grid(
        row=2, column=0, columnspan=2, sticky="w", pady=(2, 8)
    )
    ttk.Label(search_panel, text="Search results per query", style="Muted.TLabel").grid(row=3, column=0, sticky="w", pady=6)
    ttk.Entry(search_panel, textvariable=search_results_var).grid(row=3, column=1, sticky="ew", pady=6)
    ttk.Label(search_panel, text="Maximum trailer length", style="Muted.TLabel").grid(row=4, column=0, sticky="w", pady=6)
    ttk.Entry(search_panel, textvariable=max_duration_var).grid(row=4, column=1, sticky="ew", pady=6)
    ttk.Label(search_panel, text="Candidate attempts", style="Muted.TLabel").grid(row=5, column=0, sticky="w", pady=6)
    ttk.Entry(search_panel, textvariable=candidate_attempts_var).grid(row=5, column=1, sticky="ew", pady=6)
    ttk.Label(search_panel, text="Delay between searches (seconds)", style="Muted.TLabel").grid(row=6, column=0, sticky="w", pady=6)
    ttk.Entry(search_panel, textvariable=search_delay_var).grid(row=6, column=1, sticky="ew", pady=6)
    ttk.Label(search_panel, text="Download sleep min (seconds)", style="Muted.TLabel").grid(row=7, column=0, sticky="w", pady=6)
    ttk.Entry(search_panel, textvariable=download_sleep_min_var).grid(row=7, column=1, sticky="ew", pady=6)
    ttk.Label(search_panel, text="Download sleep max (seconds)", style="Muted.TLabel").grid(row=8, column=0, sticky="w", pady=6)
    ttk.Entry(search_panel, textvariable=download_sleep_max_var).grid(row=8, column=1, sticky="ew", pady=6)

    runtime_panel = panel(settings_body, row=1, column=0, sticky="nsew", padx=(0, 8))
    runtime_panel.columnconfigure(1, weight=1)
    card_title(runtime_panel, "Runtime", "Keep test runs short while tuning searches.")
    ttk.Checkbutton(runtime_panel, text="Keep temporary folders", variable=keep_temp_var).grid(
        row=2, column=0, columnspan=2, sticky="w", pady=(2, 8)
    )
    ttk.Checkbutton(
        runtime_panel,
        text="Use saved success history",
        variable=skip_success_history_var,
        command=update_summary,
    ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(2, 8))
    ttk.Checkbutton(
        runtime_panel,
        text="Total re-download existing trailers",
        variable=redownload_existing_var,
        command=update_summary,
    ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(2, 8))
    ttk.Label(runtime_panel, text="Process first N folders", style="Muted.TLabel").grid(row=5, column=0, sticky="w", pady=6)
    ttk.Entry(runtime_panel, textvariable=limit_var).grid(row=5, column=1, sticky="ew", pady=6)
    ttk.Label(runtime_panel, text="Delay between movies (seconds)", style="Muted.TLabel").grid(row=6, column=0, sticky="w", pady=6)
    ttk.Entry(runtime_panel, textvariable=movie_delay_var).grid(row=6, column=1, sticky="ew", pady=6)
    ttk.Label(runtime_panel, text="FFmpeg threads", style="Muted.TLabel").grid(row=7, column=0, sticky="w", pady=6)
    ttk.Entry(runtime_panel, textvariable=ffmpeg_threads_var).grid(row=7, column=1, sticky="ew", pady=6)
    ttk.Label(runtime_panel, text="FFmpeg preset", style="Muted.TLabel").grid(row=8, column=0, sticky="w", pady=6)
    ttk.Combobox(runtime_panel, textvariable=ffmpeg_preset_var, values=FFMPEG_PRESETS).grid(row=8, column=1, sticky="ew", pady=6)
    ttk.Label(runtime_panel, text="FFmpeg CRF", style="Muted.TLabel").grid(row=9, column=0, sticky="w", pady=6)
    ttk.Entry(runtime_panel, textvariable=ffmpeg_crf_var).grid(row=9, column=1, sticky="ew", pady=6)

    def browse_results_file() -> None:
        selected = filedialog.asksaveasfilename(
            initialfile=Path(results_file_var.get() or str(DEFAULT_RESULTS_PATH)).name,
            initialdir=str(Path(results_file_var.get() or str(DEFAULT_RESULTS_PATH)).parent),
            defaultextension=".json",
            filetypes=(("JSON files", "*.json"), ("All files", "*.*")),
        )
        if selected:
            results_file_var.set(selected)

    labeled_entry(runtime_panel, 10, "Results file", results_file_var, browse_results_file)

    cookies_panel = panel(settings_body, row=0, column=1, rowspan=2, sticky="nsew", padx=(8, 0))
    cookies_panel.columnconfigure(1, weight=1)
    card_title(cookies_panel, "Cookies", "Prefer a generated cookies.txt file over live browser extraction.")

    def browse_cookies_file() -> None:
        selected = filedialog.asksaveasfilename(
            initialfile=Path(cookies_file_var.get() or str(DEFAULT_COOKIES_PATH)).name,
            initialdir=str(Path(cookies_file_var.get() or str(DEFAULT_COOKIES_PATH)).parent),
            defaultextension=".txt",
            filetypes=(("Cookies files", "*.txt"), ("All files", "*.*")),
        )
        if selected:
            cookies_file_var.set(selected)
            update_summary()

    labeled_entry(cookies_panel, 2, "Cookies file", cookies_file_var, browse_cookies_file)

    ttk.Label(cookies_panel, text="Extract from", style="Muted.TLabel").grid(row=3, column=0, sticky="w", pady=6, padx=(0, 12))
    extract_row = ttk.Frame(cookies_panel, style="Row.TFrame")
    extract_row.grid(row=3, column=1, sticky="ew", pady=6)
    extract_row.columnconfigure(0, weight=1)
    extract_combo = ttk.Combobox(
        extract_row,
        textvariable=extract_browser_var,
        values=("edge", "chrome", "chromium", "firefox", "brave", "vivaldi", "opera"),
    )
    extract_combo.grid(row=0, column=0, sticky="ew", padx=(0, 8))
    extract_button = ttk.Button(extract_row, text="Extract Cookies", command=extract_worker)
    extract_button.grid(row=0, column=1)
    busy_buttons.append(extract_button)

    ttk.Label(cookies_panel, text="Direct fallback", style="Muted.TLabel").grid(row=4, column=0, sticky="w", pady=6)
    cookies_combo = ttk.Combobox(cookies_panel, textvariable=cookies_var, values=("", "edge", "chrome", "firefox"))
    cookies_combo.grid(row=4, column=1, sticky="ew", pady=6)
    cookies_combo.bind("<<ComboboxSelected>>", lambda _event: update_summary())

    ttk.Label(cookies_panel, text="JS runtime", style="Muted.TLabel").grid(row=5, column=0, sticky="w", pady=6)
    js_runtime_combo = ttk.Combobox(cookies_panel, textvariable=js_runtime_var, values=("", "node", "deno", "quickjs"))
    js_runtime_combo.grid(row=5, column=1, sticky="ew", pady=6)

    ttk.Label(cookies_panel, text="Remote EJS components", style="Muted.TLabel").grid(row=6, column=0, sticky="w", pady=6)
    ttk.Entry(cookies_panel, textvariable=remote_components_var).grid(row=6, column=1, sticky="ew", pady=6)

    ttk.Label(cookies_panel, text="Settings file", style="Muted.TLabel").grid(row=7, column=0, sticky="w", pady=(18, 4))
    ttk.Label(cookies_panel, text=str(SETTINGS_PATH), style="Tiny.TLabel", wraplength=430).grid(
        row=7, column=1, sticky="w", pady=(18, 4)
    )

    ttk.Button(cookies_panel, text="Save Settings", command=persist_current_settings).grid(
        row=8, column=1, sticky="e", pady=(14, 0)
    )

    update_summary()
    drain_log_queue()
    root.mainloop()
    return 0


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    if args.check_deps:
        print_dependency_status()
        return 1 if missing_dependency_names() else 0
    if args.install_deps:
        return install_dependencies()

    if args.gui or len(sys.argv) == 1:
        return launch_gui()
    return run_cli(args)


if __name__ == "__main__":
    raise SystemExit(main())
