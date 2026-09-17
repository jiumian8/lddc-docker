"""LDDC headless web service: scan local LRC files and fetch verbatim lyrics."""
from __future__ import annotations

import asyncio
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

class ScanRequest(BaseModel):
    path: str = "/music"
    overwrite: bool = False
    sources: list[str] = Field(default_factory=lambda: ["QM", "KG", "NE", "LRCLIB"])


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
    parts = re.split(r"\s+-\s+", name, maxsplit=1)
    artist, title = (parts[0], parts[1]) if len(parts) == 2 else (None, name)
    return SongInfo(source=Source.Local, title=title, artist=Artist(artist) if artist else None, path=audio)


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
            album=first("album"),
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


def choose_lyrics(info: SongInfo, names: list[str]) -> Any:
    """Match like the original auto_fetch: multi-source search, scoring and fallback."""
    title = (info.title or "").strip()
    artist_title = info.artist_title() if title and info.artist else title
    filename = info.path.stem if info.path else title
    queries = [query for query in (artist_title, title, filename) if query]
    sources = [Source[name] for name in names if name in Source.__members__ and name != "Local"]
    candidates: list[tuple[float, Any]] = []
    seen: set[tuple[str, str]] = set()

    def do_search(source: Source, query: str) -> list[Any]:
        try:
            return list(search(source, query, SearchType.SONG))
        except Exception:
            return []

    # Search every configured source and query concurrently, as the desktop app does.
    with ThreadPoolExecutor(max_workers=max(1, len(sources) * 2)) as pool:
        futures = [pool.submit(do_search, source, query) for source in sources for query in queries]
        for future in as_completed(futures):
            for result in future.result():
                key = (result.source.name, str(result.id or result.title or ""))
                if key in seen:
                    continue
                seen.add(key)
                if info.duration and result.duration and abs(info.duration - result.duration) > 4000:
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
                if score >= 55:
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
    raise RuntimeError(f"未找到逐词歌词（已尝试 {len(candidates)} 个候选）")


def scan_sync(request: ScanRequest) -> dict[str, Any]:
    global last_run
    root = Path(request.path).resolve()
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
            if lrc.exists() and not request.overwrite and is_verbatim(read_text(lrc)):
                continue
            lyrics = choose_lyrics(song_info(media), request.sources)
            lrc.write_text(verbatim_lrc(lyrics), encoding="utf-8")
            updated += 1
        except Exception as exc:
            errors.append(f"{media.relative_to(root)}: {exc}")
    last_run = {"status": "done", "scanned": scanned, "updated": updated, "errors": errors, "at": datetime.now(timezone.utc).isoformat()}
    return last_run


def scheduled_scan() -> None:
    interval = int(os.getenv("SCAN_INTERVAL_MINUTES", "0"))
    if interval <= 0:
        return
    while True:
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
    return {**last_run, "music_root": str(ROOT), "interval_minutes": int(os.getenv("SCAN_INTERVAL_MINUTES", "0"))}

@app.post("/api/scan")
async def scan(request: ScanRequest) -> dict[str, Any]:
    if lock.locked():
        raise HTTPException(409, "已有扫描任务正在运行")
    with lock:
        return await asyncio.to_thread(scan_sync, request)

@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
