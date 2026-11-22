"""Auto-Rename Bot - entrypoint (v3.1).

Handlers, the processing pipeline (download -> rename / wipe old metadata / remux -> upload with thumbnail + HD cover)
and the startup / shutdown logic live here. Panels are in ``ui.py``, the engine is in ``tools.py`` / ``namer.py``.
"""
import asyncio

# The event loop must exist before the Pyrogram client is created.
LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)

import functools
import itertools
import logging
import os
import re
import shutil
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from pyrogram import Client, filters, idle
from pyrogram.enums import ParseMode
from pyrogram.errors import BadRequest, FloodWait, MessageNotModified
from pyrogram.types import BotCommand

import config
import namer
import tools
import ui
from database import db
from tasks import BATCHES, TASKS, Batch, Task, TaskCancelled, UserError, limiter, queue_position
from utils import COLOR_MODE, btn, esc, kb
from webserver import keep_alive_loop, start_web_server

config.validate()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
logging.getLogger("pyrogram").setLevel(logging.WARNING)
log = logging.getLogger("bot")

app = Client(
    "AutoRenameBot",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN,
    in_memory=True,  # no session file -> works on read-only / ephemeral disks
    max_concurrent_transmissions=config.MAX_TRANSMISSIONS,
    parse_mode=ParseMode.HTML,
    sleep_threshold=30,
)

INPUT_TTL = 300
MANUAL_TTL = 1800
input_state: dict = {}   # uid -> {action, chat_id, msg_id, expires}
MANUAL: dict = {}        # uid -> manual-rename session (files waiting for a typed name)
COLLECT: dict = {}       # (chat_id, uid) -> files that arrived together (becomes ONE batch)
BACKGROUND: set = set()  # keeps references to running tasks
_seen: OrderedDict = OrderedDict()
_arrival = itertools.count(1)
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
BOT_ID = {"id": 0}


def spawn(coro) -> None:
    """Run a long job in the background so Pyrogram's dispatcher is never blocked."""
    task = asyncio.get_running_loop().create_task(coro)
    BACKGROUND.add(task)
    task.add_done_callback(BACKGROUND.discard)


@dataclass
class Item:
    """One file (or link) that must be renamed."""
    client: object
    message: object            # message used to download the file and to reply to
    name: str                  # original file name
    uid: int                   # who asked
    chat_id: int
    suid: int                  # whose settings are used (differs from uid for group members)
    media: object = None
    url: str = None
    custom: str = None         # new name typed by the user (manual rename / "/l link | name")
    arrival: int = field(default_factory=lambda: next(_arrival))
    new_name: str = ""
    task: object = None


# --------------------------------------------------------------------------- #
# Who may do what:  owner  >  admins  >  authorised users  >  members of an authorised group
# --------------------------------------------------------------------------- #
def is_group(message) -> bool:
    chat = getattr(message, "chat", None)
    return getattr(getattr(chat, "type", None), "name", "") in ("GROUP", "SUPERGROUP")


def can_use(message) -> bool:
    user = message.from_user
    if not user:
        return False
    if db.is_authorized(user.id):
        return True
    if is_group(message):
        info = db.chat_info(message.chat.id)
        return bool(info and info.get("members", True))
    return False


def settings_uid_for(message, uid: int) -> int:
    """Group members who are not authorised use the settings of the admin who activated the group."""
    if is_group(message) and not db.is_authorized(uid):
        info = db.chat_info(message.chat.id)
        if info and info.get("by"):
            return int(info["by"])
    return uid


def _dedupe(message) -> bool:
    key = (message.chat.id, message.id)
    if key in _seen:
        return False
    _seen[key] = True
    if len(_seen) > 2000:
        _seen.popitem(last=False)
    return True


def guarded(func):
    """Private chat: authorised users only."""
    @functools.wraps(func)
    async def wrapper(client, message):
        user = message.from_user
        if not user:
            return
        if not db.is_authorized(user.id):
            if message.command and message.command[0] == "start":
                await message.reply("🔒 <b>This is a private bot.</b>\nAsk the owner to authorize you.")
            return
        if not _dedupe(message):
            return
        return await func(client, message)
    return wrapper


def group_ok(func):
    """Private chat (authorised users) AND authorised groups (members)."""
    @functools.wraps(func)
    async def wrapper(client, message):
        if not can_use(message) or not _dedupe(message):
            return
        return await func(client, message)
    return wrapper


def admin_only(func):
    @functools.wraps(func)
    async def wrapper(client, message):
        user = message.from_user
        if not user or not db.is_admin(user.id) or not _dedupe(message):
            return
        return await func(client, message)
    return wrapper


def owner_only(func):
    @functools.wraps(func)
    async def wrapper(client, message):
        user = message.from_user
        if not user or not db.is_owner(user.id) or not _dedupe(message):
            return
        return await func(client, message)
    return wrapper


def _arg_id(message):
    if len(message.command) >= 2 and message.command[1].lstrip("-").isdigit():
        return int(message.command[1])
    return None


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
@app.on_message(filters.command("start") & filters.private)
@guarded
async def start_cmd(client, message):
    text, markup = ui.start_panel(ui.role_of(message.from_user.id))
    try:
        await message.reply_photo(photo=config.WELCOME_PIC, caption=text, reply_markup=markup)
    except Exception:
        await message.reply(text, reply_markup=markup)


@app.on_message(filters.command("help") & (filters.private | filters.group))
@group_ok
async def help_cmd(client, message):
    text, markup = ui.help_panel()
    await message.reply(text, reply_markup=markup)


@app.on_message(filters.command("settings") & filters.private)
@guarded
async def settings_cmd(client, message):
    text, markup = ui.settings_main(message.from_user.id)
    await message.reply(text, reply_markup=markup)


@app.on_message(filters.command("metadata") & filters.private)
@guarded
async def metadata_cmd(client, message):
    text, markup = ui.metadata_panel(message.from_user.id)
    await message.reply(text, reply_markup=markup)


