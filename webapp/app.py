"""LDDC headless web service: scan local LRC files and fetch verbatim lyrics."""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from LDDC.common.models import Artist, LyricsType, SearchType, Source, SongInfo
from LDDC.core.api.lyrics import get_lyrics, search
from LDDC.core.parser.lrc import lrc2data
from LDDC.core.parser.utils import judge_lyrics_type
from LDDC.core.algorithm import calculate_artist_score, calculate_title_score, text_difference

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".mp4", ".ogg", ".opus", ".wav", ".aac", ".wma", ".ape"}
ROOT = Path(os.getenv("MUSIC_ROOT", "/music")).resolve()
STATE = Path(os.getenv("STATE_DIR", "/data")).resolve()
STATE.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="LDDC Docker", version="1.0.0")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
lock = threading.Lock()
last_run: dict[str, Any] = {"status": "idle", "scanned": 0, "updated": 0, "errors": [], "at": None}
DEFAULT_SETTINGS = {"path": "/music", "schedule_enabled": True, "interval_minutes": 360, "overwrite": False, "sources": ["QM", "KG", "NE", "LRCLIB"], "min_score": 55, "duration_filter": True}
settings_file = STATE / "settings.json"

def load_settings() -> dict[str, Any]:
    try:
        saved = json.loads(settings_file.read_text(encoding="utf-8"))
        return {**DEFAULT_SETTINGS, **saved}
    except Exception:
        return DEFAULT_SETTINGS.copy()

def save_settings(value: dict[str, Any]) -> dict[str, Any]:
    result = {**DEFAULT_SETTINGS, **value}
    settings_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result

app_settings = load_settings()

class ScanRequest(BaseModel):
    path: str | None = None
    overwrite: bool | None = None
    sources: list[str] | None = None
    min_score: int | None = None
    duration_filter: bool | None = None


def is_verbatim(text: str) -> bool:
    """A line is verbatim when it contains at least two timed word segments."""
    _tags, data = lrc2data(text)
    return judge_lyrics_type(data) == LyricsType.VERBATIM


