"""Rename engine: understands almost every way people write quality, season and episode.

Quality   : 1080p, 1080 p, q144, Q 720, 1920x1080, [1080], (720), 4k / 2k, FHD, UHD, HD, SD
            and - when nothing else explains it - a bare 144/240/360/480/720/1080/1440/2160.
Season    : S01, S1, Season 1, season_2, 2nd season, 2x08
Episode   : E05, EP05, Ep.05, Episode 5, episode_1, - 05, #05, S01E05, 2x08
Any order, any separator ( space  .  _  - ). Underscores are treated as spaces.

Pure functions only (no Telegram / ffmpeg here) so everything is unit-testable.
"""
import os
import re
from datetime import datetime

import config

KNOWN_EXTS = config.VIDEO_EXTS | config.AUDIO_EXTS | {".srt", ".ass", ".zip", ".rar", ".7z", ".pdf", ".apk"}

# --------------------------------------------------------------------------- #
# Patterns
# --------------------------------------------------------------------------- #
# Season + episode written together:  S01E05  S1 E5  S01.E05  Season 1 Episode 5
_SE = re.compile(
    r"(?i)\bS(?:eason)?[\s.\-]?(\d{1,2})[\s.\-]*E(?:p(?:isode)?)?[\s.\-]*(\d{1,4})(?:v\d)?\b"
)
# 2x08  (first number has only 1-2 digits, so 1920x1080 is never read as season/episode)
_NXN = re.compile(r"(?i)\b(\d{1,2})x(\d{2,3})\b")
# Season on its own:  Season 2 | S2 | 2nd season
_SEASON = re.compile(
    r"(?i)(?:\b(?:season|saison|temporada)[\s.\-]*(\d{1,2})\b)"
    r"|(?:\bS[\s.\-]?(\d{1,2})\b)"
    r"|(?:\b(\d{1,2})(?:st|nd|rd|th)[\s.\-]+season\b)"
)
# Episode on its own:  Episode 5 | Ep.05 | E05 | #05 | " - 05 "
_EPISODE = re.compile(
    r"(?i)(?:\b(?:episode|epis|ep|e)[\s.\-#]*(\d{1,4})(?:v\d)?\b)"
    r"|(?:(?<![A-Za-z0-9])#\s?(\d{1,4})\b)"
    r"|(?:\s-\s(\d{1,4})(?:v\d)?(?=[\s.\[(]|$))"
)

_SEP = r"(?<![A-Za-z0-9])"
_END = r"(?![A-Za-z0-9])"
_Q_P = re.compile(rf"(?i){_SEP}(\d{{3,4}})\s?p{_END}")                       # 1080p / 1080 p
_Q_Q = re.compile(rf"(?i){_SEP}q[\s.\-]?(\d{{3,4}})p?{_END}")                  # q144 / Q 720 / q 480p
_Q_WH = re.compile(rf"(?i){_SEP}(\d{{3,4}})\s?[x\u00d7]\s?(\d{{3,4}}){_END}")  # 1920x1080
_Q_K = re.compile(rf"(?i){_SEP}([248])\s?k{_END}")                            # 4k / 2k / 8k
_Q_BR = re.compile(r"[\[(]\s*(\d{3,4})\s*p?\s*[\])]")                         # [1080] (720p)
_Q_WORD = re.compile(rf"(?i){_SEP}(full[\s.\-]?hd|fhd|qhd|uhd|hd|sd){_END}")  # FHD / HD / SD
_Q_BARE = re.compile(rf"{_SEP}(144|240|360|480|540|576|720|1080|1440|2160|4320){_END}")
_RES = {144, 240, 360, 480, 540, 576, 720, 1080, 1440, 2160, 4320}
_WORDS = {"fullhd": "1080p", "fhd": "1080p", "qhd": "1440p", "uhd": "4k", "hd": "720p", "sd": "480p"}

_YEAR = re.compile(r"\b((?:19|20)\d{2})\b")
_CODEC = re.compile(r"(?i)\b(x264|x265|h[\s.]?264|h[\s.]?265|hevc|av1|xvid)\b")
_JUNK = re.compile(
    r"(?i)\b(web[ .-]?dl|web[ .-]?rip|blu[ .-]?ray|brrip|bdrip|hdrip|hdtv|dvdrip|hdcam|10bit|8bit|"
    r"aac(?:2\.0)?|ac3|eac3|ddp?[0-9.]*|dts|atmos|dual[ .-]?audio|multi[ .-]?audio|e?subs?)\b"
)
_BRACKETS = re.compile(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}")
_VARIABLE = re.compile(r"\{(title|season|episode|quality|year|codec|original)\}", re.I)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def split_name(name: str):
    """Split ``name`` into (stem, extension) - only real file extensions are treated as such."""
    stem, ext = os.path.splitext(name)
    if ext.lower() in KNOWN_EXTS:
        return stem, ext
    return name, ""


def _cut(text: str, match) -> str:
    """Replace exactly the matched span with a space."""
    return text[: match.start()] + " " + text[match.end():]


def _first_group(match) -> str:
    return next((g for g in match.groups() if g), "")


def _normalize_quality(kind: str, match) -> str:
    if kind == "wh":  # 1920x1080 -> use the smaller side (height)
        a, b = int(match.group(1)), int(match.group(2))
        return f"{min(a, b)}p"
    value = match.group(1)
    if kind == "k":
        return f"{value}k"
    if kind == "word":
        return _WORDS.get(re.sub(r"[\s.\-]", "", value.lower()), value.lower())
    return f"{int(value)}p"