@app.on_message(filters.command("autorename") & filters.private)
@guarded
async def autorename_cmd(client, message):
    uid = message.from_user.id
    parts = message.text.split(None, 1)
    prefix = ""
    if len(parts) > 1:
        db.user(uid)["rename_format"] = parts[1].strip()[:200]
        db.touch()
        prefix = "✅ <b>Rename format saved!</b>\n\n"
    text, markup = ui.rename_panel(uid)
    await message.reply(prefix + text, reply_markup=markup)


@app.on_message(filters.command("queue") & (filters.private | filters.group))
@group_ok
async def queue_cmd(client, message):
    uid = message.from_user.id
    text, markup = ui.queue_panel(uid, db.is_admin(uid))
    await message.reply(text, reply_markup=markup)


@app.on_message(filters.command("cancel") & (filters.private | filters.group))
@group_ok
async def cancel_cmd(client, message):
    uid = message.from_user.id
    if input_state.pop(uid, None) or MANUAL.pop(uid, None):
        return await message.reply("✅ <b>Input cancelled.</b>")
    arg = message.command[1].lstrip("#") if len(message.command) > 1 else ""
    mine = [t for t in TASKS.values() if t.user_id == uid or db.is_admin(uid)]
    if arg == "all":
        for task in mine:
            task.cancel()
        await limiter.wake()
        return await message.reply(f"🛑 <b>Cancelling {len(mine)} file(s)...</b>")
    if arg.isdigit():
        task = TASKS.get(arg)
        if task and task in mine:
            task.cancel()
            await limiter.wake()
            return await message.reply(f"🛑 <b>Cancelling file</b> <code>#{arg}</code>...")
        return await message.reply("⚠️ <b>File not found.</b>")
    text, markup = ui.queue_panel(uid, db.is_admin(uid))
    await message.reply("Usage: <code>/cancel [id|all]</code>\n\n" + text, reply_markup=markup)


@app.on_message(filters.command("thumb") & filters.private)
@guarded
async def thumb_cmd(client, message):
    file_id = db.user(message.from_user.id)["thumb_file_id"]
    if not file_id:
        return await message.reply("🖼 <b>No custom picture yet.</b>\nSend me a photo (best: as a <b>File</b>) to set one.")
    markup = kb([[btn("🖼 Thumbnail Settings", "s:thumb", "primary"), btn("🗑 Delete", "s:thumb:del", "danger")]])
    try:
        await message.reply_photo(file_id, caption="🖼 <b>Your custom picture</b>", reply_markup=markup)
    except Exception:
        await message.reply_document(file_id, caption="🖼 <b>Your custom picture</b>", reply_markup=markup)


@app.on_message(filters.command("ping") & filters.private)
@guarded
async def ping_cmd(client, message):
    started = time.time()
    reply = await message.reply("🏓 <b>Pinging...</b>")
    await reply.edit_text(f"🏓 <b>Pong!</b> <code>{(time.time() - started) * 1000:.0f} ms</code>")


# ---- admin + owner ------------------------------------------------------- #
@app.on_message(filters.command("stats") & filters.private)
@admin_only
async def stats_cmd(client, message):
    text, markup = ui.stats_panel()
    await message.reply(text, reply_markup=markup)


@app.on_message(filters.command("admin") & filters.private)
@admin_only
async def admin_cmd(client, message):
    text, markup = ui.admin_panel(db.is_owner(message.from_user.id))
    await message.reply(text, reply_markup=markup)


@app.on_message(filters.command("auth") & filters.private)
@admin_only
async def auth_cmd(client, message):
    target = _arg_id(message)
    if target is None:
        users = ", ".join(f"<code>{u}</code>" for u in db.auth_list()) or "none"
        return await message.reply(f"🔐 <b>Authorized users:</b> {users}\nUsage: <code>/auth user_id</code>")
    db.add_auth(target)
    await message.reply(f"✅ <code>{target}</code> <b>is now authorized.</b>")


@app.on_message(filters.command("unauth") & filters.private)
@admin_only
async def unauth_cmd(client, message):
    target = _arg_id(message)
    if target is None:
        return await message.reply("Usage: <code>/unauth user_id</code>")
    removed = db.remove_auth(target)
    await message.reply("✅ <b>Removed.</b>" if removed else "⚠️ <b>That user was not in the list.</b>")


@app.on_message(filters.command("addadmin") & filters.private)
@owner_only
async def addadmin_cmd(client, message):
    target = _arg_id(message)
    if target is None:
        text, markup = ui.admins_panel()
        return await message.reply(text, reply_markup=markup)
    if db.add_admin(target):
        db.add_auth(target)  # an admin can of course use the bot
        await message.reply(f"👮 <code>{target}</code> <b>is now an admin.</b>\n"
                            "<i>They can use /auth /unauth /addgroup /stats and the admin panel.</i>")
    else:
        await message.reply("⚠️ <b>Already an admin (or the owner).</b>")


@app.on_message(filters.command(["removeadmin", "deladmin"]) & filters.private)
@owner_only
async def removeadmin_cmd(client, message):
    target = _arg_id(message)
    if target is None:
        return await message.reply("Usage: <code>/removeadmin user_id</code>")
    removed = db.remove_admin(target)
    await message.reply("✅ <b>Admin rights removed.</b>" if removed else "⚠️ <b>That user is not an admin.</b>")


@app.on_message(filters.command("admins") & filters.private)
@owner_only
async def admins_cmd(client, message):
    text, markup = ui.admins_panel()
    await message.reply(text, reply_markup=markup)