def read_text(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def fallback_info(audio: Path) -> SongInfo:
    name = audio.stem
    parts = [part.strip() for part in re.split(r"\s+-\s+", name) if part.strip()]
    title, artist, album = name, None, None
    if len(parts) >= 4 and parts[2].lower() in {"master", "hi-res", "hires", "lossless", "flac", "web"}:
        # Common NAS naming: title - artist - quality - album
        title, artist, album = parts[0], parts[1], " - ".join(parts[3:])
    elif len(parts) >= 3:
        # Prefer title - artist - album, but still support artist - title.
        title, artist, album = parts[0], parts[1], " - ".join(parts[2:])
    elif len(parts) == 2:
        left, right = parts
        title, artist = left, right
    return SongInfo(source=Source.Local, title=title, artist=Artist(artist) if artist else None, album=album, path=audio)


def song_info(audio: Path) -> SongInfo:
    """Read common audio tags without importing the Qt-bound desktop module."""
    try:
        from mutagen import File
        media = File(audio, easy=True)
        if media is None or media.info is None:
            return fallback_info(audio)
        def first(key: str) -> str | None:
            value = media.get(key)
            return str(value[0]) if value else None
        fallback = fallback_info(audio)
        return SongInfo(
            source=Source.Local,
            title=first("title") or fallback.title,
            artist=Artist(first("artist")) if first("artist") else fallback.artist,
            album=first("album") or fallback.album,
            duration=round(media.info.length * 1000),
            path=audio,
        )
    except Exception:
        return fallback_info(audio)


def timestamp(ms: int | None) -> str:
    value = max(ms or 0, 0)
    return f"{value // 60000:02}:{value % 60000 // 1000:02}.{value % 1000:03}"


def verbatim_lrc(lyrics: Any) -> str:
    """Serialize LDDC's timed words without importing its Qt-bound config layer."""
    tags = "\n".join(f"[{key}:{value}]" for key, value in lyrics.tags.items() if key in {"al", "ar", "au", "by", "offset", "ti"} and value)
    lines: list[str] = []
    for line in lyrics["orig"]:
        start = line.words[0].start if line.words and line.words[0].start is not None else line.start
        output = f"[{timestamp(start)}]"
        last_end = start
        for word in line.words:
            if word.start is not None and word.start != last_end:
                output += f"[{timestamp(word.start)}]"
            output += word.text
            if word.end is not None:
                output += f"[{timestamp(word.end)}]"
            last_end = word.end
        if line.end is not None and not output.endswith("]"):
            output += f"[{timestamp(line.end)}]"
        lines.append(output)
    header = (tags + "\n") if tags else ""
    return header + "[tool:LDDC Docker]\n\n" + "\n".join(lines) + "\n"


def choose_lyrics(info: SongInfo, names: list[str], min_score: int = 55, duration_filter: bool = True) -> Any:
    """Match like the original auto_fetch: multi-source search, scoring and fallback."""
    title = (info.title or "").strip()
    artist_title = info.artist_title() if title and info.artist else title
    filename = info.path.stem if info.path else title
    queries = [query for query in (artist_title, title, filename) if query]
    sources = [Source[name] for name in names if name in Source.__members__ and name != "Local"]
    candidates: list[tuple[float, Any]] = []
    seen: set[tuple[str, str]] = set()
    raw_count = 0

    search_errors: list[str] = []
    def do_search(source: Source, query: str) -> tuple[list[Any], str | None]:
        try:
            return list(search(source, query, SearchType.SONG)), None
        except Exception as exc:
            return [], f"{source.name}/{query}: {exc.__class__.__name__}: {exc}"

    # Search every configured source and query concurrently, as the desktop app does.
    with ThreadPoolExecutor(max_workers=max(1, len(sources) * 2)) as pool:
        futures = [pool.submit(do_search, source, query) for source in sources for query in queries]
        for future in as_completed(futures):
            results, search_error = future.result()
            if search_error:
                search_errors.append(search_error)
            for result in results:
                raw_count += 1
                key = (result.source.name, str(result.id or result.title or ""))
                if key in seen:
                    continue
                seen.add(key)
                if duration_filter and info.duration and result.duration and abs(info.duration - result.duration) > 4000:
                    continue
                title_score = calculate_title_score(title, result.title or "") if title else 0
                artist_score = None
                if info.artist and result.artist:
                    artist_score = calculate_artist_score(str(info.artist), str(result.artist))
                album_score = None
                if info.album and result.album:
                    album_score = text_difference(info.album.lower(), result.album.lower()) * 100
                if artist_score is not None:
                    score = (max(title_score * .5 + artist_score * .5,
                                  title_score * .5 + artist_score * .35 + (album_score or 0) * .15)
                             if album_score is not None else title_score * .5 + artist_score * .5)
                elif album_score is not None:
                    score = max(title_score * .7 + album_score * .3, title_score * .8)
                else:
                    score = title_score
                if title_score < 30:
                    score = max(0, score - 35)
                if score >= min_score:
                    candidates.append((score, result))

    candidates.sort(key=lambda pair: pair[0], reverse=True)
    errors: list[Exception] = []
    # Original code retries the best candidates; we try all scored candidates so
    # an API's first non-verbatim result cannot hide a later verbatim result.
    for _score, candidate in candidates:
        try:
            lyrics = get_lyrics(candidate)
            if lyrics.types.get("orig") == LyricsType.VERBATIM:
                return lyrics
        except Exception as exc:
            errors.append(exc)
    detail = "；".join(search_errors[:4])
    meta = f"识别为：{info.artist_title() or info.title or filename}；专辑：{info.album or '-'}；搜索返回 {raw_count} 条，评分后 {len(candidates)} 条"
    raise RuntimeError(f"未找到逐词歌词（已尝试 {len(candidates)} 个候选；{meta}）" + (f"；搜索错误：{detail}" if detail else ""))


def scan_sync(request: ScanRequest) -> dict[str, Any]:
    global last_run
    active = load_settings()
    root = Path(request.path or active["path"]).resolve()
    overwrite = active["overwrite"] if request.overwrite is None else request.overwrite
    source_names = active["sources"] if request.sources is None else request.sources
    min_score = int(active["min_score"] if request.min_score is None else request.min_score)
    duration_filter = bool(active["duration_filter"] if request.duration_filter is None else request.duration_filter)
    if not root.exists() or not root.is_dir():
        raise ValueError(f"目录不存在: {root}")
    scanned = updated = 0
    errors: list[str] = []
    targets: dict[Path, Path] = {}
    for path in root.rglob("*"):
        # Synology creates @eaDir sidecar folders. They are not user music and
        # must never be scanned or reported as failed songs.
        if any(part.startswith("@eaDir") for part in path.parts):
            continue
        if path.is_file() and path.suffix.lower() in AUDIO_EXTS:
            targets[path.with_suffix(".lrc")] = path

    for lrc, media in targets.items():
        scanned += 1
        try:
            if lrc.exists() and not overwrite and is_verbatim(read_text(lrc)):
                continue
            lyrics = choose_lyrics(song_info(media), source_names, min_score, duration_filter)
            lrc.write_text(verbatim_lrc(lyrics), encoding="utf-8")
            updated += 1
        except Exception as exc:
            errors.append(f"{media.relative_to(root)}: {exc}")
    last_run = {"status": "done", "scanned": scanned, "updated": updated, "errors": errors, "at": datetime.now(timezone.utc).isoformat()}
    return last_run


def scheduled_scan() -> None:
    while True:
        active = load_settings()
        interval = int(active.get("interval_minutes", 0)) if active.get("schedule_enabled") else 0
        if interval <= 0:
            threading.Event().wait(60)
            continue
        try:
            scan_sync(ScanRequest())
        except Exception as exc:
            last_run.update(status="error", errors=[str(exc)], at=datetime.now(timezone.utc).isoformat())
        threading.Event().wait(interval * 60)

@app.on_event("startup")
async def startup() -> None:
    threading.Thread(target=scheduled_scan, daemon=True, name="scheduled-scan").start()

@app.get("/")
async def index() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "index.html")

@app.get("/api/status")
async def status() -> dict[str, Any]:
    active = load_settings()
    return {**last_run, "music_root": active["path"], "interval_minutes": active["interval_minutes"] if active["schedule_enabled"] else 0, "settings": active}

@app.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    return load_settings()

@app.put("/api/settings")
async def put_settings(value: dict[str, Any]) -> dict[str, Any]:
    global app_settings
    value["interval_minutes"] = max(0, int(value.get("interval_minutes", 360)))
    value["min_score"] = min(100, max(0, int(value.get("min_score", 55))))
    value["schedule_enabled"] = bool(value.get("schedule_enabled", False))
    value["overwrite"] = bool(value.get("overwrite", False))
    value["duration_filter"] = bool(value.get("duration_filter", True))
    value["sources"] = [name for name in value.get("sources", []) if name in {"QM", "KG", "NE", "LRCLIB"}]
    if not value["sources"]:
        value["sources"] = DEFAULT_SETTINGS["sources"]
    app_settings = save_settings(value)
    return app_settings

@app.post("/api/scan")
async def scan(request: ScanRequest) -> dict[str, Any]:
    if lock.locked():
        raise HTTPException(409, "已有扫描任务正在运行")
    with lock:
        return await asyncio.to_thread(scan_sync, request)

@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
