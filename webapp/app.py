"""LDDC headless web service: scan local LRC files and fetch verbatim lyrics."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Cookie, Depends, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse
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

app = FastAPI(title="LDDC MUSIC", version="1.0.0")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
lock = threading.Lock()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("lddc-docker")
last_run: dict[str, Any] = {"status": "idle", "scanned": 0, "updated": 0, "skipped": 0, "failed": 0, "results": [], "at": None}
DEFAULT_SETTINGS = {"path": "/music", "schedule_enabled": True, "interval_minutes": 360, "overwrite": False, "sources": ["QM", "KG", "NE", "LRCLIB"], "min_score": 55, "duration_filter": True, "save_mode": "sidecar", "save_path": "/music", "lyrics_format": "verbatim", "filename_template": "%title% - %artist%"}
settings_file = STATE / "settings.json"
auth_file = STATE / "auth.json"
history_file = STATE / "scrape-history.json"
SESSION_TTL = int(os.getenv("SESSION_TTL_SECONDS", "14400"))
ALLOWED_ROOTS = [Path(item).resolve() for item in os.getenv("ALLOWED_ROOTS", "/music,/data,/lyrics").split(",") if item]
sessions: dict[str, float] = {}

class PasswordRequest(BaseModel):
    password: str


def password_hash(password: str, salt: bytes | None = None) -> dict[str, str | int]:
    salt = salt or secrets.token_bytes(16)
    rounds = 260000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return {"algorithm": "pbkdf2_sha256", "rounds": rounds, "salt": base64.b64encode(salt).decode(), "hash": base64.b64encode(digest).decode()}


def auth_config() -> dict[str, Any] | None:
    try:
        return json.loads(auth_file.read_text(encoding="utf-8"))
    except Exception:
        return None


def verify_password(password: str) -> bool:
    cfg = auth_config()
    if not cfg:
        return False
    salt = base64.b64decode(cfg["salt"])
    expected = base64.b64decode(cfg["hash"])
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(cfg["rounds"]))
    return hmac.compare_digest(digest, expected)


def create_session(response: Response) -> None:
    token = secrets.token_urlsafe(32)
    sessions[token] = time.time() + SESSION_TTL
    response.set_cookie("lddc_session", token, max_age=SESSION_TTL, httponly=True, secure=os.getenv("COOKIE_SECURE", "false").lower() == "true", samesite="lax", path="/")


def require_auth(lddc_session: str | None = Cookie(default=None)) -> None:
    if not auth_file.exists():
        raise HTTPException(401, "需要先设置管理员密码")
    if not lddc_session or sessions.get(lddc_session, 0) < time.time():
        raise HTTPException(401, "登录已过期")
    sessions[lddc_session] = time.time() + SESSION_TTL


def clear_session(response: Response, token: str | None) -> None:
    if token:
        sessions.pop(token, None)
    response.delete_cookie("lddc_session", path="/")


def ensure_allowed_path(path: Path) -> Path:
    resolved = path.resolve()
    if not any(resolved == root or root in resolved.parents for root in ALLOWED_ROOTS):
        allowed = ", ".join(str(root) for root in ALLOWED_ROOTS)
        raise ValueError(f"路径不在允许范围内: {resolved}; 允许范围: {allowed}")
    return resolved

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


def now_text() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def result_item(level: str, message: str) -> dict[str, str]:
    return {"time": now_text(), "level": level, "message": message}


def load_history() -> list[dict[str, Any]]:
    try:
        value = json.loads(history_file.read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []
    except Exception:
        return []


def save_history(run: dict[str, Any]) -> None:
    history = load_history()
    history.append(run)
    temp = history_file.with_suffix(".tmp")
    temp.write_text(json.dumps(history[-100:], ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(history_file)


saved_history = load_history()
if saved_history:
    last_run = saved_history[-1]

class ScanRequest(BaseModel):
    path: str | None = None
    overwrite: bool | None = None
    sources: list[str] | None = None
    min_score: int | None = None
    duration_filter: bool | None = None
    save_mode: str | None = None
    save_path: str | None = None
    lyrics_format: str | None = None
    filename_template: str | None = None


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


def lyrics_to_lrc(lyrics: Any, lyrics_format: str = "verbatim") -> str:
    """Serialize LDDC's lyrics without importing its Qt-bound config layer."""
    tags = "\n".join(f"[{key}:{value}]" for key, value in lyrics.tags.items() if key in {"al", "ar", "au", "by", "offset", "ti"} and value)
    lines: list[str] = []
    for line in lyrics["orig"]:
        start = line.words[0].start if line.words and line.words[0].start is not None else line.start
        if lyrics_format == "line":
            lines.append(f"[{timestamp(start)}]" + "".join(word.text for word in line.words))
            continue
        enhanced = lyrics_format == "enhanced"
        left, right = ("<", ">") if enhanced else ("[", "]")
        output = f"[{timestamp(start)}]"
        last_end = None if enhanced else start
        for word in line.words:
            if word.start is not None and word.start != last_end:
                output += f"{left}{timestamp(word.start)}{right}"
            output += word.text
            if word.end is not None:
                output += f"{left}{timestamp(word.end)}{right}"
            last_end = word.end
        if line.end is not None and not output.endswith(right):
            output += f"{left}{timestamp(line.end)}{right}"
        lines.append(output)
    header = (tags + "\n") if tags else ""
    return header + "[tool:jiumian]\n\n" + "\n".join(lines) + "\n"


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