@app.on_message(filters.command("addgroup") & (filters.private | filters.group))
@admin_only
async def addgroup_cmd(client, message):
    if is_group(message):
        chat_id, title = message.chat.id, message.chat.title or ""
    else:
        chat_id, title = _arg_id(message), ""
        if chat_id is None or chat_id > 0:
            return await message.reply("Usage: send <code>/addgroup</code> <b>inside the group</b>, "
                                       "or <code>/addgroup -100123456789</code> here.")
    db.add_chat(chat_id, message.from_user.id, title)
    await message.reply(
        "✅ <b>Group activated!</b>\n<blockquote>Everyone in this group can now send files and I will rename them.\n"
        "Use /removegroup to stop. Settings used here: <b>yours</b> (the admin who activated it).</blockquote>\n"
        "<i>Tip: if I do not see files, make me a group admin or set Privacy to Disabled in @BotFather.</i>"
    )


@app.on_message(filters.command("removegroup") & (filters.private | filters.group))
@admin_only
async def removegroup_cmd(client, message):
    chat_id = message.chat.id if is_group(message) else _arg_id(message)
    if chat_id is None:
        return await message.reply("Usage: <code>/removegroup</code> inside the group, or <code>/removegroup -100...</code>")
    removed = db.remove_chat(chat_id)
    await message.reply("✅ <b>Group removed.</b>" if removed else "⚠️ <b>That group was not active.</b>")


@app.on_message(filters.command("restart") & filters.private)
@owner_only
async def restart_cmd(client, message):
    await message.reply("♻️ <b>Restarting...</b>")
    await restart_bot()


async def restart_bot() -> None:
    await db.flush()
    for task in list(TASKS.values()):
        task.cancel()
    await asyncio.sleep(1)
    os.execv(sys.executable, [sys.executable, *sys.argv])


@app.on_message(filters.new_chat_members & filters.group)
async def bot_added_to_group(client, message):
    if not BOT_ID["id"]:
        BOT_ID["id"] = (await client.get_me()).id
    if any(u.id == BOT_ID["id"] for u in (message.new_chat_members or [])) and not db.chat_info(message.chat.id):
        try:
            await message.reply(ui.group_added_text())
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Planning: new name + the S/E/quality chips of a file
# --------------------------------------------------------------------------- #
def plan_name(item: Item, original_name: str = None) -> str:
    st, user = db.settings(item.suid), db.user(item.suid)
    fmt = user["rename_format"] or config.DEFAULT_RENAME_FORMAT
    custom = item.custom
    if custom and namer.has_variables(custom):  # "{title} S{season}E{episode}" typed by hand
        fmt, custom = custom, None
    return tools.build_new_name(original_name or item.name, fmt, st["remove_words"], custom, st["bare_quality"])


def make_chips(name: str, suid: int) -> str:
    st = db.settings(suid)
    info = namer.parse_media_info(name, st["remove_words"], st["bare_quality"])
    parts = []
    if info["season"] or info["episode"]:
        parts.append((f"S{info['season']}" if info["season"] else "") + (f"E{info['episode']}" if info["episode"] else ""))
    if info["quality"]:
        parts.append(info["quality"])
    return " · ".join(parts)


def make_item(client, chat_message, uid: int, target, name: str, media=None, url=None, custom=None) -> Item:
    return Item(client=client, message=target, name=name, uid=uid, chat_id=chat_message.chat.id,
                suid=settings_uid_for(chat_message, uid), media=media, url=url, custom=custom)


# --------------------------------------------------------------------------- #
# Intake: files / links -> (manual naming) -> ONE batch -> queue
# --------------------------------------------------------------------------- #
async def collect(item: Item) -> None:
    """Files that arrive within a couple of seconds belong together: they are sorted and shown on ONE panel."""
    key = (item.chat_id, item.uid)
    box = COLLECT.setdefault(key, {"items": [], "first": time.time(), "handle": None})
    box["items"].append(item)
    if box["handle"] is not None:
        box["handle"].cancel()
    delay = config.BATCH_WINDOW if time.time() - box["first"] < 10 else 0.05  # never wait forever
    box["handle"] = asyncio.get_running_loop().call_later(delay, lambda: spawn(flush_collected(key)))


async def flush_collected(key) -> None:
    box = COLLECT.pop(key, None)
    if not box or not box["items"]:
        return
    items = sorted(box["items"], key=lambda it: namer.sort_key(it.name, it.arrival))
    first = items[0]
    private = not is_group(first.message)
    if private and not first.url and db.settings(first.suid)["rename_mode"] == "manual":
        await manual_start(first.client, first.uid, items)
    else:
        await enqueue(first.client, items)


async def enqueue(client, items: list) -> None:
    """Create the tasks in the final (sorted) order, show ONE panel, then start them. No limit on the count."""
    if not items:
        return
    tasks = []
    for item in items:
        item.new_name = plan_name(item)
        task = Task(user_id=item.uid, chat_id=item.chat_id, name=item.new_name,
                    source="url" if item.url else "telegram", client=client, settings_uid=item.suid,
                    chips=make_chips(item.name, item.suid))
        TASKS[task.task_id] = task
        item.task = task
        tasks.append(task)

    first = items[0]
    if len(tasks) > 1:
        batch = Batch(chat_id=first.chat_id, user_id=first.uid, total=len(tasks), client=client)
        batch.tasks = tasks
        for task in tasks:
            task.batch = batch
        BATCHES[batch.batch_id] = batch
        try:
            batch.panel = await first.message.reply(tools.batch_text(batch), reply_markup=tools.batch_markup(batch))
        except Exception as exc:
            log.warning("Could not send the batch panel: %s", exc)
    else:
        task = tasks[0]
        try:
            task.status_msg = await first.message.reply(
                tools.queued_text(task, queue_position(task)), reply_markup=tools.cancel_markup(task))
        except Exception as exc:
            log.warning("Could not send the status message: %s", exc)

    for item in items:  # spawn in sorted order - the limiter also enforces it by sequence number
        spawn(run_task(item))


