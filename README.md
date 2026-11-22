# Auto-Rename Bot v3.1

Telegram auto-rename / leech bot (Pyrogram, aria2, ffmpeg) with colourful inline buttons, a pro settings
panel, a clean batch progress UI and ready-to-use deployment files for a **VPS** (Docker or systemd) and **Render**.

## What's new in v3.1

| Feature | What it does |
|---|---|
| **Smarter file-name reader** | Understands almost every way people write quality, season and episode (table below) |
| **Manual rename** | Choose *Rename: Manual* in `/settings` and the bot asks you for the new name of every file. Or reply to a file with `/rename New Name` |
| **Old metadata wipe** | Removes the uploader's old tags, comments, track titles, chapters and cover, then writes **your** metadata. Audio / subtitle languages and fonts are kept |
| **HD cover + thumbnail** | Normal 320 px thumbnail **and** a big HD cover (up to 1280 px) on top of the video |
| **Admins** | The owner can make friends *admins* (`/addadmin`) without giving away owner rights |
| **Groups** | Activate the bot in a group with `/addgroup`; members send files and get them renamed |
| **Unlimited batches** | Send as many files as you like. They are sorted by episode number and shown in **one** progress panel |
| **New progress UI** | Step tracker (Download ▸ Process ▸ Upload), bar, speed, ETA, batch counter |
| **Old data is never removed** | New settings are only *added* with safe defaults. Existing users, formats, metadata and thumbnails stay untouched |

## Rename formats it understands

Quality: `1080p`, `1080 p`, `q144`, `Q 720`, `1920x1080`, `[1080]`, `(720p)`, `4k`, `FHD`, `UHD`, `HD`, `SD` and
(when nothing else explains it) a lone `144 / 240 / 360 / 480 / 720 / 1080 / 1440 / 2160`.
Season: `S01`, `S1`, `Season 1`, `season_2`, `2nd season`, `2x08`.
Episode: `E05`, `EP05`, `Ep.05`, `Episode 5`, `episode_1`, ` - 05`, `#05`, `S01E05`, `2x08`.
Any order and any separator (space, `.`, `_`, `-`).

With the format `[MyChannel] {title} S{season}E{episode} {quality}`:

| Original name | Result |
|---|---|
| `Naruto q144 1080.mkv` | `[MyChannel] Naruto 144p.mkv` |
| `Naruto season 1 Episode 1.mkv` | `[MyChannel] Naruto S01E01.mkv` |
| `Naruto episode_1_season_2_.mkv` | `[MyChannel] Naruto S02E01.mkv` |
| `Bleach EP 07 480.mp4` | `[MyChannel] Bleach E07 480p.mp4` |
| `Spy x Family 2x08 360p.mkv` | `[MyChannel] Spy x Family S02E08 360p.mkv` |
| `Jujutsu Kaisen - 12 [720p].mkv` | `[MyChannel] Jujutsu Kaisen E12 720p.mkv` |
| `Attack on Titan Episode 12 q720.mkv` | `[MyChannel] Attack on Titan E12 720p.mkv` |
| `One Piece S01E05 FHD.mkv` | `[MyChannel] One Piece S01E05 1080p.mkv` |
| `Demon Slayer Season 2 Ep 11 1080.mkv` | `[MyChannel] Demon Slayer S02E11 1080p.mkv` |

Format variables: `{title} {season} {episode} {quality} {year} {codec} {original}`. Missing values vanish cleanly.
Set your format with `/autorename`. If a show has a lone number that is really part of the title
(for example *One Piece 1080*), switch **Lone number = quality** off (`/settings` → Advanced).

## Commands

**Everyone allowed**

| Command | Purpose |
|---|---|
| `/start`, `/help` | Welcome and help |
| `/settings` | Settings panel (mode, audio, subtitles, thumbnail, HD cover, rename mode, wipe old metadata, caption, advanced) |
| `/metadata` | Edit your 15 metadata tags, master ON/OFF, wipe-old switch |
| `/autorename` | Set the rename format with live preview |
| `/rename New Name` | Reply to a file to rename it right now (in private chat `/rename` alone asks for the name) |
| `/thumb` | Thumbnail / HD cover settings. Send a picture to set it |
| `/l link` or `/l link \| Name` | Leech a direct link with live aria2 progress |
| `/queue`, `/cancel` | See and cancel your tasks |
| `/ping` | Latency |

**Admins** (owner + friends the owner promoted)

| Command | Purpose |
|---|---|
| `/admin`, `/stats` | Admin panel and statistics, parallel files 1-8 |
| `/auth user_id`, `/unauth user_id` | Allow / block a user |
| `/addgroup`, `/removegroup` | Activate / deactivate a group (send inside the group, or `/addgroup -100...` in private) |

**Owner only**

| Command | Purpose |
|---|---|
| `/addadmin user_id`, `/removeadmin user_id`, `/admins` | Manage admins |
| `/restart` | Restart the bot |

## How things work