def safe_name(value: str) -> str:
    return re.sub(r"[\\/:*?\"<>|]", "_", value).strip() or "lyrics"


def render_filename(template: str, media: Path, info: SongInfo) -> str:
    values = {
        "%title%": info.title or media.stem,
        "%artist%": str(info.artist) if info.artist else "",
        "%album%": info.album or "",
        "%filename%": media.stem,
    }
    name = template or DEFAULT_SETTINGS["filename_template"]
    for key, value in values.items():
        name = name.replace(key, value)
    return safe_name(name) + ".lrc"


def scan_sync(request: ScanRequest, trigger: str = "manual") -> dict[str, Any]:
    global last_run
    active = load_settings()
    root = ensure_allowed_path(Path(request.path or active["path"]))
    overwrite = active["overwrite"] if request.overwrite is None else request.overwrite
    source_names = active["sources"] if request.sources is None else request.sources
    min_score = int(active["min_score"] if request.min_score is None else request.min_score)
    duration_filter = bool(active["duration_filter"] if request.duration_filter is None else request.duration_filter)
    save_mode = active["save_mode"] if request.save_mode is None else request.save_mode
    save_path = ensure_allowed_path(Path(active["save_path"] if request.save_path is None else request.save_path))
    lyrics_format = active["lyrics_format"] if request.lyrics_format is None else request.lyrics_format
    filename_template = active["filename_template"] if request.filename_template is None else request.filename_template
    if not root.exists() or not root.is_dir():
        raise ValueError(f"目录不存在: {root}")
    scanned = updated = skipped = failed = 0
    results: list[dict[str, str]] = []
    targets: dict[Path, Path] = {}
    for path in root.rglob("*"):
        # Synology creates @eaDir sidecar folders. They are not user music and
        # must never be scanned or reported as failed songs.
        if any(part.startswith("@eaDir") for part in path.parts):
            continue
        if path.is_symlink():
            continue
        if path.is_file() and path.suffix.lower() in AUDIO_EXTS:
            targets[path.with_suffix(".lrc")] = path

    total = len(targets)
    started_at = datetime.now(timezone.utc).isoformat()
    last_run = {"status": "running", "trigger": trigger, "scanned": total, "processed": 0, "updated": 0, "skipped": 0, "failed": 0, "results": [result_item("info", f"开始扫描：共 {total} 首音乐；成功 0，失败 0，存在跳过 0")], "at": started_at}

    for lrc, media in targets.items():
        rel = str(media.relative_to(root))
        try:
            info = song_info(media)
            target = lrc if save_mode == "sidecar" else save_path / render_filename(filename_template, media, info)
            if target.exists() and not overwrite and is_verbatim(read_text(target)):
                skipped += 1
                results.append(result_item("skip", f"{rel} 已存在逐词歌词：{target}"))
            else:
                lyrics = choose_lyrics(info, source_names, min_score, duration_filter)
                target.parent.mkdir(parents=True, exist_ok=True)
                content = lyrics_to_lrc(lyrics, lyrics_format)
                temp = target.with_suffix(target.suffix + ".tmp")
                temp.write_text(content, encoding="utf-8")
                temp.replace(target)
                if not target.is_file() or target.stat().st_size == 0:
                    raise OSError(f"歌词写入后校验失败: {target}")
                updated += 1
                results.append(result_item("ok", f"{rel} -> {target}"))
                log.info("Saved lyrics: %s -> %s (%d bytes)", rel, target, target.stat().st_size)
        except Exception:
            failed += 1
            results.append(result_item("fail", rel))
            log.exception("Failed to scrape lyrics for %s", rel)
        last_run.update(processed=updated + skipped + failed, updated=updated, skipped=skipped, failed=failed, results=[last_run["results"][0], *results])

    results.append(result_item("info", f"扫描完成：共 {total} 首音乐；成功 {updated}，失败 {failed}，存在跳过 {skipped}"))
    last_run = {"status": "done", "trigger": trigger, "scanned": total, "processed": total, "updated": updated, "skipped": skipped, "failed": failed, "results": [last_run["results"][0], *results], "at": started_at, "finished_at": datetime.now(timezone.utc).isoformat()}
    save_history(last_run)
    log.info("Scan completed: trigger=%s total=%d success=%d failed=%d skipped=%d", trigger, total, updated, failed, skipped)
    return last_run