# ---- manual rename -------------------------------------------------------- #
async def manual_start(client, uid: int, items: list) -> None:
    box = MANUAL.get(uid)
    if box and box["expires"] > time.time():  # a session is already running: just queue the new files
        box["pending"].extend(items)
        box["expires"] = time.time() + MANUAL_TTL
        return
    MANUAL[uid] = {"pending": list(items), "named": [], "chat_id": items[0].chat_id, "prompt": None,
                   "expires": time.time() + MANUAL_TTL}
    await manual_ask(client, uid)


async def manual_ask(client, uid: int) -> None:
    box = MANUAL.get(uid)
    if not box:
        return
    if not box["pending"]:
        named = box["named"]
        prompt = box["prompt"]
        MANUAL.pop(uid, None)
        if prompt is not None:
            try:
                await prompt.delete()
            except Exception:
                pass
        if named:
            await enqueue(client, named)
        return
    item = box["pending"][0]
    text, markup = ui.manual_prompt(item.name, plan_name(item), len(box["pending"]), len(box["named"]))
    prompt = box["prompt"]
    if prompt is not None:
        try:
            await client.edit_message_text(prompt.chat.id, prompt.id, text, reply_markup=markup)
            return
        except MessageNotModified:
            return
        except Exception:
            pass
    box["prompt"] = await client.send_message(box["chat_id"], text, reply_markup=markup)


async def manual_answer(client, uid: int, mode: str, text: str = "") -> None:
    box = MANUAL.get(uid)
    if not box or not box["pending"]:
        return
    item = box["pending"].pop(0)
    if mode == "name":
        item.custom = text
        box["named"].append(item)
    elif mode == "auto":
        box["named"].append(item)
    elif mode == "autoall":
        box["named"].append(item)
        box["named"].extend(box["pending"])
        box["pending"].clear()
    # mode == "skip": the file is simply dropped
    box["expires"] = time.time() + MANUAL_TTL
    await manual_ask(client, uid)


async def manual_cancel(client, uid: int) -> None:
    box = MANUAL.pop(uid, None)
    if box and box["prompt"] is not None:
        try:
            await box["prompt"].edit_text("🛑 <b>Manual rename cancelled.</b>")
        except Exception:
            pass


# ---- incoming messages ---------------------------------------------------- #
@app.on_message(filters.command(["l", "leech"]) & filters.private)
@guarded
async def leech_cmd(client, message):
    parts = message.text.split(None, 1)
    if len(parts) < 2:
        return await message.reply(
            "⚠️ <b>Usage:</b> <code>/l [direct link]</code>\n"
            "<i>Custom name:</i> <code>/l [link] | My New Name</code>"
        )
    link, _, custom = parts[1].partition("|")
    link, custom = link.strip(), custom.strip()
    if not tools.is_url(link):
        return await message.reply("⚠️ <b>Please send a valid http(s) / ftp direct link.</b>")
    guessed = link.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or "downloaded_video.mp4"
    item = make_item(client, message, message.from_user.id, message, guessed, url=link, custom=custom or None)
    await enqueue(client, [item])


@app.on_message(filters.command("rename") & (filters.private | filters.group))
@group_ok
async def rename_cmd(client, message):
    uid = message.from_user.id
    target = message.reply_to_message
    media = target and (target.document or target.video or target.audio)
    if not media:
        text, markup = ui.rename_usage(settings_uid_for(message, uid))
        return await message.reply(text, reply_markup=markup)
    if (getattr(media, "file_size", 0) or 0) > config.MAX_FILE_SIZE:
        return await message.reply("⚠️ <b>File size exceeds the limit!</b>")
    default_ext = ".mp3" if target.audio else ".mp4"
    name = getattr(media, "file_name", None) or f"file_{target.id}{default_ext}"
    parts = message.text.split(None, 1)
    typed = parts[1].strip() if len(parts) > 1 else ""
    item = make_item(client, message, uid, target, name, media=media, custom=typed or None)
    if typed:
        return await enqueue(client, [item])
    if is_group(message):
        return await message.reply("✏️ <b>Type the name after the command:</b> <code>/rename My New Name</code>")
    await manual_start(client, uid, [item])


@app.on_message((filters.document | filters.video | filters.audio) & (filters.private | filters.group))
@group_ok
async def media_handler(client, message):
    media = message.document or message.video or message.audio
    uid = message.from_user.id
    if message.document and not is_group(message):
        ext = os.path.splitext(getattr(media, "file_name", "") or "")[1].lower()
        if (getattr(media, "mime_type", "") or "").startswith("image/") or ext in IMAGE_EXTS:
            return await save_picture(message, media.file_id, as_file=True)  # an image sent as a FILE = sharpest thumbnail
    if (getattr(media, "file_size", 0) or 0) > config.MAX_FILE_SIZE:
        return await message.reply("⚠️ <b>File size exceeds the limit!</b>")
    default_ext = ".mp3" if message.audio else ".mp4"
    name = getattr(media, "file_name", None) or f"file_{message.id}{default_ext}"
    await collect(make_item(client, message, uid, message, name, media=media))


async def save_picture(message, file_id: str, as_file: bool = False) -> None:
    db.user(message.from_user.id)["thumb_file_id"] = file_id
    db.touch()
    markup = kb([[btn("🖼 Thumbnail Settings", "s:thumb", "primary")]])
    note = ("It will be used as the <b>thumbnail</b> and as the <b>HD cover</b> of your next videos."
            if as_file else
            "It will be used as thumbnail and HD cover. <i>Tip: send the picture as a <b>File</b> for the sharpest result.</i>")
    await message.reply(f"✅ <b>Custom picture saved!</b>\n<blockquote>{note}</blockquote>", reply_markup=markup)


@app.on_message(filters.photo & filters.private)
@guarded
async def save_thumb(client, message):
    await save_picture(message, message.photo.file_id)


