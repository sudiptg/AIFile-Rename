"""Every panel (text + colourful keyboard) shown by the bot lives here."""
import os
import shutil

import config
import tools
from database import db
from tasks import BATCHES, TASKS, limiter
from utils import COLOR_MODE, btn, esc, format_bytes, kb, shorten, uptime

SAMPLE_NAME = "Naruto.Shippuden.S02E15.1080p.WEB-DL.x265.mkv"
THUMB_LABELS = {"smart": "Smart", "frame": "Frame", "off": "Off"}
STATE_LABELS = {"queued": "⏳ queued", "downloading": "📥 downloading", "processing": "⚙️ processing", "uploading": "📤 uploading"}


def _onoff(value: bool):
    return ("ON", "success") if value else ("OFF", "danger")


def _db_label() -> str:
    if config.IS_RENDER and db.backend == "json":
        return "json ⚠️ temporary - set MONGO_URI"
    return db.backend


def _fmt(uid) -> str:
    return db.user(uid)["rename_format"] or config.DEFAULT_RENAME_FORMAT


def role_of(uid) -> str:
    if db.is_owner(uid):
        return "owner"
    return "admin" if db.is_admin(uid) else "user"


# --------------------------------------------------------------------------- #
# Start / help
# --------------------------------------------------------------------------- #
def start_panel(role: str):
    lines = [
        "➜ /autorename - Set the rename format",
        "➜ /rename - Rename one file with a name you type",
        "➜ /l [link] - Leech a direct link",
        "➜ /settings - Output, thumbnail, cover &amp; more",
        "➜ /metadata - Custom metadata engine",
        "➜ /queue - Running &amp; waiting files",
        "➜ /help - Full guide",
    ]
    if role in ("owner", "admin"):
        lines.append("➜ /admin - Admin panel · /addgroup - Use me in a group")
    text = (
        f"👋 <b>Welcome to {esc(config.BOT_NAME)} Auto-Rename Bot!</b>\n\n"
        f"<blockquote>🛠 <b>Commands</b>\n{chr(10).join(lines)}</blockquote>\n"
        "📤 <i>Send me any number of files - they are renamed one after another, in episode order.</i>"
    )
    last = [btn("📖 Help", "go:help")]
    if role in ("owner", "admin"):
        last.append(btn("🛠 Admin", "go:admin", "danger"))
    rows = [
        [btn("⚙️ Settings", "go:settings", "primary"), btn("🏷 Metadata", "go:metadata", "primary")],
        [btn("✏️ Rename Format", "go:rename", "success"), btn("📋 Queue", "go:queue", "primary")],
        last,
        [btn("❌ Close", "x:close", "danger")],
    ]
    return text, kb(rows)


def help_panel():
    text = (
        "📖 <b>How it works</b>\n"
        "<blockquote>1. Set a format with /autorename\n"
        "2. Send files (as many as you like) or use <code>/l link</code>\n"
        "3. The bot downloads, renames, <b>wipes the old metadata</b>, writes yours and uploads them back - "
        "in episode order, with ONE progress panel.</blockquote>\n"
        "<b>Understood automatically</b>\n"
        "<blockquote>🎞 <b>Quality:</b> <code>1080p</code> <code>q144</code> <code>720</code> <code>1920x1080</code> "
        "<code>[480]</code> <code>4k</code> <code>FHD</code>\n"
        "📺 <b>Episode:</b> <code>E05</code> <code>Ep 7</code> <code>episode_1</code> <code>- 12</code> <code>2x08</code>\n"
        "🍿 <b>Season:</b> <code>S02</code> <code>season 1</code> <code>season_2_</code> <code>2nd season</code></blockquote>\n"
        "<b>Format variables</b>\n"
        "<blockquote><code>{title}</code> <code>{season}</code> <code>{episode}</code> <code>{quality}</code>\n"
        "<code>{year}</code> <code>{codec}</code> <code>{original}</code></blockquote>\n"
        "<b>Manual rename</b>\n"
        "<blockquote>• <code>/rename New Name</code> as a reply to a file\n"
        "• Settings ▸ <b>Rename mode: Manual</b> - the bot asks the name of every file\n"
        "• <code>/l link | My New Name</code> - leech with a custom name</blockquote>\n"
        "<b>Picture</b>\n"
        "<blockquote>• Send a photo <b>as a File</b> to set a custom thumbnail (best quality)\n"
        "• A 320px thumbnail + a big <b>HD cover</b> are added to every video</blockquote>\n"
        "<b>Groups &amp; admins</b>\n"
        "<blockquote>• An admin adds me to a group and sends /addgroup there\n"
        "• The owner gives admin rights with <code>/addadmin id</code>\n"
        "• /cancel [id|all] - cancel files</blockquote>"
    )
    return text, kb([[btn("⚙️ Settings", "go:settings", "primary"), btn("❌ Close", "x:close", "danger")]])


