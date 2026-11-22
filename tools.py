"""Core engine: rename helpers, URL helpers, aria2 download, ffprobe / ffmpeg, progress panels.

Every external command is started with an argument list (never through a shell), so file
names, URLs or metadata values can not inject shell commands.

The rename PARSER lives in ``namer.py`` (pure functions, unit-tested); it is re-exported here so the
rest of the bot keeps using ``tools.build_new_name`` & co.
"""
import asyncio
import json
import logging
import os
import re
import shutil
import time
from collections import deque
from pathlib import Path
from urllib.parse import unquote, urlparse

import aiohttp
from pyrogram.errors import FloodWait, MessageNotModified

import config
from namer import (  # noqa: F401  (re-exported)
    KNOWN_EXTS, apply_format, build_new_name, has_variables, parse_media_info, sanitize_filename,
    sort_key, split_name,
)
from tasks import Task, TaskCancelled, UserError
from utils import bar, btn, esc, format_bytes, format_time, kb, shorten

log = logging.getLogger("tools")
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"


def build_caption(template: str, filename: str, size=0, duration=0, width=0, height=0) -> str:
    values = {
        "filename": esc(filename),
        "size": format_bytes(size) if size else "N/A",
        "duration": format_time(duration) if duration else "N/A",
        "resolution": f"{width}x{height}" if width and height else "N/A",
    }
    template = template or config.DEFAULT_CAPTION
    return re.sub(r"\{(filename|size|duration|resolution)\}", lambda m: values[m.group(1)], template)


def clean_leftovers(root: Path) -> None:
    """Remove ONLY the bot's own work folders (``<user>_<task>``) - never anything else in the folder."""
    try:
        for child in root.iterdir():
            if child.is_dir() and re.fullmatch(r"-?\d+_\d+", child.name):
                shutil.rmtree(child, ignore_errors=True)
    except OSError:
        pass


# =========================================================================== #
# 2. Progress panels (single file panel + one shared panel for batches)
# =========================================================================== #
_ICONS = {"queued": "⏳", "downloading": "📥", "processing": "⚙️", "uploading": "📤"}
_STEPS = (("downloading", "📥", "Download"), ("processing", "⚙️", "Process"), ("uploading", "📤", "Upload"))
_STEP_NO = {"downloading": 1, "processing": 2, "uploading": 3}


def pipeline_line(state: str) -> str:
    """``✅ Download ▸ ⚙️ Process ▸ ⬜ Upload`` - finished steps are ticked, the current one is highlighted."""
    current = _STEP_NO.get(state, 0)
    parts = []
    for number, (_, emoji, label) in enumerate(_STEPS, start=1):
        mark = "⬜" if current == 0 or number > current else ("✅" if number < current else emoji)
        parts.append(f"{mark} {label}")
    return " ▸ ".join(parts)


def cancel_markup(task: Task):
    if task.batch is not None:
        return kb([[btn("❌ This file", f"t:cancel:{task.task_id}", "danger"),
                    btn("🛑 Whole batch", f"b:cancel:{task.batch.batch_id}", "danger")]])
    return kb([[btn("❌ Cancel Task", f"t:cancel:{task.task_id}", "danger")]])


def batch_markup(batch):
    return kb([[btn("🛑 Cancel batch", f"b:cancel:{batch.batch_id}", "danger"),
                btn("📋 Queue", "q:refresh", "primary")]])


def queued_text(task: Task, position: int) -> str:
    chips = f"\n🎞 <b>{esc(task.chips)}</b>" if task.chips else ""
    return (
        "⏳ <b>Added to queue</b>\n"
        f"<code>{esc(shorten(task.name, 60))}</code>\n"
        f"<blockquote>📍 <b>Position:</b> {position}   🆔 <code>#{task.task_id}</code>{chips}</blockquote>\n"
        f"<blockquote>{pipeline_line('queued')}</blockquote>"
    )