# --------------------------------------------------------------------------- #
# Processing pipeline
# --------------------------------------------------------------------------- #
async def build_pictures(client, task: Task, workdir: Path, final: Path, info: dict, settings: dict, user: dict, wants_cover: bool):
    """Return (thumbnail, cover). Custom picture first (Smart mode), else a sharp frame from the video."""
    thumb = cover = None
    if settings["thumb_mode"] == "off":
        return thumb, cover
    picture = None
    custom_id = user["thumb_file_id"]
    if custom_id and settings["thumb_mode"] == "smart":
        try:
            raw = await client.download_media(custom_id, file_name=str(workdir / "custom_picture"))
            picture = Path(raw) if raw else None
        except Exception as exc:
            log.warning("Custom picture failed: %s", exc)
    if picture is not None:
        thumb = await tools.make_thumbnail(picture, workdir / "thumb.jpg")
        if wants_cover:
            cover = await tools.make_cover(picture, workdir / "cover.jpg")
    elif info["has_video"]:
        at = tools.frame_time(info["duration"])
        thumb = await tools.make_thumbnail(final, workdir / "thumb.jpg", at)
        if wants_cover:
            cover = await tools.make_cover(final, workdir / "cover.jpg", at)
    return thumb, cover


async def run_task(item: Item) -> None:
    task, client, message = item.task, item.client, item.message
    uid, suid = task.user_id, task.settings_uid or task.user_id
    settings, user = db.settings(suid), db.user(suid)
    workdir = config.DOWNLOAD_DIR / f"{uid}_{task.task_id}"
    outcome = None  # None = cancelled, True = ok, False = failed
    uploaded_bytes = 0
    try:
        original_name = item.name
        size = getattr(item.media, "file_size", 0) or 0
        if item.url:
            await tools.edit_status(task, "🔎 <b>Fetching link info...</b>", force=True)
            real_name, size = await tools.get_url_info(item.url)
            original_name = real_name or original_name
            item.new_name = plan_name(item, original_name)
        if size and size > config.MAX_FILE_SIZE:
            raise UserError(f"File is too large ({size / 1e9:.2f} GB). Limit: {config.MAX_FILE_SIZE / 1e9:.2f} GB.")

        new_name = item.new_name or plan_name(item, original_name)
        task.name = new_name
        ext = tools.split_name(original_name)[1] or ".mp4"

        async with limiter.slot(task):
            task.started = time.time()
            workdir.mkdir(parents=True, exist_ok=True)
            source, final = workdir / f"source{ext}", workdir / new_name

            # 1) download ------------------------------------------------------
            await tools.set_state(task, "downloading", "Downloading")
            if item.url:
                await tools.download_from_url(task, item.url, source, size)
            else:
                path = await message.download(
                    file_name=str(source), progress=tools.telegram_progress,
                    progress_args=(task, "Downloading", time.time()),
                )
                task.check()
                if not path:
                    raise UserError("Telegram download failed. Please try again.")
                source = Path(path)

            # 2) rename / wipe old metadata / remux ----------------------------
            await tools.set_state(task, "processing", "Processing")
            is_media = ext.lower() in config.VIDEO_EXTS | config.AUDIO_EXTS
            info = await tools.probe(source) if is_media else None
            processed = bool(info) and await tools.process_media(task, source, final, info, settings, user["metadata"])
            if processed:
                source.unlink(missing_ok=True)
            else:  # not a media file (or ffmpeg refused it): rename only, keep the original bytes
                shutil.move(str(source), str(final))
                if is_media:
                    task.notes.append("Metadata / audio / subtitle settings could NOT be applied "
                                      "(ffmpeg could not process this file). It was only renamed.")
            task.check()
            info = info or {"duration": 0, "width": 0, "height": 0, "has_video": False, "streams": []}

            # 3) thumbnail + HD cover ------------------------------------------
            suffix = final.suffix.lower()
            as_video = settings["mode"] == "video" and suffix in config.VIDEO_EXTS and info["has_video"]
            wants_cover = bool(settings["cover"]) and as_video and tools.supports_cover(client)
            thumb, cover = await build_pictures(client, task, workdir, final, info, settings, user, wants_cover)
            task.check()

            # 4) upload (files of one batch are uploaded in episode order) -----
            await tools.wait_my_turn(task)
            await tools.set_state(task, "uploading", "Uploading")
            uploaded_bytes = final.stat().st_size
            caption = tools.build_caption(settings["caption"], new_name, uploaded_bytes,
                                          info["duration"], info["width"], info["height"])
            common = dict(caption=caption, progress=tools.telegram_progress,
                          progress_args=(task, "Uploading", time.time()))
            thumb_arg = str(thumb) if thumb else None
            if as_video:
                kwargs = dict(duration=info["duration"], width=info["width"], height=info["height"],
                              thumb=thumb_arg, supports_streaming=True, **common)
                if cover:
                    kwargs["cover"] = str(cover)
                try:
                    sent = await client.send_video(task.chat_id, str(final), **kwargs)
                except (TypeError, BadRequest) as exc:
                    if "cover" not in kwargs:
                        raise
                    log.warning("Upload with cover failed (%s) - retrying with the thumbnail only", exc)
                    kwargs.pop("cover")
                    kwargs["progress_args"] = (task, "Uploading", time.time())
                    sent = await client.send_video(task.chat_id, str(final), **kwargs)
            elif settings["mode"] == "video" and suffix in config.AUDIO_EXTS:
                sent = await client.send_audio(
                    task.chat_id, str(final), duration=info["duration"], thumb=thumb_arg, **common)
            else:
                sent = await client.send_document(task.chat_id, str(final), thumb=thumb_arg, **common)
            task.check()
            if sent is None:
                raise UserError("Upload failed. Please try again.")

        outcome = True
        if task.batch is None and task.status_msg is not None:
            try:
                await task.status_msg.delete()
            except Exception:
                pass
        if task.notes:
            try:
                await sent.reply("⚠️ <b>Note</b>\n<blockquote>" + "\n".join(esc(n) for n in task.notes) + "</blockquote>")
            except Exception:
                pass
        if settings["delete_source"] and item.media is not None:
            try:
                await message.delete()
            except Exception:
                pass
        await tools.finish_task(task, "done")

    except TaskCancelled:
        await tools.finish_task(task, "cancelled", single_text="❌ <b>Task cancelled by user.</b>")
    except UserError as exc:
        outcome = False
        await tools.finish_task(task, "failed", reason=str(exc),
                                single_text=f"❌ <b>Failed</b>\n<blockquote>{esc(exc)}</blockquote>")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        outcome = False
        log.exception("Task #%s crashed", task.task_id)
        reason = f"{type(exc).__name__}: {str(exc)[:120]}"
        await tools.finish_task(task, "failed", reason=reason,
                                single_text=f"❌ <b>Unexpected error</b>\n<blockquote>{esc(reason)}</blockquote>")
    finally:
        TASKS.pop(task.task_id, None)
        tools.cleanup(workdir)
        if task.batch is not None and task.batch.finished:
            BATCHES.pop(task.batch.batch_id, None)
            tools.BATCHES_DONE.clear()
        if outcome is not None:
            db.record_result(uid, outcome, uploaded_bytes if outcome else 0)