def prompt_panel(title: str, hint: str, cancel_target: str):
    text = (
        f"✏️ <b>{esc(title)}</b>\n<blockquote>{hint}</blockquote>\n"
        "<i>Send the new value now, or tap Cancel. Send</i> <code>-</code> <i>to clear it.</i>"
    )
    return text, kb([[btn("❌ Cancel", cancel_target, "danger")]])


def confirm_panel(question: str, yes_data: str, no_data: str):
    text = f"⚠️ <b>Are you sure?</b>\n<blockquote>{question}</blockquote>"
    return text, kb([[btn("✅ Yes, do it", yes_data, "success"), btn("✖️ No", no_data, "danger")]])


def manual_prompt(name: str, suggestion: str, left: int, named: int):
    """Shown for every file while Rename mode = Manual (or /rename without a name)."""
    text = (
        f"✏️ <b>Send the new name</b>   <i>file {named + 1} of {named + left}</i>\n"
        f"<code>{esc(shorten(name, 70))}</code>\n\n"
        f"<blockquote>🤖 <b>Auto name would be</b>\n<code>{esc(shorten(suggestion, 90))}</code></blockquote>\n"
        "<i>Type the name now (the extension is added for you). You may use</i> "
        "<code>{title}</code> <code>{season}</code> <code>{episode}</code> <code>{quality}</code>"
    )
    rows = [[btn("🤖 Use auto name", "man:auto", "success"), btn("⏭ Skip file", "man:skip", "primary")]]
    if left > 1:
        rows.append([btn(f"⚡ Auto name for all {left}", "man:autoall", "success")])
    rows.append([btn("🛑 Cancel all", "man:cancel", "danger")])
    return text, kb(rows)