def scheduled_scan() -> None:
    while True:
        active = load_settings()
        interval = int(active.get("interval_minutes", 0)) if active.get("schedule_enabled") else 0
        if interval <= 0:
            threading.Event().wait(60)
            continue
        try:
            if lock.acquire(blocking=False):
                try:
                    scan_sync(ScanRequest(), trigger="scheduled")
                finally:
                    lock.release()
            else:
                log.info("Scheduled scan skipped because another scan is running")
        except Exception:
            log.exception("Scheduled scan failed")
            last_run.update(status="error", results=[result_item("fail", "定时扫描异常，详细信息见 Docker 日志")], at=datetime.now(timezone.utc).isoformat())
        threading.Event().wait(interval * 60)

@app.on_event("startup")
async def startup() -> None:
    threading.Thread(target=scheduled_scan, daemon=True, name="scheduled-scan").start()

@app.get("/")
async def index() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "index.html")

@app.get("/api/auth/status")
async def auth_status(lddc_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    configured = auth_file.exists()
    authenticated = bool(configured and lddc_session and sessions.get(lddc_session, 0) >= time.time())
    return {"configured": configured, "authenticated": authenticated, "session_ttl_seconds": SESSION_TTL}

@app.post("/api/auth/setup")
async def setup_auth(value: PasswordRequest, response: Response) -> dict[str, bool]:
    if auth_file.exists():
        raise HTTPException(409, "管理员密码已设置")
    if len(value.password) < 8:
        raise HTTPException(400, "密码至少 8 位")
    auth_file.write_text(json.dumps(password_hash(value.password), ensure_ascii=False, indent=2), encoding="utf-8")
    create_session(response)
    return {"ok": True}

@app.post("/api/auth/login")
async def login(value: PasswordRequest, response: Response) -> dict[str, bool]:
    if not verify_password(value.password):
        raise HTTPException(401, "密码错误")
    create_session(response)
    return {"ok": True}

@app.post("/api/auth/logout")
async def logout(response: Response, lddc_session: str | None = Cookie(default=None)) -> dict[str, bool]:
    clear_session(response, lddc_session)
    return {"ok": True}

@app.get("/api/status")
async def status(_: None = Depends(require_auth)) -> dict[str, Any]:
    active = load_settings()
    return {**last_run, "music_root": active["path"], "interval_minutes": active["interval_minutes"] if active["schedule_enabled"] else 0, "settings": active}

@app.get("/api/settings")
async def get_settings(_: None = Depends(require_auth)) -> dict[str, Any]:
    return load_settings()

@app.get("/api/history")
async def get_history(_: None = Depends(require_auth)) -> list[dict[str, Any]]:
    return list(reversed(load_history()))

@app.put("/api/settings")
async def put_settings(value: dict[str, Any], _: None = Depends(require_auth)) -> dict[str, Any]:
    global app_settings
    value["interval_minutes"] = max(0, int(value.get("interval_minutes", 360)))
    value["min_score"] = min(100, max(0, int(value.get("min_score", 55))))
    value["schedule_enabled"] = bool(value.get("schedule_enabled", False))
    value["overwrite"] = bool(value.get("overwrite", False))
    value["duration_filter"] = bool(value.get("duration_filter", True))
    value["save_mode"] = value.get("save_mode", "sidecar") if value.get("save_mode") in {"sidecar", "directory"} else "sidecar"
    value["path"] = str(ensure_allowed_path(Path(value.get("path") or DEFAULT_SETTINGS["path"])))
    value["save_path"] = str(ensure_allowed_path(Path(value.get("save_path") or DEFAULT_SETTINGS["save_path"])))
    value["lyrics_format"] = value.get("lyrics_format", "verbatim") if value.get("lyrics_format") in {"verbatim", "enhanced", "line"} else "verbatim"
    value["filename_template"] = value.get("filename_template") or DEFAULT_SETTINGS["filename_template"]
    value["sources"] = [name for name in value.get("sources", []) if name in {"QM", "KG", "NE", "LRCLIB"}]
    if not value["sources"]:
        value["sources"] = DEFAULT_SETTINGS["sources"]
    app_settings = save_settings(value)
    return app_settings

@app.post("/api/scan")
async def scan(request: ScanRequest, _: None = Depends(require_auth)) -> dict[str, Any]:
    if lock.locked():
        raise HTTPException(409, "已有扫描任务正在运行")
    with lock:
        return await asyncio.to_thread(scan_sync, request, "manual")

@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