# --------------------------------------------------------------------------- #
# Text input (values typed after tapping an "Edit" button, or new names in manual mode)
# --------------------------------------------------------------------------- #
@app.on_message(filters.private & filters.text & ~filters.regex(r"^/"))
@guarded
async def text_input(client, message):
    uid = message.from_user.id
    state = input_state.pop(uid, None)

    box = MANUAL.get(uid)
    if (not state or state["expires"] < time.time()) and box:
        if box["expires"] < time.time():
            MANUAL.pop(uid, None)
            return
        value = namer.sanitize_filename(message.text.strip()[:200])
        if message.text.strip():
            try:
                await message.delete()
            except Exception:
                pass
            await manual_answer(client, uid, "name", value)
        return

    if not state or state["expires"] < time.time():
        return
    value, action = message.text.strip(), state["action"]
    clear = value.lower() in ("-", "clear")
    st, user = db.settings(uid), db.user(uid)
    rollback = None

    notice = ""
    if action == "metabulk":
        values, unknown = tools.parse_bulk_metadata(message.text)
        if not values:
            input_state[uid] = {**state, "expires": time.time() + INPUT_TTL}  # let the user retry
            return await message.reply("⚠️ <b>No valid line found.</b>\nUse one tag per line, for example:\n"
                                       "<code>title: My Show\nartist: FlyTon</code>")
        for key, val in values.items():
            if val is None:
                user["metadata"].pop(key, None)
            else:
                user["metadata"][key] = val
        if unknown:
            notice = "⚠️ <b>Ignored unknown keys:</b> " + ", ".join(f"<code>{esc(k)}</code>" for k in unknown[:10])
        builder = ui.metadata_panel
    elif action.startswith("meta:"):
        key = action.split(":", 1)[1]
        if clear:
            user["metadata"].pop(key, None)
        else:
            user["metadata"][key] = value[:200]
        builder = ui.metadata_panel
    elif action == "fmt":
        user["rename_format"] = "" if clear else value[:200]
        builder = ui.rename_panel
    elif action == "caption":
        previous = st["caption"]
        rollback = lambda: st.__setitem__("caption", previous)
        st["caption"] = "" if clear else value[:700]
        builder = ui.caption_panel
    elif action == "words":
        words = [] if clear else [w.strip()[:40] for w in re.split(r"[,\n]", value) if w.strip()][:30]
        st["remove_words"] = words
        builder = ui.advanced_panel
    else:
        return
    db.touch()
    try:
        await message.delete()
    except Exception:
        pass
    try:
        text, markup = builder(uid)
        await client.edit_message_text(state["chat_id"], state["msg_id"], text, reply_markup=markup)
        if notice:
            await client.send_message(state["chat_id"], notice)
    except MessageNotModified:
        pass
    except Exception as exc:
        if rollback:
            rollback()
            db.touch()
        log.info("Panel refresh failed after input: %s", exc)
        await client.send_message(
            state["chat_id"], "⚠️ <b>That value could not be saved</b> (invalid HTML?). The previous value was kept.")


# --------------------------------------------------------------------------- #
# Callback queries
# --------------------------------------------------------------------------- #
async def edit(cq, panel) -> None:
    text, markup = panel
    try:
        await cq.message.edit_text(text, reply_markup=markup)
    except MessageNotModified:
        pass


async def ask(cq, uid, action, title, hint, cancel_target) -> None:
    input_state[uid] = {
        "action": action, "chat_id": cq.message.chat.id, "msg_id": cq.message.id,
        "expires": time.time() + INPUT_TTL,
    }
    await edit(cq, ui.prompt_panel(title, hint, cancel_target))


@app.on_callback_query()
async def on_callback(client, cq):
    uid = cq.from_user.id
    data = cq.data or ""
    ns = data.split(":")[0]
    # Cancel buttons also work for group members (they may only cancel THEIR OWN files)
    if not db.is_authorized(uid) and ns not in ("t", "b"):
        return await cq.answer("⛔ This bot is private.", show_alert=True)
    if ns not in ("t", "b", "man"):
        input_state.pop(uid, None)  # any other button press leaves the "waiting for input" mode
    try:
        result = await route_callback(client, cq, uid, data.split(":"))
    except MessageNotModified:
        result = None
    except Exception as exc:
        log.exception("Callback failed: %s", data)
        result = (f"⚠️ {type(exc).__name__}: {str(exc)[:80]}", True)
    try:
        if result:
            await cq.answer(result[0], show_alert=result[1])
        else:
            await cq.answer()
    except Exception:
        pass