### Thumbnail and HD cover
Telegram limits a thumbnail to 320 px / 200 kB, so the bot sends **two pictures**:
1. a sharp **320 px thumbnail** (highest JPEG quality that fits), and
2. a large **HD cover** (default 1280 px, `COVER_SIZE`) shown on the video.

Use a custom picture (send it to the bot; **send it as a File** for the sharpest result) or let the bot take a
frame from the video. If the installed library or the viewer's Telegram app does not support covers, the file is still
uploaded with its normal thumbnail. Covers work in *Video* upload mode (Telegram ignores them for documents).

### Old metadata wipe
With **Wipe old meta: ON** (default) the bot removes the original file's global tags, per-track titles, comments,
chapters, embedded cover and encoder notes, then writes your metadata. Audio / subtitle **languages**, default/forced
flags and embedded **fonts** are kept so subtitles still render. Switch **Keep chapters** on (`/settings` → Advanced) if you want chapters kept.
Switch the wipe OFF to get the old behaviour.

### Batches without limits
Send or forward as many files as you want, even a whole season at once. After a short wait the bot sorts them by
season/episode, shows **one** panel (`3/20 done`) and processes them in order. Cancel one file or the whole batch from
the panel. There is no limit on the number of files; each file can be up to 2 GB (Telegram limit) and the speed depends on
your server. Raise *Parallel files* in `/admin` on a strong VPS.

### Admins and groups
* Owner: everything. Admins: `/auth`, `/unauth`, `/addgroup`, `/stats`, admin panel. Admins cannot add other admins or restart the bot.
* In an activated group every member can send files. The group uses the **settings of the admin who activated it**.
  Strangers in groups that are not activated (and strangers in private chat) are ignored.
* For the bot to see files in a group: make it a **group admin**, or open @BotFather → `/setprivacy` → *Disable*.

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `API_ID`, `API_HASH` | yes | from https://my.telegram.org |
| `BOT_TOKEN` | yes | from @BotFather |
| `OWNER_ID` | yes | your numeric Telegram id |
| `ADMIN_USERS` | no | extra admins from the first start, comma separated |
| `AUTH_USERS` | no | extra allowed users, comma separated |
| `BOT_NAME`, `WELCOME_PIC`, `DEFAULT_RENAME_FORMAT` | no | branding and default format |
| `MAX_CONCURRENT_TASKS` | no | parallel files at start (default 1, changeable live in `/admin`) |
| `MAX_PARALLEL` | no | upper limit of the parallel buttons (default 8) |
| `COVER_SIZE` | no | HD cover size in px (default 1280) |
| `MONGO_URI` | **Render Free: yes** | MongoDB connection string (free Atlas cluster works) |
| `DATA_DIR`, `DOWNLOAD_DIR` | no | where `database.json` / temp files live |
| `KEEP_ALIVE`, `APP_URL`, `PORT` | no | Render Free keep-alive and web server |

Full list with comments: `.env.example`.

## Deploy on a VPS (recommended)

**Option A - Docker**

```bash
git clone <your-repo> autorename-bot && cd autorename-bot
cp .env.example .env && nano .env      # fill API_ID, API_HASH, BOT_TOKEN, OWNER_ID
docker compose up -d --build
docker compose logs -f
```

Data lives in `./data` (survives updates and rebuilds). Update later with `git pull && docker compose up -d --build`.

**Option B - systemd, no Docker (Ubuntu / Debian)**

```bash
sudo bash deploy/vps_install.sh
sudo nano /opt/autorename-bot/.env
sudo systemctl restart autorename-bot
sudo journalctl -u autorename-bot -f
```

Re-running the installer updates the code but never overwrites `.env` or `data/`.

## Deploy on Render

1. Push the folder to GitHub.
2. Render → **New → Web Service** → pick the repo → **Runtime: Docker**.
3. Environment: `API_ID`, `API_HASH`, `BOT_TOKEN`, `OWNER_ID`, `MONGO_URI`, `KEEP_ALIVE=true`. Health check path: `/health`.

Render Free wipes its disk on every restart, which is why `MONGO_URI` is required there. Keep `MAX_CONCURRENT_TASKS=1`
and avoid very large files on the free plan. On a paid plan attach a disk at `/data` and set `DATA_DIR=/data`,
`DOWNLOAD_DIR=/data/downloads`.

## Your data is safe

* Settings are only ever **added**. Old users keep their format, metadata, thumbnail, words and statistics; new options
  appear with safe defaults. Old `database.json` files (v1, v2, v3.0) are migrated automatically.
* `DOWNLOAD_DIR` can never be the data folder, so cleaning temporary files at start-up cannot touch `database.json`.
* The previous `database.json` is kept as `database.bak` on every save.

## Important notes

* Run **only one instance per bot token**. Use a separate bot token for each deployment.
* This bot needs `pyrotgfork` (provides colourful buttons and the video cover). Never install it together with `pyrofork` or
  `pyrogram`; in an old environment run `pip uninstall -y pyrofork pyrogram` first.
* Only rename and process files you own or have permission to share.