def progress_text(task: Task, action: str, current=0, total=0, speed=0.0, note: str = "") -> str:
    step = _STEP_NO.get(task.state)
    head = f"{_ICONS.get(task.state, '⏳')} <b>{action}</b>"
    if step:
        head += f"   <i>step {step}/3</i>"
    lines = [head, f"<code>{esc(shorten(task.name, 60))}</code>", ""]
    body = []
    if total > 0:
        percent = min(100.0, current * 100.0 / total)
        lines.append(f"<code>{bar(percent, 14)}</code>  <b>{percent:.1f}%</b>")
        lines.append("")
        body.append(f"💾 <b>Size</b>     {format_bytes(current)} / {format_bytes(total)}")
    elif current > 0:
        body.append(f"💾 <b>Done</b>     {format_bytes(current)}")
    if speed > 0:
        body.append(f"⚡ <b>Speed</b>   {format_bytes(speed)}/s")
        if total > current:
            body.append(f"⏱ <b>ETA</b>       {format_time((total - current) / speed)}")
    body.append(f"🕒 <b>Elapsed</b>  {format_time(time.time() - (task.started or task.created))}")
    if note:
        body.append(note)
    lines.append("<blockquote>" + "\n".join(body) + "</blockquote>")
    lines.append(f"<blockquote>{pipeline_line(task.state)}</blockquote>")
    chips = f"  ·  🎞 {esc(task.chips)}" if task.chips else ""
    lines.append(f"🆔 <code>#{task.task_id}</code>{chips}")
    return "\n".join(lines)


def _task_line(task: Task) -> str:
    p = task.progress or {}
    total, current, speed = p.get("total", 0), p.get("current", 0), p.get("speed", 0.0)
    head = f"{_ICONS.get(task.state, '⏳')} <b>{p.get('action') or task.state.title()}</b>  <code>{esc(shorten(task.name, 34))}</code>"
    if total > 0:
        percent = min(100.0, current * 100.0 / total)
        extra = f"<code>{bar(percent, 10)}</code> {percent:.0f}%"
        if speed > 0:
            extra += f" · {format_bytes(speed)}/s"
            if total > current:
                extra += f" · ETA {format_time((total - current) / speed)}"
        return head + "\n" + extra
    return head


def batch_text(batch) -> str:
    total = batch.total
    done, failed, cancelled = batch.count("done"), batch.count("failed"), batch.count("cancelled")
    finished = done + failed + cancelled
    percent = finished * 100.0 / total if total else 100.0
    stats = f"✅ {done} done"
    if failed:
        stats += f"   ❌ {failed} failed"
    if cancelled:
        stats += f"   🛑 {cancelled} cancelled"
    stats += f"   ⏳ {batch.waiting()} waiting"
    lines = [
        f"📦 <b>Batch #{batch.batch_id}</b>  ·  {total} files",
        f"<code>{bar(percent, 14)}</code>  <b>{percent:.0f}%</b>  ({finished}/{total})",
        f"<blockquote>{stats}</blockquote>",
    ]
    running = batch.running()
    for task in running[:3]:
        lines.append(_task_line(task))
    if len(running) > 3:
        lines.append(f"<i>... and {len(running) - 3} more running</i>")
    if not running:
        lines.append("<i>Starting the next file...</i>")
    lines.append(f"🕒 {format_time(time.time() - batch.created)}")
    return "\n".join(lines)


def batch_summary(batch) -> str:
    done, failed, cancelled = batch.count("done"), batch.count("failed"), batch.count("cancelled")
    icon = "✅" if not failed and not cancelled else "⚠️"
    lines = [
        f"{icon} <b>Batch #{batch.batch_id} finished</b>",
        f"<blockquote>✅ <b>{done}</b> done   ❌ <b>{failed}</b> failed   🛑 <b>{cancelled}</b> cancelled\n"
        f"📦 {batch.total} files in {format_time(time.time() - batch.created)}</blockquote>",
    ]
    problems = [t for t in batch.tasks if t.result == "failed"]
    if problems:
        shown = "\n".join(f"• <code>{esc(shorten(t.name, 32))}</code> - {esc(shorten(t.reason or 'error', 60))}"
                          for t in problems[:10])
        more = f"\n<i>... and {len(problems) - 10} more</i>" if len(problems) > 10 else ""
        lines.append(f"<b>Failed files</b>\n<blockquote>{shown}{more}</blockquote>")
    return "\n".join(lines)