async def route_callback(client, cq, uid, parts):
    """Returns ``(popup_text, show_alert)`` or None."""
    ns = parts[0]
    action = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else ""
    owner, admin = db.is_owner(uid), db.is_admin(uid)
    st, user = db.settings(uid), db.user(uid)
    if cq.message is None:
        return None

    if ns == "x":
        await cq.message.delete()
        return None

    if ns == "go":  # open a panel as a NEW message (keeps the /start card intact)
        panels = {"settings": ui.settings_main, "metadata": ui.metadata_panel, "rename": ui.rename_panel}
        if action in panels:
            text, markup = panels[action](uid)
        elif action == "help":
            text, markup = ui.help_panel()
        elif action == "queue":
            text, markup = ui.queue_panel(uid, admin)
        elif action == "admin" and admin:
            text, markup = ui.admin_panel(owner)
        else:
            return ("Not available.", True)
        await cq.message.reply(text, reply_markup=markup)
        return None

    if ns == "man":  # manual rename buttons
        box = MANUAL.get(uid)
        if not box or box["expires"] < time.time():
            MANUAL.pop(uid, None)
            return ("This rename session expired. Send the files again.", True)
        if action == "cancel":
            await manual_cancel(client, uid)
        elif action in ("auto", "skip", "autoall"):
            await manual_answer(client, uid, action)
        return None

    if ns == "s":
        if action == "main":
            await edit(cq, ui.settings_main(uid))
        elif action == "mode":
            st["mode"] = "document" if st["mode"] == "video" else "video"
            db.touch()
            await edit(cq, ui.settings_main(uid))
        elif action == "rmode":
            st["rename_mode"] = "auto" if st["rename_mode"] == "manual" else "manual"
            db.touch()
            await edit(cq, ui.rename_usage(uid) if arg == "rename" else ui.settings_main(uid))
            return ("Manual: the bot asks the name of every file." if st["rename_mode"] == "manual"
                    else "Auto: the format from /autorename is used.", False)
        elif action == "wipe":
            st["wipe_meta"] = not st["wipe_meta"]
            db.touch()
            await edit(cq, ui.settings_main(uid))
        elif action == "cover":
            st["cover"] = not st["cover"]
            db.touch()
            await edit(cq, ui.thumb_panel(uid) if arg == "thumb" else ui.settings_main(uid))
        elif action == "sub":
            st["sub"] = not st["sub"]
            db.touch()
            await edit(cq, ui.settings_main(uid))
        elif action == "audio":
            if arg in config.AUDIO_LANGUAGES:
                st["audio"] = arg
                db.touch()
            await edit(cq, ui.audio_panel(uid))
        elif action == "thumb":
            if arg == "del":
                user["thumb_file_id"] = ""
                db.touch()
                await edit(cq, ui.thumb_panel(uid))
                return ("Custom picture deleted.", False)
            await edit(cq, ui.thumb_panel(uid))
        elif action == "tmode":
            if arg in ui.THUMB_LABELS:
                st["thumb_mode"] = arg
                db.touch()
            await edit(cq, ui.thumb_panel(uid))
        elif action == "cap":
            if arg == "set":
                await ask(cq, uid, "caption", "Custom Caption",
                          "Variables: <code>{filename}</code> <code>{size}</code> <code>{duration}</code> "
                          "<code>{resolution}</code>\nHTML tags are supported.", "s:cap")
                return None
            if arg == "reset":
                st["caption"] = ""
                db.touch()
            await edit(cq, ui.caption_panel(uid))
        elif action == "fmt":
            if arg == "set":
                await ask(cq, uid, "fmt", "Rename Format",
                          "Example: <code>[FlyTon] {title} S{season}E{episode} {quality}</code>", "s:fmt")
                return None
            if arg == "reset":
                user["rename_format"] = ""
                db.touch()
            await edit(cq, ui.rename_panel(uid))
        elif action == "adv":
            if arg == "words":
                await ask(cq, uid, "words", "Remove Words",
                          "Comma separated words to strip from original names.\nExample: <code>www.site.com, x264, [Spam]</code>",
                          "s:adv")
                return None
            if arg == "delsrc":
                st["delete_source"] = not st["delete_source"]
            elif arg == "chap":
                st["keep_chapters"] = not st["keep_chapters"]
            elif arg == "bare":
                st["bare_quality"] = not st["bare_quality"]
            elif arg == "wclear":
                st["remove_words"] = []
            db.touch()
            await edit(cq, ui.advanced_panel(uid))
        elif action == "reset":
            if arg == "yes":
                user["settings"] = {}
                user["rename_format"] = ""
                db.touch()
                await edit(cq, ui.settings_main(uid))
                return ("Settings reset to defaults.", False)
            await edit(cq, ui.confirm_panel(
                "This resets all settings and the rename format. Metadata and your picture are kept.",
                "s:reset:yes", "s:main"))
        return None

    if ns == "m":
        if action == "open":
            await edit(cq, ui.metadata_panel(uid))
        elif action == "toggle":
            st["meta_enabled"] = not st["meta_enabled"]
            db.touch()
            await edit(cq, ui.metadata_panel(uid))
        elif action == "wipe":
            st["wipe_meta"] = not st["wipe_meta"]
            db.touch()
            await edit(cq, ui.metadata_panel(uid))
        elif action == "edit":
            label = "Change All" if arg == "all" else next((n for k, n, _ in config.METADATA_FIELDS if k == arg), None)
            if label is None:
                return ("Unknown field.", True)
            current = user["metadata"].get(arg)
            await ask(cq, uid, f"meta:{arg}", label,
                      f"Current: <code>{esc(current)}</code>" if current else "Current: <i>not set</i>", "m:open")
        elif action == "bulk":
            await ask(cq, uid, "metabulk", "Set Multiple Tags",
                      "One tag per line:\n<code>title: My Show\nartist: FlyTon\nyear: 2024</code>\n"
                      "<b>Keys:</b> all, title, desc, artist, album, year, author, official, copyright, "
                      "publisher, genre, encodedby, comment, encoder, audiotitle, subtitle\n"
                      "Use <code>key: -</code> to clear a tag.", "m:open")
        elif action == "reset":
            if arg == "yes":
                user["metadata"] = {}
                db.touch()
                await edit(cq, ui.metadata_panel(uid))
                return ("All metadata values cleared.", False)
            await edit(cq, ui.confirm_panel("Clear every saved metadata value?", "m:reset:yes", "m:open"))
        return None

    if ns == "t" and action == "cancel":
        task = TASKS.get(arg)
        if not task or (task.user_id != uid and not admin):
            return ("File not found or already finished.", True)
        task.cancel()
        await limiter.wake()
        return ("Cancelling...", False)

    if ns == "b" and action == "cancel":
        batch = BATCHES.get(arg)
        if not batch or (batch.user_id != uid and not admin):
            return ("Batch not found or already finished.", True)
        batch.cancel()
        await limiter.wake()
        return ("Cancelling the whole batch...", False)

    if ns == "q":
        if action == "all":
            for task in [t for t in TASKS.values() if admin or t.user_id == uid]:
                task.cancel()
            await limiter.wake()
            return ("Cancelling all your files...", False)
        await edit(cq, ui.queue_panel(uid, admin))
        return None

    if ns == "a" and admin:
        if action == "main":
            await edit(cq, ui.admin_panel(owner))
        elif action == "stats":
            await edit(cq, ui.stats_panel())
        elif action == "conc" and arg.isdigit() and 1 <= int(arg) <= config.MAX_PARALLEL:
            await limiter.set_limit(int(arg))
            db.data["global"]["max_tasks"] = int(arg)
            db.touch()
            await edit(cq, ui.admin_panel(owner))
        elif action == "groups":
            await edit(cq, ui.groups_panel())
        elif action == "gm" and arg.lstrip("-").isdigit():
            info = db.chat_info(arg)
            if info is not None:
                db.set_chat_members(arg, not info.get("members", True))
            await edit(cq, ui.groups_panel())
        elif action == "gdel" and arg.lstrip("-").isdigit():
            db.remove_chat(arg)
            await edit(cq, ui.groups_panel())
            return ("Group removed.", False)
        elif action == "admins" and owner:
            await edit(cq, ui.admins_panel())
        elif action == "restart" and owner:
            if arg == "yes":
                await cq.answer("Restarting...")
                await cq.message.edit_text("♻️ <b>Restarting...</b>")
                await restart_bot()
                return None
            await edit(cq, ui.confirm_panel("The bot will restart and running files will be cancelled.",
                                            "a:restart:yes", "a:main"))
        return None

    return ("Unknown or expired button.", False)