def rename_usage(uid):
    st = db.settings(uid)
    mode = "Manual" if st["rename_mode"] == "manual" else "Auto"
    text = (
        "✏️ <b>Rename</b>\n"
        "<blockquote><b>Reply to a file</b> with:\n"
        "<code>/rename My New Name</code> - uses exactly this name\n"
        "<code>/rename</code> - the bot asks you for the name</blockquote>\n"
        f"<b>Rename mode now:</b> {mode}\n"
        "<i>Auto = the format from /autorename · Manual = the bot asks the name of every file you send.</i>"
    )
    rows = [[btn(f"✏️ Rename mode: {mode}", "s:rmode:rename", "primary")],
            [btn("✏️ Rename Format", "s:fmt", "success"), btn("❌ Close", "x:close", "danger")]]
    return text, kb(rows)


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def settings_main(uid):
    st, user = db.settings(uid), db.user(uid)
    sub_label, sub_style = _onoff(st["sub"])
    wipe_label, wipe_style = _onoff(st["wipe_meta"])
    cover_label, cover_style = _onoff(st["cover"])
    audio_label = config.AUDIO_LANGUAGES.get(st["audio"], ("All tracks",))[0]
    mode_label = "Video" if st["mode"] == "video" else "Document"
    rmode = "Manual" if st["rename_mode"] == "manual" else "Auto"
    custom = " + custom" if user["thumb_file_id"] else ""
    text = (
        "⚙️ <b>Settings</b>\n"
        "<blockquote>Fine-tune how your files are renamed, processed and uploaded.</blockquote>\n"
        f"🎬 <b>Upload as:</b> {mode_label}\n"
        f"✏️ <b>Rename mode:</b> {rmode}\n"
        f"🎵 <b>Audio:</b> {audio_label}   📝 <b>Subtitles:</b> {sub_label}\n"
        f"🖼 <b>Thumbnail:</b> {THUMB_LABELS[st['thumb_mode']]}{custom}   🌄 <b>HD cover:</b> {cover_label}\n"
        f"🏷 <b>Metadata:</b> {'ON' if st['meta_enabled'] else 'OFF'}   🧼 <b>Wipe old:</b> {wipe_label}\n"
        f"✏️ <b>Format:</b> <code>{esc(shorten(_fmt(uid), 60))}</code>"
    )
    rows = [
        [btn(f"🎬 Upload as: {mode_label}", "s:mode", "primary")],
        [btn(f"✏️ Rename: {rmode}", "s:rmode", "primary"), btn(f"🧼 Wipe old meta: {wipe_label}", "s:wipe", wipe_style)],
        [btn(f"🎵 Audio: {audio_label}", "s:audio", "primary"), btn(f"📝 Subs: {sub_label}", "s:sub", sub_style)],
        [btn("🖼 Thumbnail", "s:thumb", "primary"), btn(f"🌄 HD Cover: {cover_label}", "s:cover", cover_style)],
        [btn("💬 Caption", "s:cap", "primary"), btn("✏️ Rename Format", "s:fmt", "success")],
        [btn("🏷 Metadata", "m:open", "success"), btn("🧰 Advanced", "s:adv", "primary")],
        [btn("♻️ Reset", "s:reset", "danger"), btn("❌ Close", "x:close", "danger")],
    ]
    return text, kb(rows)


def audio_panel(uid):
    current = db.settings(uid)["audio"]
    text = (
        "🎵 <b>Audio Track Filter</b>\n"
        "<blockquote>Keep only the chosen language. If a file has no track in that language, "
        "all tracks are kept so you never get a silent video.</blockquote>"
    )
    buttons = []
    for key, (label, _) in config.AUDIO_LANGUAGES.items():
        selected = key == current
        buttons.append(btn(f"✅ {label}" if selected else label, f"s:audio:{key}", "success" if selected else None))
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([btn("🔙 Back", "s:main")])
    return text, kb(rows)


def thumb_panel(uid):
    st, user = db.settings(uid), db.user(uid)
    has_custom = bool(user["thumb_file_id"])
    cover_label, cover_style = _onoff(st["cover"])
    text = (
        "🖼 <b>Thumbnail &amp; HD Cover</b>\n"
        f"<blockquote>Custom picture: <b>{'saved ✅' if has_custom else 'not set'}</b>\n"
        "📷 Send a photo to this chat to set it. <b>Send it as a File</b> (not compressed) for the sharpest result.</blockquote>\n"
        "<b>Every video gets two pictures</b>\n"
        f"• <b>Thumbnail</b> - small (Telegram limit 320px), top JPEG quality\n"
        f"• <b>HD cover</b> - big ({config.COVER_SIZE}px) picture on the video: <b>{cover_label}</b>\n\n"
        "<b>Modes</b>\n"
        "• <b>Smart</b> - your custom picture, else a video frame\n"
        "• <b>Frame</b> - always a frame from the video\n"
        "• <b>Off</b> - no picture at all"
    )
    mode_buttons = []
    for key, label in THUMB_LABELS.items():
        selected = st["thumb_mode"] == key
        mode_buttons.append(btn(f"✅ {label}" if selected else label, f"s:tmode:{key}", "success" if selected else "primary"))
    rows = [mode_buttons, [btn(f"🌄 HD Cover: {cover_label}", "s:cover:thumb", cover_style)]]
    if has_custom:
        rows.append([btn("🗑 Delete custom picture", "s:thumb:del", "danger")])
    rows.append([btn("🔙 Back", "s:main")])
    return text, kb(rows)