async def _safe_edit(message, text, markup) -> float:
    """Edit a message; returns the extra seconds to wait (FloodWait) or 0."""
    try:
        await message.edit_text(text, reply_markup=markup)
    except MessageNotModified:
        pass
    except FloodWait as exc:
        return float(exc.value)
    except Exception as exc:  # editing is cosmetic - never break the task for it
        log.debug("status edit failed: %s", exc)
    return 0.0


async def edit_batch(batch, force: bool = False) -> None:
    now = time.time()
    if now - batch.last_edit < (1.5 if force else config.EDIT_INTERVAL):
        return
    batch.last_edit = now
    if batch.panel is None:
        return
    text = batch_summary(batch) if batch.finished else batch_text(batch)
    markup = None if batch.finished else batch_markup(batch)
    wait = await _safe_edit(batch.panel, text, markup)
    if wait:
        batch.last_edit = now + wait


async def edit_status(task: Task, text: str, force: bool = False, markup="default") -> None:
    """Edit the panel of a single-file task (batch members update the shared batch panel instead)."""
    if task.batch is not None:
        await edit_batch(task.batch, force)
        return
    now = time.time()
    if not force and now - task.last_edit < config.EDIT_INTERVAL:
        return
    task.last_edit = now
    if task.status_msg is None:
        return
    wait = await _safe_edit(task.status_msg, text, cancel_markup(task) if markup == "default" else markup)
    if wait:
        task.last_edit = now + wait


async def report(task: Task, action: str, current=0, total=0, speed=0.0, force: bool = False) -> None:
    """Store the progress of a task and refresh whichever panel shows it."""
    task.progress = {"action": action, "current": current, "total": total, "speed": speed}
    if task.batch is not None:
        await edit_batch(task.batch, force)
    else:
        await edit_status(task, progress_text(task, action, current, total, speed), force=force)


async def set_state(task: Task, state: str, action: str) -> None:
    task.state = state
    await report(task, action, force=True)


async def wait_my_turn(task: Task) -> None:
    """Files of ONE batch are UPLOADED in episode order.

    Downloading and processing of later files already ran in parallel; only the upload waits until every earlier
    file of the batch has finished (done, failed or cancelled). No deadlock is possible: the limiter starts files
    in sequence order, so every earlier file already runs or has ended.
    """
    batch = task.batch
    if batch is None:
        return
    announced = False
    while True:
        task.check()
        if all(t.result for t in batch.tasks if t.seq < task.seq):
            return
        if not announced:
            announced = True
            await report(task, "Waiting for its turn (uploads keep the episode order)", force=True)
        await asyncio.sleep(0.5)


async def telegram_progress(current, total, task: Task, action: str, started: float) -> None:
    """Pyrogram progress callback (download + upload)."""
    if task.cancelled:
        task.client.stop_transmission()  # raises StopTransmission inside Pyrogram
    last = task.batch.last_edit if task.batch is not None else task.last_edit
    if time.time() - last < config.EDIT_INTERVAL:
        task.progress = {"action": action, "current": current, "total": total,
                         "speed": current / max(0.001, time.time() - started)}
        return
    elapsed = time.time() - started
    await report(task, action, current, total, current / elapsed if elapsed > 0 else 0, force=True)


async def finish_task(task: Task, result: str, reason: str = "", single_text: str = "") -> None:
    """Record how a task ended; shows the final text (single file) or refreshes / finishes the batch panel."""
    task.result = result
    task.reason = reason
    batch = task.batch
    if batch is None:
        if single_text:
            await edit_status(task, single_text, force=True, markup=None)
        return
    if all(t.result for t in batch.tasks):
        batch.finished = True
        BATCHES_DONE.append(batch)
    await edit_batch(batch, force=True)


BATCHES_DONE: list = []  # finished batches (kept only so main.py can drop them from the registry)


# =========================================================================== #
# 3. URL helpers + aria2 download
# =========================================================================== #
def is_url(text: str) -> bool:
    return bool(re.match(r"(?i)^(https?|ftp)://\S+$", text.strip()))