def _find_quality(work: str, bare_quality: bool, has_episode: bool):
    """Return (quality, work_without_quality_tokens)."""
    quality = ""
    for pattern, kind in ((_Q_P, "p"), (_Q_Q, "q"), (_Q_WH, "wh"), (_Q_K, "k"), (_Q_BR, "br"), (_Q_WORD, "word")):
        match = pattern.search(work)
        if not match:
            continue
        if kind in ("q", "br") and int(match.group(1)) not in _RES and kind == "q":
            continue
        if kind == "br" and int(match.group(1)) not in _RES:
            continue
        quality = _normalize_quality("p" if kind == "br" else kind, match)
        break

    # Remove EVERY explicit quality token from the text.
    for pattern in (_Q_Q, _Q_P, _Q_WH, _Q_K, _Q_WORD):
        work = pattern.sub(" ", work)

    # Bare numbers (e.g. "1080"): only when something explains them, or the user allows guessing.
    evidence = bool(quality) or has_episode
    if bare_quality or evidence:
        bare = _Q_BARE.search(work)
        if bare:
            if not quality:
                quality = f"{int(bare.group(1))}p"
            work = _Q_BARE.sub(" ", work)
    return quality, work


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def parse_media_info(original: str, remove_words=(), bare_quality: bool = True) -> dict:
    stem, _ = split_name(original)
    work = stem
    for word in remove_words:
        if word.strip():
            work = re.sub(re.escape(word.strip()), " ", work, flags=re.I)
    work = work.replace("_", " ")  # underscore is a word character for regex - make it a separator

    season = episode = ""
    match = _SE.search(work)
    if match:
        season, episode = match.group(1), match.group(2)
        work = _cut(work, match)
    else:
        match = _NXN.search(work)
        if match:
            season, episode = match.group(1), match.group(2)
            work = _cut(work, match)
        else:
            match = _SEASON.search(work)
            if match:
                season = _first_group(match)
                work = _cut(work, match)
            match = _EPISODE.search(work)
            if match:
                episode = _first_group(match)
                work = _cut(work, match)

    quality, work = _find_quality(work, bare_quality, bool(episode))

    def first(pattern) -> str:
        found = pattern.search(work)
        return re.sub(r"[\s.]", "", found.group(1).lower()) if found else ""

    codec = first(_CODEC)
    years = [y for y in _YEAR.findall(work) if int(y) <= datetime.now().year + 1]
    year = years[-1] if years else ""  # the release year is normally the LAST number ("Blade Runner 2049 2017")

    title = work
    for pattern in (_CODEC, _JUNK, _BRACKETS):
        title = pattern.sub(" ", title)
    title = title.replace(".", " ")
    title = re.sub(r"\s+-\s+|^[\s\-]+|[\s\-]+$", " ", title)

    def tidy(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    without_year = title
    if year:  # cut only the last occurrence of the year
        hits = list(re.finditer(rf"\b{year}\b", title))
        if hits:
            last = hits[-1]
            without_year = title[: last.start()] + " " + title[last.end():]
    title = tidy(without_year) or tidy(title)  # keep the year if the title IS a number ("2012")
    return {
        "title": title,
        "season": season.zfill(2) if season else "",
        "episode": episode.zfill(2) if episode else "",
        "quality": quality,
        "year": year,
        "codec": codec,
        "original": stem,
    }


def sort_key(name: str, arrival: int = 0):
    """Order files of a batch: numbered files by season, then episode; files without any number last."""
    info = parse_media_info(name)
    if not info["season"] and not info["episode"]:
        return (1, 0, 0, arrival)
    try:
        season = int(info["season"] or 0)
        episode = int(info["episode"]) if info["episode"] else 10 ** 6
    except ValueError:
        return (1, 0, 0, arrival)
    return (0, season, episode, arrival)


def has_variables(fmt: str) -> bool:
    return bool(_VARIABLE.search(fmt))


def apply_format(fmt: str, info: dict) -> str:
    template = fmt
    if not info["season"] and not info["episode"]:
        template = re.sub(r"(?i)S?\{season\}[\s._-]*E?\{episode\}", "", template)
    if not info["season"]:
        template = re.sub(r"(?i)\bS\{season\}", "", template)
    if not info["episode"]:
        template = re.sub(r"(?i)\bEP?\{episode\}", "", template)
    result = _VARIABLE.sub(lambda m: info.get(m.group(1).lower(), ""), template)
    result = re.sub(r"\(\s*\)|\[\s*\]|\{\s*\}", "", result)
    result = re.sub(r"\s{2,}", " ", result)
    return re.sub(r"[\s\-_.]+$", "", result).strip()


def sanitize_filename(name: str, max_bytes: int = 200) -> str:
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "-", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    stem, ext = os.path.splitext(name)
    while len((stem + ext).encode("utf-8")) > max_bytes and stem:
        stem = stem[:-1]
    return (stem + ext) or "file"


def build_new_name(original: str, fmt: str, remove_words=(), custom_name: str = None,
                   bare_quality: bool = True) -> str:
    """Return the final file name (with extension) for ``original``."""
    _, ext = split_name(original)
    ext = ext or ".mp4"
    if custom_name:
        stem = custom_name
    elif has_variables(fmt):
        stem = apply_format(fmt, parse_media_info(original, remove_words, bare_quality))
    else:
        stem = fmt
    if stem.lower().endswith(ext.lower()):
        stem = stem[: -len(ext)]
    return sanitize_filename(stem.strip() or "file") + ext