def caption_panel(uid):
    template = db.settings(uid)["caption"]
    preview = tools.build_caption(template, "Naruto S02E15 1080p.mkv", 734003200, 1440, 1920, 1080)
    text = (
        "💬 <b>Custom Caption</b>\n"
        f"<blockquote>Template: <code>{esc(template or config.DEFAULT_CAPTION)}</code></blockquote>\n"
        f"<b>Preview</b>\n{preview}\n\n"
        "<b>Variables:</b> <code>{filename}</code> <code>{size}</code> <code>{duration}</code> <code>{resolution}</code>\n"
        "<i>HTML tags like &lt;b&gt;, &lt;i&gt;, &lt;code&gt;, &lt;blockquote&gt; are supported.</i>"
    )
    rows = [
        [btn("✏️ Set Caption", "s:cap:set", "success"), btn("♻️ Reset", "s:cap:reset", "danger")],
        [btn("🔙 Back", "s:main")],
    ]
    return text, kb(rows)


def rename_panel(uid):
    fmt = _fmt(uid)
    st = db.settings(uid)
    tests = ["Naruto q144 1080.mkv", "Naruto season 1 Episode 1.mkv", "Naruto episode_1_season_2_.mkv"]
    sample = tools.build_new_name(SAMPLE_NAME, fmt, st["remove_words"], bare_quality=st["bare_quality"])
    extra = "\n".join(
        f"<code>{esc(shorten(t, 30))}</code> ➜ <code>{esc(shorten(tools.build_new_name(t, fmt, st['remove_words'], bare_quality=st['bare_quality']), 40))}</code>"
        for t in tests
    )
    text = (
        "✏️ <b>Auto-Rename Format</b>\n"
        f"<blockquote>Current: <code>{esc(fmt)}</code>\n"
        f"Preview: <code>{esc(sample)}</code>\n"
        f"<i>from</i> <code>{esc(SAMPLE_NAME)}</code></blockquote>\n"
        f"<b>More examples</b>\n<blockquote>{extra}</blockquote>\n"
        "<b>Variables</b>\n"
        "<code>{title}</code> <code>{season}</code> <code>{episode}</code> <code>{quality}</code> "
        "<code>{year}</code> <code>{codec}</code> <code>{original}</code>\n\n"
        "<i>Missing values (e.g. no season) are removed automatically.</i>\n"
        "Quick set: <code>/autorename [FlyTon] {title} S{season}E{episode} {quality}</code>"
    )
    rows = [
        [btn("✏️ Set Format", "s:fmt:set", "success"), btn("♻️ Default", "s:fmt:reset", "danger")],
        [btn("⚙️ Settings", "s:main"), btn("❌ Close", "x:close", "danger")],
    ]
    return text, kb(rows)


def advanced_panel(uid):
    st = db.settings(uid)
    del_label, del_style = _onoff(st["delete_source"])
    chap_label, chap_style = _onoff(st["keep_chapters"])
    bare_label, bare_style = _onoff(st["bare_quality"])
    words = ", ".join(st["remove_words"]) or "none"
    text = (
        "🧰 <b>Advanced</b>\n"
        f"<blockquote>🗑 <b>Auto-delete source:</b> {del_label}\n"
        f"📖 <b>Keep chapters</b> (when wiping old metadata)<b>:</b> {chap_label}\n"
        f"🔢 <b>Lone number = quality</b> (<code>Naruto 720</code> ➜ 720p)<b>:</b> {bare_label}\n"
        f"🚫 <b>Remove words:</b> <code>{esc(shorten(words, 80))}</code></blockquote>\n"
        "<i>Remove words are stripped from the original file name before renaming "
        "(site names, tags, spam...).</i>"
    )
    rows = [
        [btn(f"🗑 Delete source: {del_label}", "s:adv:delsrc", del_style)],
        [btn(f"📖 Keep chapters: {chap_label}", "s:adv:chap", chap_style)],
        [btn(f"🔢 Lone number = quality: {bare_label}", "s:adv:bare", bare_style)],
        [btn("🚫 Set Remove Words", "s:adv:words", "primary"), btn("🧹 Clear", "s:adv:wclear", "danger")],
        [btn("🔙 Back", "s:main")],
    ]
    return text, kb(rows)