def _name_from_headers(headers) -> str:
    disposition = headers.get("Content-Disposition", "")
    match = re.search(r"filename\*=(?:UTF-8|utf-8)''([^;]+)", disposition)
    if match:
        return unquote(match.group(1)).strip('"')
    match = re.search(r'filename="?([^";]+)"?', disposition)
    return unquote(match.group(1)).strip() if match else ""


async def get_url_info(url: str):
    """Return ``(file_name, size_in_bytes)``; either value may be empty / 0."""
    name, size, final_url = "", 0, url
    timeout = aiohttp.ClientTimeout(total=20)
    try:
        parsed = urlparse(url)
        async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}, timeout=timeout) as session:
            if "pixeldrain" in parsed.netloc.lower():
                file_id = parsed.path.rstrip("/").split("/")[-1]
                async with session.get(f"https://pixeldrain.com/api/file/{file_id}/info") as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        return data.get("name") or "", int(data.get("size") or 0)
                return "", 0
            try:
                async with session.head(url, allow_redirects=True) as resp:
                    name = _name_from_headers(resp.headers)
                    size = int(resp.headers.get("Content-Length") or 0)
                    final_url = str(resp.url)
            except aiohttp.ClientError:
                pass
            if not size:  # some servers reject HEAD - ask for a single byte instead
                async with session.get(url, headers={"Range": "bytes=0-0"}, allow_redirects=True) as resp:
                    total = resp.headers.get("Content-Range", "").rpartition("/")[2]
                    if total.isdigit():
                        size = int(total)
                    elif resp.status == 200:
                        size = int(resp.headers.get("Content-Length") or 0)
                    name = name or _name_from_headers(resp.headers)
                    final_url = str(resp.url)
    except Exception as exc:
        log.debug("get_url_info failed: %s", exc)
    if not name:
        name = unquote(urlparse(final_url).path.rsplit("/", 1)[-1])
    return name, size


_ARIA_LINE = re.compile(
    r"\[#\w+\s+([\d.]+)([KMGT]?i?B)/([\d.]+)([KMGT]?i?B)(?:\(\d+%\))?.*?DL:([\d.]+)([KMGT]?i?B)"
)
_UNITS = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3, "TiB": 1024 ** 4}


def _to_bytes(value: str, unit: str) -> float:
    return float(value) * _UNITS.get(unit, 1)


def parse_aria_line(line: str):
    """Return ``(downloaded, total, speed)`` in bytes from an aria2 summary line, or None."""
    match = _ARIA_LINE.search(line)
    if not match:
        return None
    done, done_u, total, total_u, speed, speed_u = match.groups()
    return _to_bytes(done, done_u), _to_bytes(total, total_u), _to_bytes(speed, speed_u)


async def download_from_url(task: Task, url: str, dest: Path, size_hint: int = 0) -> Path:
    parsed = urlparse(url)
    if "pixeldrain" in parsed.netloc.lower():
        url = f"https://pixeldrain.com/api/file/{parsed.path.rstrip('/').split('/')[-1]}"
    conns = str(config.ARIA2_CONNECTIONS)
    cmd = [
        "aria2c", f"--dir={dest.parent}", f"--out={dest.name}", "--summary-interval=1",
        "--console-log-level=warn", "--download-result=hide", "--allow-overwrite=true",
        "--auto-file-renaming=false", "--file-allocation=none", f"--max-connection-per-server={conns}",
        f"--split={conns}", "--min-split-size=1M", "--max-tries=3", "--retry-wait=3",
        "--connect-timeout=20", "--timeout=60", f"--user-agent={USER_AGENT}", url,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
    except FileNotFoundError:
        raise UserError("aria2c is not installed on this server.")
    task.proc = proc
    started = time.time()
    tail = deque(maxlen=5)
    try:
        while True:
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=3)
            except asyncio.TimeoutError:
                task.check()
                continue
            if not raw:
                break
            task.check()
            line = raw.decode("utf-8", errors="ignore").strip()
            stats = parse_aria_line(line)
            if stats:
                done, total, speed = stats
                await report(task, "Downloading", done, total or size_hint, speed)
            elif line and not line.startswith(("*", "=", "-", "FILE:", "Download Results")):
                tail.append(line)
        await proc.wait()
    finally:
        task.proc = None
        if proc.returncode is None:
            proc.kill()
    task.check()
    if proc.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        reason = " | ".join(tail)[-300:] or f"aria2 exit code {proc.returncode}"
        raise UserError(f"Download failed - the link may be expired or blocked.\n{reason}")
    log.info("Downloaded %s in %s", dest.name, format_time(time.time() - started))
    return dest


