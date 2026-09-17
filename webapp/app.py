"""LDDC headless web service: scan local LRC files and fetch verbatim lyrics."""
from __future__ import annotations

import asyncio
import os
import re
import threading
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
    for name in names:
        source = Source[name]
        try:
            results = search(source, info.artist_title() or info.title or info.path.stem, SearchType.SONG)
            candidates = list(results)
            if not candidates:
                continue
            # API result order is already source-ranked; try the first two candidates.
            for candidate in candidates[:2]:
                try:
                    lyrics = get_lyrics(candidate)
                    if lyrics.types.get("orig") == LyricsType.VERBATIM:
                        return lyrics
                except Exception:
                    continue
        except Exception:
            continue
    raise RuntimeError("未找到逐词歌词")


def scan_sync(request: ScanRequest) -> dict[str, Any]:
    global last_run
    root = Path(request.path).resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"目录不存在: {root}")
    scanned = updated = 0
    errors: list[str] = []
    targets: dict[Path, Path] = {}
    for path in root.rglob("*"):
        if path.suffix.lower() in AUDIO_EXTS:
            targets[path.with_suffix(".lrc")] = path
        elif path.suffix.lower() == ".lrc":
            targets.setdefault(path, path)

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
