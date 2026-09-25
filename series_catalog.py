"""Channel series catalogs.

Exposes groups of videos from a Telegram channel as a single Stremio series,
with one episode per date, newest first. Used for daily shows that have no
IMDb entry (e.g. "El Sótano CLUB", uploaded every weekday by the bot as
"El Sotano CLUB 2026-09-24.mp4" with the YouTube link in the caption).

Configured with SERIES_CHANNELS, a JSON list:

    [{"id": "sotano", "channel": -1001234567890, "name": "El Sótano CLUB",
      "match": "El Sotano CLUB", "query": "Sótano",
      "poster": "", "background": "", "description": ""}]

- id:      catalog/meta id suffix -> "tgcat_sotano"
- channel: channel to read (it can be shared with other content)
- match:   only files whose name starts with this text (case-insensitive)
           and contains a YYYY-MM-DD date become episodes
- query:   Telegram search text used to find them (default: name)

Episode ids are regular "tgfile_<chat>_<msg>" ids, so the existing stream
handler plays them with no changes.
"""
import json
import logging
import re
import time
from datetime import date

from config import Config
from tg_client import tg_client_manager

logger = logging.getLogger(__name__)

CATALOG_PREFIX = "tgcat_"
CACHE_TTL = 600  # new episodes show up within 10 minutes

DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
YOUTUBE_RE = re.compile(r"(?:youtu\.be/|[?&]v=)([\w-]{11})")
MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
          "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
WEEKDAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

_cache: dict = {}


def _load() -> dict:
    raw = (Config.SERIES_CHANNELS or "").strip()
    if not raw:
        return {}
    try:
        items = json.loads(raw)
    except Exception as e:
        logger.error(f"SERIES_CHANNELS is not valid JSON: {e}")
        return {}
    series = {}
    for item in items:
        sid = str(item.get("id", "")).strip()
        channel = item.get("channel")
        if not sid or not channel or not item.get("name"):
            logger.warning(f"Skipping incomplete SERIES_CHANNELS entry: {item}")
            continue
        if isinstance(channel, str) and channel.lstrip("-").isdigit():
            channel = int(channel)
        meta_id = f"{CATALOG_PREFIX}{sid}"
        series[meta_id] = {**item, "channel": channel, "meta_id": meta_id}
    return series


SERIES = _load()


def manifest_catalogs() -> list:
    return [{"type": "series", "id": meta_id, "name": s["name"]} for meta_id, s in SERIES.items()]


def _base_meta(s: dict) -> dict:
    return {
        "id": s["meta_id"],
        "type": "series",
        "name": s["name"],
        "poster": s.get("poster") or None,
        "background": s.get("background") or None,
        "description": s.get("description") or None,
    }


def catalog_metas(catalog_id: str) -> list:
    s = SERIES.get(catalog_id)
    return [_base_meta(s)] if s else []


def long_date(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()].capitalize()} {d.day} de {MONTHS[d.month - 1]} de {d.year}"


def build_videos(messages: list, match: str) -> list:
    """One episode per date (the latest upload wins), newest first.
    Season = year, episode = order by date within that year."""
    by_date = {}
    for msg in messages:
        media = msg.video or msg.document
        if not media:
            continue
        name = getattr(media, "file_name", None) or ""
        if match and not name.lower().startswith(match.lower()):
            continue
        m = DATE_RE.search(name)
        if not m:
            continue
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        if d not in by_date or msg.id > by_date[d].id:
            by_date[d] = msg

    videos = []
    counters = {}
    for d in sorted(by_date):
        msg = by_date[d]
        counters[d.year] = counters.get(d.year, 0) + 1
        yt = YOUTUBE_RE.search(msg.caption or "")
        videos.append({
            "id": f"tgfile_{msg.chat.id}_{msg.id}",
            "title": long_date(d),
            "season": d.year,
            "episode": counters[d.year],
            "released": f"{d.isoformat()}T00:00:00.000Z",
            "thumbnail": f"https://i.ytimg.com/vi/{yt.group(1)}/maxresdefault.jpg" if yt else None,
            "overview": (msg.caption or "").split("\n")[0] or None,
        })
    videos.reverse()
    return videos


async def series_meta(meta_id: str) -> dict | None:
    s = SERIES.get(meta_id)
    if not s:
        return None

    cached = _cache.get(meta_id)
    if cached and time.time() - cached[0] < CACHE_TTL:
        return cached[1]

    if not tg_client_manager.is_running:
        await tg_client_manager.start()

    messages = []
    try:
        async for msg in tg_client_manager.client.search_messages(
            chat_id=s["channel"], query=s.get("query") or s["name"], limit=2000
        ):
            messages.append(msg)
    except Exception as e:
        logger.error(f"Series catalog search failed for {meta_id}: {e}")
        # Better stale episodes than an empty page; never cache the failure.
        return cached[1] if cached else {**_base_meta(s), "videos": []}

    videos = build_videos(messages, s.get("match") or "")
    meta = _base_meta(s)
    meta["videos"] = videos
    if videos:
        meta["releaseInfo"] = f"{min(v['season'] for v in videos)}-"  # ongoing show
    _cache[meta_id] = (time.time(), meta)
    return meta