# =========================================================================== #
# 4. ffprobe / ffmpeg
# =========================================================================== #
async def run_cmd(*args, task: Task = None):
    proc = await asyncio.create_subprocess_exec(
        *map(str, args), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    if task:
        task.proc = proc
    try:
        out, err = await proc.communicate()
    finally:
        if task:
            task.proc = None
    return proc.returncode, out, err


async def probe(path: Path):
    """Return media info dict or ``None`` when ffprobe can not read the file."""
    try:
        code, out, _ = await run_cmd(
            "ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path
        )
    except FileNotFoundError:
        return None
    if code != 0:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    streams, width, height, has_video = [], 0, 0, False
    for s in data.get("streams", []):
        tags = {k.lower(): v for k, v in (s.get("tags") or {}).items()}
        kind = s.get("codec_type")
        attached = bool((s.get("disposition") or {}).get("attached_pic"))
        streams.append({"index": s.get("index"), "type": kind, "lang": (tags.get("language") or "").lower(),
                        "tags": tags, "attached": attached})
        if kind == "video" and not has_video and not attached:
            has_video, width, height = True, int(s.get("width") or 0), int(s.get("height") or 0)
    try:
        duration = int(float(data.get("format", {}).get("duration") or 0))
    except ValueError:
        duration = 0
    return {"duration": duration, "width": width, "height": height, "has_video": has_video, "streams": streams}


def _clean(value) -> str:
    return str(value).replace("\n", " ").strip() if value else ""


_GLOBAL_TAGS = (
    ("desc", "description"), ("artist", "artist"), ("album", "album"), ("author", "author"),
    ("official", "OFFICIAL_SITE"), ("copyright", "copyright"), ("publisher", "publisher"),
    ("genre", "genre"), ("encodedby", "encoded_by"), ("comment", "comment"),
)
# MP4 can only store these natively; the others need the extended "mdta" tag format.
_MP4_EXTENDED_ONLY = ("author", "official", "publisher", "encodedby")


def resolve_metadata(meta: dict, fallback_title: str) -> dict:
    """Merge per-field values with 'Change All'. Only non-empty values are returned."""
    change_all = _clean(meta.get("all"))
    values = {key: _clean(meta.get(key)) or change_all for key, _, _ in config.METADATA_FIELDS}
    values["title"] = _clean(meta.get("title")) or change_all or fallback_title
    return {key: value for key, value in values.items() if value}


def build_metadata_args(values: dict, is_mp4: bool = False) -> list:
    args = ["-metadata", f"title={values['title']}"]
    for key, tag in _GLOBAL_TAGS:
        if key in values:
            args += ["-metadata", f"{tag}={values[key]}"]
    if "year" in values:
        args += ["-metadata", f"date={values['year']}", "-metadata", f"year={values['year']}"]

    def track_title(selector: str, text: str) -> None:
        args.extend([f"-metadata:s:{selector}", f"title={text}"])
        if is_mp4:  # MP4 stores track names in handler_name
            args.extend([f"-metadata:s:{selector}", f"handler_name={text}"])

    track_title("v:0", values["title"])
    if "audiotitle" in values:
        track_title("a", values["audiotitle"])
    if "subtitle" in values:
        track_title("s", values["subtitle"])
    if "encoder" in values:
        # The muxer always overwrites the global 'encoder' tag, so it is written per stream as well.
        args += ["-metadata", f"encoder={values['encoder']}", "-metadata:s", f"encoder={values['encoder']}"]
    if is_mp4 and any(key in values for key in _MP4_EXTENDED_ONLY):
        args += ["-movflags", "+use_metadata_tags"]
    return args


_BULK_ALIASES = {"all": "all", "changeall": "all", "description": "desc", "site": "official",
                 "website": "official", "audio": "audiotitle", "sub": "subtitle", "subtitletitle": "subtitle"}


def parse_bulk_metadata(text: str):
    """Parse ``key: value`` lines. Returns ``(values, unknown_keys)``; a value of None means 'clear'."""
    aliases = dict(_BULK_ALIASES)
    for key, label, _ in config.METADATA_FIELDS:
        aliases[key] = key
        aliases[re.sub(r"[^a-z]", "", label.lower())] = key
    values, unknown = {}, []
    for line in text.splitlines():
        match = re.match(r"^\s*([^:=]+?)\s*[:=]\s*(.*?)\s*$", line)
        if not match:
            continue
        raw_key, value = match.groups()
        field = aliases.get(re.sub(r"[^a-z]", "", raw_key.lower()))
        if not field:
            unknown.append(raw_key.strip())
        elif value:
            values[field] = None if value == "-" else value[:200]
    return values, unknown


# Stream tags that must SURVIVE the "wipe old metadata" step: the audio / subtitle language and the
# file name + type of attached fonts (MKV can not store an attachment without them).
_KEEP_STREAM_TAGS = {"language", "filename", "mimetype"}


def _is_font(stream: dict) -> bool:
    tags = stream.get("tags") or {}
    name, mime = (tags.get("filename") or "").lower(), (tags.get("mimetype") or "").lower()
    return name.endswith((".ttf", ".otf", ".ttc", ".woff", ".woff2")) or any(k in mime for k in ("font", "truetype", "opentype"))


def _wipe_args(mapped: list, settings: dict) -> list:
    """ffmpeg options that drop the OLD metadata (global tags, chapters, every stream tag except language)."""
    args = ["-map_metadata:g", "-1"]
    if not settings.get("keep_chapters"):
        args += ["-map_chapters", "-1"]
    for out_index, stream in enumerate(mapped):  # output stream numbers follow the order of the -map options
        for key in stream.get("tags", {}):
            if key.lower() not in _KEEP_STREAM_TAGS:
                args += [f"-metadata:s:{out_index}", f"{key}="]  # an empty value deletes the tag
    return args + ["-fflags", "+bitexact"]  # no ffmpeg version string in the encoder tag


async def process_media(task: Task, src: Path, dst: Path, info: dict, settings: dict, meta: dict) -> bool:
    """Remux ``src`` -> ``dst`` (stream copy) applying audio/subtitle filters and metadata.

    * wipe_meta ON : the OLD metadata is removed first (global tags, chapters, stream titles, the old cover
      image, images attached to MKV) - audio/subtitle languages and fonts are kept - and then YOUR metadata is written.
    * Several strategies are tried before giving up, so metadata is applied reliably:
      1. everything (subtitles + attachments)  2. without subtitles  3. MP4 only: audio -> AAC.
    Returns False when ffmpeg could not handle the file (caller then keeps the original bytes).
    """
    streams = info["streams"]
    wipe = bool(settings.get("wipe_meta", True))
    video = [s for s in streams if s["type"] == "video" and not (wipe and s.get("attached"))]
    audio = [s for s in streams if s["type"] == "audio"]
    chosen = audio
    codes = config.AUDIO_LANGUAGES.get(settings["audio"], ("", ()))[1]
    if codes:
        matched = [s for s in audio if s["lang"] in codes]
        chosen = matched or audio  # never drop every audio track when the language is missing
    subs = [s for s in streams if s["type"] == "subtitle"]
    attachments = [s for s in streams if s["type"] == "attachment" and (not wipe or _is_font(s))]
    wants_subs = bool(settings["sub"]) and bool(subs)
    is_mp4 = dst.suffix.lower() in config.MP4_FAMILY
    is_mkv = dst.suffix.lower() == ".mkv"
    metadata = []
    if settings["meta_enabled"]:
        metadata = build_metadata_args(resolve_metadata(meta, dst.stem), is_mp4)

    plans = [(True, False)] if wants_subs else []     # (with_subs, audio_to_aac)
    plans.append((False, False))
    if is_mp4 and audio:
        plans.append((False, True))

    for with_subs, to_aac in plans:
        mapped = list(video) + list(chosen)
        if with_subs:
            mapped += subs
            if is_mkv:
                mapped += attachments  # keep attached fonts (ASS subtitles)
        cmd = ["ffmpeg", "-nostdin", "-y", "-i", src]
        for stream in mapped:
            cmd += ["-map", f"0:{stream['index']}"]
        cmd += ["-dn"] if not with_subs else []
        if wipe:
            cmd += _wipe_args(mapped, settings)
        cmd += metadata + ["-c", "copy"]
        if to_aac:
            cmd += ["-c:a", "aac", "-b:a", "192k"]
        if with_subs and is_mp4:
            cmd += ["-c:s", "mov_text"]
        cmd.append(dst)
        code, _, err = await run_cmd(*cmd, task=task)
        task.check()
        if code == 0 and dst.exists() and dst.stat().st_size > 0:
            if to_aac:
                task.notes.append("Audio was converted to AAC because the original codec can not be stored in MP4.")
            return True
        log.warning("ffmpeg failed (subs=%s, aac=%s): %s", with_subs, to_aac, err.decode(errors="ignore")[-300:])
        dst.unlink(missing_ok=True)
    return False


# --------------------------------------------------------------------------- #
# Thumbnail (320 px - Telegram limit) and HD cover (large photo)
# --------------------------------------------------------------------------- #
def frame_time(duration: int) -> int:
    """A moment after the intro, so the picture is not a black frame."""
    return int(min(90, max(1, duration * 0.12)))


async def _jpeg(src: Path, dst: Path, box: int, max_bytes: int, at: int = None, sharpen: bool = False):
    """Fit ``src`` (video frame or image) inside box x box - never upscaled, Lanczos scaling - and keep the
    BEST JPEG quality that still fits ``max_bytes``."""
    vf = (f"scale=w='min({box},iw)':h='min({box},ih)':force_original_aspect_ratio=decrease:"
          f"force_divisible_by=2:flags=lanczos")
    if sharpen:
        vf += ",unsharp=5:5:0.5:5:5:0.0"  # downscaling softens a picture a little - bring the detail back
    vf += ",format=yuvj420p"
    for quality in (2, 3, 4, 6, 9, 14, 20, 28):
        cmd = ["ffmpeg", "-nostdin", "-y"]
        if at is not None:
            cmd += ["-ss", str(at)]
        cmd += ["-i", src, "-vframes", "1", "-vf", vf, "-q:v", str(quality), dst]
        try:
            code, _, _ = await run_cmd(*cmd)
        except FileNotFoundError:
            return None
        if code != 0 or not dst.exists() or dst.stat().st_size == 0:
            if at:  # a seek past the end of a short clip gives no frame - retry from the start once
                at = 0
                continue
            return None
        if dst.stat().st_size <= max_bytes:
            return dst
    return dst if dst.exists() and dst.stat().st_size > 0 else None


async def make_thumbnail(src: Path, dst: Path, at: int = None):
    """Normal thumbnail: max 320 x 320 (both sides - portrait pictures too), best quality under 200 kB."""
    return await _jpeg(src, dst, config.THUMB_SIZE, config.THUMB_MAX_BYTES, at, sharpen=True)


async def make_cover(src: Path, dst: Path, at: int = None):
    """HD cover: up to COVER_SIZE px (default 1280), highest JPEG quality."""
    return await _jpeg(src, dst, config.COVER_SIZE, config.COVER_MAX_BYTES, at)


def supports_cover(client) -> bool:
    """True when the installed Pyrogram fork can send ``cover=`` with send_video (pyrotgfork can)."""
    import inspect
    try:
        return "cover" in inspect.signature(client.send_video).parameters
    except (TypeError, ValueError, AttributeError):
        return False


def cleanup(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