# --------------------------------------------------------------------------- #
# Metadata
# --------------------------------------------------------------------------- #
def metadata_panel(uid):
    meta = db.user(uid)["metadata"]
    st = db.settings(uid)
    enabled = st["meta_enabled"]

    def value(key):
        stored = meta.get(key)
        return f"<code>{esc(shorten(stored, 40))}</code>" if stored else "<i>not set</i>"

    lines = [f"🔄 <b>Change All:</b> {value('all')}"]
    lines += [f"{emoji} <b>{label}:</b> {value(key)}" for key, label, emoji in config.METADATA_FIELDS]
    text = (
        "🏷 <b>Metadata Engine</b>\n"
        f"<blockquote>{chr(10).join(lines)}</blockquote>\n"
        f"🧼 <b>Wipe old metadata:</b> {'ON' if st['wipe_meta'] else 'OFF'} - the uploader's old tags, comments, "
        "track names and chapters are removed first, then YOUR tags are written (audio/subtitle languages are kept).\n"
        "<i>'Change All' fills every tag that has no value of its own. 'Set Multiple' saves many tags in one message.</i>"
    )
    label, style = _onoff(enabled)
    wipe_label, wipe_style = _onoff(st["wipe_meta"])
    rows = [[btn(f"🏷 Metadata: {label}", "m:toggle", style), btn(f"🧼 Wipe old: {wipe_label}", "m:wipe", wipe_style)],
            [btn("🔄 Change All", "m:edit:all", "success"), btn("📋 Set Multiple", "m:bulk", "success")]]
    fields = [btn(f"{emoji} {name}", f"m:edit:{key}", "primary") for key, name, emoji in config.METADATA_FIELDS]
    rows += [fields[i:i + 2] for i in range(0, len(fields), 2)]
    rows.append([btn("🧹 Reset All", "m:reset", "danger"), btn("⚙️ Settings", "s:main")])
    rows.append([btn("❌ Close", "x:close", "danger")])
    return text, kb(rows)


# --------------------------------------------------------------------------- #
# Queue / admin / groups / stats
# --------------------------------------------------------------------------- #
def queue_panel(uid, is_admin: bool):
    tasks = sorted((t for t in TASKS.values() if t.batch is None and (is_admin or t.user_id == uid)), key=lambda t: t.seq)
    batches = sorted((b for b in BATCHES.values() if not b.finished and (is_admin or b.user_id == uid)), key=lambda b: b.seq)
    if not tasks and not batches:
        return "📋 <b>Queue</b>\n<blockquote>No active files right now.</blockquote>", kb(
            [[btn("🔄 Refresh", "q:refresh", "primary"), btn("❌ Close", "x:close", "danger")]]
        )
    lines, rows = [], []
    for batch in batches[:5]:
        finished = sum(1 for t in batch.tasks if t.result)
        lines.append(f"📦 <b>Batch #{batch.batch_id}</b> - {finished}/{batch.total} done, {batch.waiting()} waiting")
        rows.append([btn(f"🛑 Cancel batch #{batch.batch_id}", f"b:cancel:{batch.batch_id}", "danger")])
    for t in tasks[:10]:
        lines.append(f"<code>#{t.task_id}</code> {STATE_LABELS.get(t.state, t.state)} - <code>{esc(shorten(t.name, 28))}</code>")
    for t in tasks[:6]:
        rows.append([btn(f"❌ Cancel #{t.task_id}", f"t:cancel:{t.task_id}", "danger")])
    text = f"📋 <b>Queue</b> ({len(TASKS)} files)\n<blockquote>{chr(10).join(lines)}</blockquote>"
    rows.append([btn("🔄 Refresh", "q:refresh", "primary"), btn("🛑 Cancel All", "q:all", "danger")])
    rows.append([btn("❌ Close", "x:close", "danger")])
    return text, kb(rows)