# --------------------------------------------------------------------------- #
# Startup / shutdown
# --------------------------------------------------------------------------- #
COMMANDS = [
    ("start", "Start the bot"), ("autorename", "Set the rename format"), ("rename", "Rename a file by hand"),
    ("settings", "Open settings"), ("metadata", "Metadata engine"), ("l", "Leech a direct link"),
    ("queue", "Running files"), ("cancel", "Cancel files"), ("thumb", "Show custom picture"),
    ("help", "Full guide"), ("ping", "Check latency"),
]


async def start_client() -> None:
    while True:
        try:
            await app.start()
            return
        except FloodWait as exc:
            log.warning("FloodWait while logging in - sleeping %ss", exc.value)
            await asyncio.sleep(float(exc.value) + 1)


async def main() -> None:
    log.info("Starting %s v%s on %s", config.BOT_NAME, config.VERSION, config.PLATFORM)
    # Only the bot's OWN work folders are removed - never anything else (your data folder is safe).
    config.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    tools.clean_leftovers(config.DOWNLOAD_DIR)

    await db.connect()
    await limiter.set_limit(db.data["global"].get("max_tasks", config.MAX_CONCURRENT_TASKS))
    runner = await start_web_server()
    keep_task = None
    if config.KEEP_ALIVE:
        if config.APP_URL:
            keep_task = asyncio.get_running_loop().create_task(keep_alive_loop())
        else:
            log.warning("KEEP_ALIVE is on but APP_URL / RENDER_EXTERNAL_URL is empty - ping skipped.")

    await start_client()
    me = await app.get_me()
    BOT_ID["id"] = me.id
    cover_ok = tools.supports_cover(app)
    log.info("Logged in as @%s | database=%s | %s | HD cover=%s", me.username, db.backend, COLOR_MODE,
             "supported" if cover_ok else "NOT supported by this Pyrogram fork (thumbnail only)")
    try:
        await app.set_bot_commands([BotCommand(name, desc) for name, desc in COMMANDS])
    except Exception as exc:
        log.warning("Could not set bot commands: %s", exc)
    if config.NOTIFY_OWNER_ON_START:
        warning = ""
        if config.IS_RENDER and db.backend == "json":
            warning = ("\n\n⚠️ <b>Database is temporary!</b> Render wipes the disk on every restart, so your "
                       "settings and metadata will be lost. Add the <code>MONGO_URI</code> variable.")
        if not cover_ok:
            warning += ("\n\n⚠️ <b>HD cover is not available:</b> install <code>pyrotgfork</code> "
                        "(<code>pip install -U pyrotgfork</code>) to send the big cover picture.")
        try:
            await app.send_message(
                config.OWNER_ID,
                f"✅ <b>{esc(config.BOT_NAME)} is online!</b>\n<blockquote>🌐 {esc(config.PLATFORM)}\n"
                f"💾 Database: {db.backend}\n🎨 {COLOR_MODE}\n🌄 HD cover: {'ON' if cover_ok else 'unavailable'}</blockquote>{warning}")
        except Exception as exc:
            log.info("Owner start notification skipped: %s", exc)

    await idle()

    log.info("Shutting down...")
    for task in list(TASKS.values()):
        task.cancel()
    pending = list(BACKGROUND)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    if keep_task:
        keep_task.cancel()
    await app.stop()
    await db.close()
    await runner.cleanup()


if __name__ == "__main__":
    LOOP.run_until_complete(main())