def admin_panel(is_owner: bool):
    users = len(db.data["users"])
    auth = db.auth_list()
    text = (
        "🛠 <b>Admin Panel</b>\n"
        f"<blockquote>👥 <b>Users:</b> {users}   🔐 <b>Authorized:</b> {len(auth) + len(config.AUTH_USERS)}\n"
        f"👮 <b>Admins:</b> {len(db.admin_list()) + len(config.ADMIN_USERS)}   💬 <b>Groups:</b> {len(db.chat_list())}\n"
        f"⚙️ <b>Parallel files:</b> {limiter.limit}   🏃 <b>Running:</b> {limiter.active}\n"
        f"💾 <b>Database:</b> {_db_label()}   🌐 <b>Host:</b> {esc(config.PLATFORM)}</blockquote>\n"
        "<i>Tap a number to change how many files run at the same time (the rest wait in the queue - there is no limit "
        "on how many you can send).</i>\n"
        "<code>/auth id</code> · <code>/unauth id</code> · <code>/addgroup</code>"
        + (" · <code>/addadmin id</code>" if is_owner else "")
    )
    numbers = [
        btn(f"✅ {n}" if limiter.limit == n else str(n), f"a:conc:{n}", "success" if limiter.limit == n else "primary")
        for n in range(1, config.MAX_PARALLEL + 1)
    ]
    rows = [numbers[i:i + 4] for i in range(0, len(numbers), 4)]
    rows.append([btn("📊 Stats", "a:stats", "primary"), btn("💬 Groups", "a:groups", "primary")])
    if is_owner:
        rows.append([btn("👮 Admins", "a:admins", "primary"), btn("♻️ Restart", "a:restart", "danger")])
    rows.append([btn("❌ Close", "x:close", "danger")])
    return text, kb(rows)


def groups_panel():
    chats = db.chat_list()
    if not chats:
        text = ("💬 <b>Groups</b>\n<blockquote>No group yet.\n1. Add me to your group\n2. Send <code>/addgroup</code> there "
                "(as an admin of this bot)\n3. In @BotFather: <code>/setprivacy</code> ➜ <b>Disable</b> "
                "(or make me a group admin) so I can see files.</blockquote>")
        return text, kb([[btn("🔙 Back", "a:main"), btn("❌ Close", "x:close", "danger")]])
    lines, rows = [], []
    for chat_id, info in list(chats.items())[:10]:
        who = "everyone" if info.get("members", True) else "admins only"
        lines.append(f"💬 <b>{esc(shorten(info.get('title') or chat_id, 28))}</b>\n<code>{chat_id}</code> · {who}")
        label = "👥 Everyone" if info.get("members", True) else "🔒 Admins only"
        rows.append([btn(label, f"a:gm:{chat_id}", "primary"), btn("🗑 Remove", f"a:gdel:{chat_id}", "danger")])
    text = "💬 <b>Groups</b>\n<blockquote>" + "\n".join(lines) + "</blockquote>\n<i>Tap the first button to choose who may send files in that group.</i>"
    rows.append([btn("🔙 Back", "a:main"), btn("❌ Close", "x:close", "danger")])
    return text, kb(rows)


def admins_panel():
    admins = sorted(set(db.admin_list()) | set(config.ADMIN_USERS))
    lines = [f"👑 <code>{config.OWNER_ID}</code> (owner)"] + [f"👮 <code>{a}</code>" for a in admins]
    text = (
        "👮 <b>Admins</b>\n"
        f"<blockquote>{chr(10).join(lines)}</blockquote>\n"
        "<b>Admins can:</b> use /auth /unauth /addgroup /removegroup /stats and the admin panel.\n"
        "<b>Only you can:</b> add / remove admins and restart.\n"
        "<code>/addadmin id</code> · <code>/removeadmin id</code>"
    )
    return text, kb([[btn("🔙 Back", "a:main"), btn("❌ Close", "x:close", "danger")]])


def group_added_text():
    return (
        "👋 <b>Hello! I am an auto-rename bot.</b>\n"
        "<blockquote>An admin of this bot must send <code>/addgroup</code> here to activate me in this group.\n"
        "Then everyone in the group can send files and I rename them.</blockquote>\n"
        "<i>If I do not see files: make me a group admin or disable privacy in @BotFather.</i>"
    )


def _read_int(path: str):
    try:
        with open(path, encoding="utf-8") as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return None


def _stat_value(path: str, key: str) -> int:
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                name, _, value = line.partition(" ")
                if name == key:
                    return int(value)
    except (OSError, ValueError):
        pass
    return 0


def _container_memory():
    """Return (used, limit) bytes of THIS container (cgroup v2 / v1), or None when unlimited.

    psutil would show the whole host machine, which is misleading on Render / Docker.
    Page cache is excluded, like ``docker stats`` does.
    """
    layouts = (
        ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.stat", "inactive_file"),
        ("/sys/fs/cgroup/memory/memory.limit_in_bytes", "/sys/fs/cgroup/memory/memory.usage_in_bytes",
         "/sys/fs/cgroup/memory/memory.stat", "total_inactive_file"),
    )
    for limit_path, used_path, stat_path, cache_key in layouts:
        limit, used = _read_int(limit_path), _read_int(used_path)  # "max" -> None (no limit)
        if limit and used is not None and limit < (1 << 50):
            return max(0, used - _stat_value(stat_path, cache_key)), limit
    return None


def _folder_size(folder) -> int:
    try:
        return sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())
    except OSError:
        return 0


def stats_panel():
    stats = db.data["global"]["stats"]
    lines = [
        f"⏱ <b>Uptime:</b> {uptime()}",
        f"📦 <b>Done:</b> {stats['done']}   ❌ <b>Failed:</b> {stats['failed']}",
        f"📤 <b>Processed:</b> {format_bytes(stats['bytes'])}",
        f"🏃 <b>Active:</b> {len(TASKS)}   👥 <b>Users:</b> {len(db.data['users'])}",
    ]
    memory = _container_memory()
    if memory:
        used, limit = memory
        lines.append(f"🧠 <b>RAM:</b> {format_bytes(used)} / {format_bytes(limit)} ({used * 100 / limit:.0f}%)")
    else:
        try:
            import psutil

            lines.append(f"🧠 <b>RAM:</b> {psutil.virtual_memory().percent:.0f}% (host)")
        except Exception:
            pass
    lines.append(f"💽 <b>Temp files:</b> {format_bytes(_folder_size(config.DOWNLOAD_DIR))}   "
                 f"free disk: {format_bytes(shutil.disk_usage(str(config.DOWNLOAD_DIR)).free)}")
    lines += [
        f"💾 <b>Database:</b> {_db_label()}",
        f"🌐 <b>Host:</b> {esc(config.PLATFORM)}   🎨 <b>Buttons:</b> {'native' if 'native' in COLOR_MODE else 'emoji'}",
        f"🔖 <b>Version:</b> {config.VERSION}   🐍 PID {os.getpid()}",
    ]
    text = f"📊 <b>Bot Stats</b>\n<blockquote>{chr(10).join(lines)}</blockquote>"
    return text, kb([[btn("🔄 Refresh", "a:stats", "primary"), btn("🛠 Admin", "a:main")],
                     [btn("❌ Close", "x:close", "danger")]])
