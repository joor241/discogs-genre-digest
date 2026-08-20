#!/usr/bin/env python3
"""
Daily Discogs record-store genre digest.

Checks a list of Discogs marketplace sellers for items listed in the last
LOOKBACK_HOURS, keeps the ones whose release genres/styles match
GENRES_INCLUDE, and emails an HTML digest.

Stateless by design: "new" means "posted inside the lookback window", so there
is no database and nothing to commit back to the repo. The window is
deliberately longer than 24h, which means a listing posted very late in the day
can show up in two consecutive digests. That is an accepted, documented
trade-off -- see README.md.

Every setting in the CONFIG block can be overridden by an environment variable,
so genres, sellers and the lookback window can be changed from the GitHub web UI
(repo Variables, or the "Run workflow" form) without editing this file.
"""

from __future__ import annotations

import argparse
import html
import logging
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from urllib.parse import quote_plus

import requests

# ---------------------------------------------------------------------------
# CONFIG -- edit these, or override them with environment variables
# ---------------------------------------------------------------------------

# Discogs seller username -> display name used in the email.
# The key must be the store's Discogs SELLER USERNAME, not their website.
# Find it in the URL of their shop page: discogs.com/seller/<username>/profile
# Verified against the live API on 2026-08-18.
SELLERS = {
    "RushHour": "Rush Hour",             # rushhour.nl
    "clone.nl": "Clone",                 # clone.nl
    "offbeat__records": "Offbeat Records",  # note: two underscores
}

# Genres/styles to keep, matched whole-word and case-insensitively against the
# release's genres + styles. "house" also catches "Deep House" / "Acid House" /
# "Tech House"; "techno" also catches "Dub Techno" / "Minimal Techno"; and
# punctuation is ignored, so "italo" catches "Italo-Disco".
#
# Because matching is whole-word, "electro" means Electro and Electro House --
# NOT "Electronic". Add or remove freely; empty list = keep everything.
GENRES_INCLUDE = [
    "techno", "house", "electro", "acid",
    "disco", "italo", "new beat", "ebm",
    "breakbeat", "trance",
]

# Formats to keep, matched case-insensitively against the release's format
# names (as Discogs categorises them: "Vinyl", "CD", "Cassette", "File", ...).
# A release can carry more than one -- e.g. "Vinyl" + "File" for a record that
# ships with a download code -- and is kept if ANY of its formats matches.
# Empty list = keep everything.
FORMATS_INCLUDE = ["Vinyl"]

# Per-store genre/style override. A store not mentioned here uses the global
# GENRES_INCLUDE above; a store listed here uses ONLY its own list instead
# (replaces, does not add to, the global one). Give a store's list as
# empty/"all"/"*" via GENRES_BY_STORE to switch off genre filtering for just
# that one store while the rest keep the global filter.
#
# Example: Offbeat mostly stocks trance/new-beat, Clone mostly house --
# GENRES_BY_STORE = {"offbeat__records": ["trance", "new beat"], "clone.nl": ["house"]}
#
# Empty dict (the default) means every store uses the same global filter.
GENRES_BY_STORE: dict[str, list[str]] = {}

# How far back to look, in hours.
#
# The digest is stateless: it asks "was this listed in the last N hours?"
# rather than remembering what it already sent. So this value should roughly
# match how often the workflow runs, plus a couple of hours of buffer.
#
# Widening this makes emails REPEAT rather than find more: anything still
# inside the window on the next run gets sent again.
#
# On the default daily schedule:
#   26  = each record appears exactly once, and nothing is ever missed,
#         because consecutive 26h windows cover every hour of the day.
#   48  = the setting here. Records can appear twice, but a shop that lists
#         every other day still lands in a digest.
#   72+ = measured at 2038 listings on 2026-08-18, because Rush Hour
#         bulk-lists thousands at a time. That blows past
#         MAX_RELEASE_LOOKUPS, so the digest truncates -- and then repeats
#         the same truncated batch for three days running.
#
# For fuller emails without repeats, run less often and keep the two in step,
# e.g. cron "0 7 */3 * *" in the workflow with LOOKBACK_HOURS = 74.
LOOKBACK_HOURS = 48

# ---------------------------------------------------------------------------
# Tunables (rarely need changing)
# ---------------------------------------------------------------------------

API_BASE = "https://api.discogs.com"

# Discogs silently blocks generic user agents (curl/..., python-requests/...).
# Must be distinctive. Override with USER_AGENT once you know your repo URL.
USER_AGENT = "DiscogsGenreDigest/1.0 +https://github.com/joris/discogs-genre-digest"

HTTP_TIMEOUT = 30           # seconds per request
MAX_ATTEMPTS = 4            # attempts per request before giving up
MIN_REQUEST_INTERVAL = 1.1  # seconds between calls -> ~54/min, under the 60/min cap
RATELIMIT_FLOOR = 5         # when this few requests remain in the window...
RATELIMIT_SLEEP = 15        # ...pause this long to let the window roll over
MAX_PAGES = 20              # safety cap on inventory pages per seller per run.
                             # Override with MAX_PAGES to see more than 2000
                             # listings/seller in the lookback window.
PER_PAGE = 100              # max allowed by the API

# How many "listen" links to show per release.
#
# Discogs videos are community-contributed, so a well-known record can carry
# dozens -- one Steve Bug 12" had 56, which would swamp the email. This caps
# the list after deduplication. Override with MAX_VIDEOS_PER_RELEASE.
MAX_VIDEOS_PER_RELEASE = 6

# How many days of dated player pages to keep in the archive directory.
ARCHIVE_KEEP_DAYS = 30

# Hard ceiling on release lookups per run, across all sellers.
#
# This exists because shops bulk-list. Rush Hour was measured dumping 2000+
# records in a single batch; at ~1.1s per lookup that is 37+ minutes, which
# would blow the workflow timeout and produce an unreadable email. When the
# budget runs out the digest is sent with what it has, plus a note saying how
# many listings went unchecked. Override with MAX_RELEASE_LOOKUPS.
MAX_RELEASE_LOOKUPS = 400

LOG = logging.getLogger("digest")


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

def env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def env_int(name: str, default: int) -> int:
    """Read an int from the environment, falling back to the default if unset
    or unparseable (a typo in a repo variable should not kill the run)."""
    raw = os.environ.get(name)
    if not raw or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        LOG.warning("%s=%r is not a whole number - using default %s", name, raw, default)
        return default


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_csv_list(name: str, default: list[str]) -> list[str]:
    """Comma-separated list, e.g. GENRES_INCLUDE="techno,deep house,electro"
    or FORMATS_INCLUDE="Vinyl,CD". Shared by the genre and format filters,
    since both are "comma list, empty means no filtering" in the same way.

    Unset or empty keeps the in-code default -- GitHub Actions passes an empty
    string for variables and inputs that were never set, so empty cannot mean
    "no filtering" here. To actually turn filtering off, set it to "all" or "*".
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return list(default)
    raw = raw.strip()
    if raw.lower() in {"all", "*"}:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def env_sellers(name: str, default: dict[str, str]) -> dict[str, str]:
    """Optional override, format: "username=Display Name, other=Other Name".

    A bare "username" with no "=" uses the username as the display name.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return dict(default)
    sellers: dict[str, str] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        username, _, display = chunk.partition("=")
        username = username.strip()
        if username:
            sellers[username] = display.strip() or username
    return sellers or dict(default)


def env_genres_by_store(name: str, default: dict[str, list[str]]) -> dict[str, list[str]]:
    """Per-seller genre override, format: "user1: techno, house; user2: disco".

    A username not mentioned here is absent from the returned dict, which
    build_digest() reads as "use the global GENRES_INCLUDE filter for this
    store". A username IS present but with an empty list when its terms are
    "all"/"*", which means "no genre filtering for this one store" -- that is
    deliberately a different, distinguishable state from "not overridden".
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return dict(default)
    result: dict[str, list[str]] = {}
    for block in raw.split(";"):
        block = block.strip()
        if not block:
            continue
        username, sep, term_part = block.partition(":")
        username = username.strip()
        if not username or not sep:
            continue
        term_part = term_part.strip()
        if term_part.lower() in {"all", "*"}:
            result[username] = []
        else:
            result[username] = [t.strip() for t in term_part.split(",") if t.strip()]
    return result or dict(default)


# ---------------------------------------------------------------------------
# Discogs API access
# ---------------------------------------------------------------------------

class DiscogsError(RuntimeError):
    """A Discogs request failed after exhausting retries."""


class Discogs:
    """Thin Discogs client: paces requests, retries transient failures, and
    backs off when the live rate-limit headers say the window is nearly spent."""

    def __init__(self, token: str, user_agent: str,
                 lookup_budget: int = MAX_RELEASE_LOOKUPS,
                 video_limit: int = MAX_VIDEOS_PER_RELEASE,
                 max_pages: int = MAX_PAGES) -> None:
        self.session = requests.Session()
        headers = {"User-Agent": user_agent}
        if token:
            headers["Authorization"] = f"Discogs token={token}"
        self.session.headers.update(headers)
        self._last_request_at = 0.0
        self.request_count = 0
        self.lookup_budget = lookup_budget
        self.video_limit = video_limit
        self.max_pages = max_pages
        # release id -> {"genres": [...], "styles": [...], "thumb", "videos"}
        self._release_cache: dict[int, dict] = {}

    def _throttle(self) -> None:
        wait = MIN_REQUEST_INTERVAL - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    @staticmethod
    def _respect_ratelimit(resp: requests.Response) -> None:
        raw = resp.headers.get("X-Discogs-Ratelimit-Remaining")
        if raw is None:
            return
        try:
            remaining = int(raw)
        except ValueError:
            return
        if remaining <= RATELIMIT_FLOOR:
            total = resp.headers.get("X-Discogs-Ratelimit", "?")
            LOG.info(
                "Rate limit nearly spent (%s/%s left) - pausing %ss",
                remaining, total, RATELIMIT_SLEEP,
            )
            time.sleep(RATELIMIT_SLEEP)

    def get(self, path: str, params: dict | None = None) -> dict:
        url = f"{API_BASE}{path}"
        last_error = "unknown error"

        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._throttle()
            try:
                resp = self.session.get(url, params=params, timeout=HTTP_TIMEOUT)
            except requests.RequestException as exc:
                last_error = f"network error: {exc}"
                if attempt == MAX_ATTEMPTS:
                    break
                backoff = 2 ** attempt
                LOG.warning("%s on %s - retrying in %ss", last_error, path, backoff)
                time.sleep(backoff)
                continue

            self.request_count += 1

            # 429: too many requests. Honour Retry-After when Discogs sends it.
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else RATELIMIT_SLEEP
                except ValueError:
                    delay = RATELIMIT_SLEEP
                last_error = "rate limited (HTTP 429)"
                if attempt == MAX_ATTEMPTS:
                    break
                LOG.warning("Rate limited on %s - waiting %ss", path, delay)
                time.sleep(delay)
                continue

            # 5xx: Discogs having a moment. Retry.
            if resp.status_code >= 500:
                last_error = f"server error (HTTP {resp.status_code})"
                if attempt == MAX_ATTEMPTS:
                    break
                backoff = 2 ** attempt
                LOG.warning("%s on %s - retrying in %ss", last_error, path, backoff)
                time.sleep(backoff)
                continue

            # 4xx: our fault (bad username, bad token). Do not retry.
            if resp.status_code >= 400:
                detail = ""
                try:
                    detail = str(resp.json().get("message", ""))[:200]
                except ValueError:
                    detail = resp.text[:200]
                hint = ""
                if resp.status_code == 401:
                    hint = " - check DISCOGS_TOKEN"
                elif resp.status_code == 404:
                    hint = " - check the seller username"
                elif resp.status_code == 403:
                    hint = " - Discogs may be blocking the User-Agent"
                raise DiscogsError(
                    f"HTTP {resp.status_code} on {path}{hint}"
                    + (f": {detail}" if detail else "")
                )

            self._respect_ratelimit(resp)

            try:
                return resp.json()
            except ValueError:
                last_error = "response was not valid JSON"
                if attempt == MAX_ATTEMPTS:
                    break
                LOG.warning("%s on %s - retrying", last_error, path)
                time.sleep(2 ** attempt)
                continue

        raise DiscogsError(f"{path} failed after {MAX_ATTEMPTS} attempts: {last_error}")

    def release_info(self, release_id: int) -> dict | None:
        """Genres, styles and thumbnail for a release, or None if the per-run
        lookup budget is spent.

        Cached, because the same record is often listed by more than one shop
        (or twice by one shop). Cache hits are free and never cost budget.
        """
        cached = self._release_cache.get(release_id)
        if cached is not None:
            return cached
        if self.lookup_budget <= 0:
            return None
        self.lookup_budget -= 1

        data = self.get(f"/releases/{release_id}")
        info = {
            "genres": [g for g in (data.get("genres") or []) if g],
            "styles": [s for s in (data.get("styles") or []) if s],
            "thumb": data.get("thumb") or "",
            # Free: these are the same response we already fetch for genres.
            "videos": extract_videos(data.get("videos"), self.video_limit),
            "formats": [f.get("name") for f in (data.get("formats") or []) if f.get("name")],
        }
        self._release_cache[release_id] = info
        return info


# ---------------------------------------------------------------------------
# Fetching and filtering
# ---------------------------------------------------------------------------

def parse_posted(value: str | None) -> datetime | None:
    """Parse a listing's `posted` timestamp into an aware UTC datetime.

    Discogs sends e.g. "2026-08-18T05:59:45-07:00". We also tolerate a
    trailing "Z" and naive timestamps (assumed UTC) so a format change on
    their side does not crash the run.
    """
    if not value:
        return None
    text = str(value).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def fetch_recent_listings(api: Discogs, username: str, cutoff: datetime):
    """Yield listings posted at or after `cutoff`, newest first.

    Relies on sort=listed&sort_order=desc: as soon as we see something older
    than the cutoff we can stop, so we never page through a whole inventory.
    """
    page = 1
    while page <= api.max_pages:
        data = api.get(
            f"/users/{username}/inventory",
            params={
                "status": "For Sale",
                "sort": "listed",
                "sort_order": "desc",
                "per_page": PER_PAGE,
                "page": page,
            },
        )

        listings = data.get("listings") or []
        if not listings:
            if page == 1:
                LOG.info("[%s] inventory is empty", username)
            return

        for listing in listings:
            posted = parse_posted(listing.get("posted"))
            if posted is None:
                LOG.warning(
                    "[%s] listing %s has no usable 'posted' timestamp - skipped",
                    username, listing.get("id", "?"),
                )
                continue
            if posted < cutoff:
                return
            yield listing

        pagination = data.get("pagination") or {}
        total_pages = pagination.get("pages") or 1
        if page >= total_pages:
            return
        page += 1

    LOG.warning(
        "[%s] stopped at the %d-page safety cap - some new listings may be missing. "
        "Raise MAX_PAGES if this store really lists >%d items in the lookback window.",
        username, api.max_pages, api.max_pages * PER_PAGE,
    )


YOUTUBE_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:[^#]*&)?v=|embed/|v/)|youtu\.be/)([A-Za-z0-9_-]{11})"
)


def youtube_id(url: str | None) -> str | None:
    """Pull the 11-character video id out of a YouTube URL, or None."""
    match = YOUTUBE_ID_RE.search(url or "")
    return match.group(1) if match else None


def extract_videos(raw: list | None, limit: int = MAX_VIDEOS_PER_RELEASE) -> list[dict]:
    """Clean up the release's `videos` array into listenable links.

    Discogs videos are community-contributed, which means two problems worth
    handling: the same clip is frequently listed more than once (deduped here
    by video id, falling back to the raw URL), and popular records can carry
    dozens, so the list is capped.
    """
    videos: list[dict] = []
    seen: set[str] = set()

    for item in raw or []:
        uri = ((item or {}).get("uri") or "").strip()
        if not uri:
            continue
        vid = youtube_id(uri)
        key = vid or uri
        if key in seen:
            continue
        seen.add(key)
        try:
            dur = int(item.get("duration") or 0)
        except (TypeError, ValueError):
            dur = 0
        videos.append({
            "title": ((item.get("title") or "").strip() or "Listen"),
            "uri": uri,
            "yt": vid,
            "dur": max(0, dur),  # seconds, 0 = unknown
        })
        if len(videos) >= limit:
            break
    return videos


def fmt_mmss(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    m, s = divmod(seconds, 60)
    return f"{m}:{s:02d}"


def playlist_url(videos: list[dict]) -> str:
    """A one-click "play the whole record" URL.

    YouTube's watch_videos endpoint builds a throwaway playlist from a list of
    ids, so a single link plays the release back to back. Only worth showing
    when there are at least two YouTube-hosted clips.
    """
    ids = [v["yt"] for v in videos if v.get("yt")]
    if len(ids) < 2:
        return ""
    return "https://www.youtube.com/watch_videos?video_ids=" + ",".join(ids)


def youtube_search_url(description: str) -> str:
    """Fallback for the rare release with no videos attached."""
    return "https://www.youtube.com/results?search_query=" + quote_plus(description)


def normalise_tag(text: str) -> str:
    """Lowercase and reduce anything non-alphanumeric to single spaces, so
    "Italo-Disco", "italo disco" and "Italo Disco" all compare equal."""
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()


def matches_genre(tags: list[str], wanted_norm: list[str]) -> bool:
    """Whole-word match of each wanted term against each genre/style.

    Word-level rather than raw substring, because plain substring matching has
    a nasty failure: "electro" is a substring of "Electronic", and Discogs tags
    nearly every dance record with the genre "Electronic" -- so a substring
    filter containing "electro" would quietly match everything. Here "electro"
    matches "Electro" and "Electro House" but not "Electronic".

    Multi-word terms still work ("new beat", "deep house"), and matching per
    tag rather than against one joined string avoids hits that span two tags.
    """
    if not wanted_norm:
        return True
    # Pad with spaces so " house " matches at the start and end of a tag too.
    haystacks = [f" {normalise_tag(tag)} " for tag in tags if tag]
    return any(f" {want} " in hay for want in wanted_norm for hay in haystacks)


def matches_format(formats: list[str], wanted_lower: list[str]) -> bool:
    """Case-insensitive match against Discogs' own format-name vocabulary
    (Vinyl, CD, Cassette, File, ...). Plain equality is enough here, unlike
    genre matching -- these are a small, clean, fixed set of names rather than
    free-text tags, so there's no "electro"-inside-"Electronic" style trap.
    """
    if not wanted_lower:
        return True
    have = {f.lower() for f in formats if f}
    return any(want in have for want in wanted_lower)


def format_price(listing: dict) -> str:
    price = listing.get("price") or {}
    value = price.get("value")
    currency = price.get("currency") or ""
    if value is None:
        return "price on request"
    try:
        return f"{float(value):.2f} {currency}".strip()
    except (TypeError, ValueError):
        return f"{value} {currency}".strip()


def collect_seller(api: Discogs, username: str, display_name: str, cutoff: datetime,
                   wanted_norm: list[str], wanted_formats: list[str]
                   ) -> tuple[list[dict], int, int]:
    """Return (matching items, listings past the date cutoff, listings left
    unchecked because the release-lookup budget ran out)."""
    matched: list[dict] = []
    considered = 0
    unchecked = 0

    for listing in fetch_recent_listings(api, username, cutoff):
        considered += 1
        release = listing.get("release") or {}
        release_id = release.get("id")
        if not release_id:
            LOG.warning("[%s] listing %s has no release id - skipped",
                        username, listing.get("id", "?"))
            continue

        try:
            info = api.release_info(release_id)
        except DiscogsError as exc:
            # One bad release should not lose the rest of the store.
            LOG.warning("[%s] could not read release %s (%s) - skipped",
                        username, release_id, exc)
            continue

        if info is None:
            # Budget spent. Keep counting so the digest can say how many were
            # missed, but stop spending requests on lookups.
            unchecked += 1
            continue

        tags = info["genres"] + info["styles"]
        if not matches_genre(tags, wanted_norm):
            continue
        if not matches_format(info.get("formats") or [], wanted_formats):
            continue

        matched.append({
            "description": release.get("description") or release.get("title") or "Unknown release",
            "price": format_price(listing),
            "condition": listing.get("condition") or "?",
            "sleeve": listing.get("sleeve_condition") or "",
            "url": listing.get("uri") or f"https://www.discogs.com/sell/item/{listing.get('id', '')}",
            "tags": ", ".join(tags) or "untagged",
            "thumb": info["thumb"],
            "label": release.get("label") or "",
            "catno": release.get("catalog_number") or "",
            "videos": info.get("videos") or [],
            "formats": info.get("formats") or [],
        })

    if unchecked:
        LOG.warning(
            "[%s] %s: %d new listing(s) in window, %d matched, "
            "%d LEFT UNCHECKED (release-lookup budget of %d spent)",
            username, display_name, considered, len(matched), unchecked,
            MAX_RELEASE_LOOKUPS,
        )
    else:
        LOG.info("[%s] %s: %d new listing(s) in window, %d matched the genre filter",
                 username, display_name, considered, len(matched))
    return matched, considered, unchecked


def build_digest(api: Discogs, sellers: dict[str, str], cutoff: datetime,
                 genres: list[str], formats: list[str],
                 genres_by_store: dict[str, list[str]] | None = None
                 ) -> tuple[list[tuple[str, list[dict], str | None]], dict]:
    """Each section is (display_name, matched_items, genre_label).

    genre_label is None when the store used the global genre filter (the
    common case, rendered with no extra note), or a string describing the
    store's own override when genres_by_store applies to it -- so the digest
    can show, right under that store's heading, that it was filtered
    differently rather than leaving that silent.
    """
    genres_by_store = genres_by_store or {}
    default_norm = [n for n in (normalise_tag(g) for g in genres) if n]
    wanted_formats = [f.lower() for f in formats if f]
    sections: list[tuple[str, list[dict], str | None]] = []
    stats = {"considered": 0, "matched": 0, "unchecked": 0, "failed_sellers": []}

    for username, display_name in sellers.items():
        override = genres_by_store.get(username)
        if override is not None:
            seller_norm = [n for n in (normalise_tag(g) for g in override) if n]
            genre_label = ", ".join(override) if override else "everything (no genre filter)"
        else:
            seller_norm = default_norm
            genre_label = None

        LOG.info("Checking %s (%s)%s...", display_name, username,
                 f" [genres: {genre_label}]" if genre_label is not None else "")
        try:
            matched, considered, unchecked = collect_seller(
                api, username, display_name, cutoff, seller_norm, wanted_formats
            )
        except DiscogsError as exc:
            # A dead store must not take the whole digest down.
            LOG.error("[%s] FAILED: %s", username, exc)
            stats["failed_sellers"].append(username)
            continue
        except Exception as exc:  # noqa: BLE001 - last line of defence per seller
            LOG.exception("[%s] unexpected error: %s", username, exc)
            stats["failed_sellers"].append(username)
            continue

        stats["considered"] += considered
        stats["matched"] += len(matched)
        stats["unchecked"] += unchecked
        if matched:
            sections.append((display_name, matched, genre_label))

    return sections, stats


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def listen_html(item: dict, e) -> str:
    """The listen links for one record.

    Email clients strip <script>, <iframe> and <audio>, so playback cannot
    happen inside the message itself. These link out instead: one button that
    plays the whole record, then the individual clips.
    """
    videos = item.get("videos") or []

    if not videos:
        url = youtube_search_url(item["description"])
        return (
            '<div style="margin-top:7px;">'
            f'<a href="{e(url)}" style="color:#777;text-decoration:none;font-size:12px;">'
            '&#9654;&#65038; Search YouTube</a></div>'
        )

    out = ['<div style="margin-top:7px;">']

    playlist = playlist_url(videos)
    if playlist:
        out.append(
            f'<a href="{e(playlist)}" style="display:inline-block;background:#cc0000;'
            'color:#ffffff;text-decoration:none;font-size:12px;font-weight:600;'
            'padding:5px 10px;border-radius:4px;margin:0 6px 4px 0;">'
            f'&#9654;&#65038; Play all {len(videos)}</a>'
        )

    links = []
    for video in videos:
        title = video["title"]
        if len(title) > 42:
            title = title[:41].rstrip() + "…"
        links.append(
            f'<a href="{e(video["uri"])}" style="color:#0b5fff;text-decoration:none;'
            f'font-size:12px;">{e(title)}</a>'
        )
    out.append(
        '<div style="margin-top:3px;color:#bbb;font-size:12px;line-height:1.7;">'
        + ' &middot; '.join(links) + '</div>'
    )

    out.append('</div>')
    return "".join(out)


PLAYER_CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 24px 16px 64px;
  background: #0e0e10; color: #e8e8ea;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  font-size: 15px; line-height: 1.45;
}
.wrap { max-width: 760px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; letter-spacing: -0.01em; }
.sub { color: #8a8a92; font-size: 13px; margin: 0 0 28px; }
h2 {
  font-size: 15px; text-transform: uppercase; letter-spacing: 0.08em;
  color: #8a8a92; margin: 34px 0 12px; padding-bottom: 8px;
  border-bottom: 1px solid #26262b;
}
.rec {
  display: flex; gap: 14px; padding: 14px 0;
  border-bottom: 1px solid #1c1c21;
}
.rec img {
  width: 68px; height: 68px; border-radius: 5px;
  background: #26262b; flex: none; object-fit: cover;
}
.rec .body { min-width: 0; flex: 1; }
.rec .title { font-weight: 600; font-size: 15px; margin-bottom: 2px; }
.rec .title a { color: #e8e8ea; text-decoration: none; }
.rec .title a:hover { color: #6ea8ff; }
.meta { color: #8a8a92; font-size: 12.5px; margin-bottom: 2px; }
.tags { color: #5f5f68; font-size: 12px; margin-bottom: 9px; }
.buy {
  display: inline-block; font-size: 12px; color: #6ea8ff;
  text-decoration: none; margin-left: 10px;
}
.buy:hover { text-decoration: underline; }
ul.tracks { list-style: none; margin: 0; padding: 0; }
li.track { margin: 0 0 9px; }
li.track .tname {
  font-size: 12.5px; color: #9a9aa2; margin: 0 0 3px 37px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.trow { display: flex; align-items: center; gap: 8px; }
button.ppbtn {
  flex: none; width: 28px; height: 28px; border-radius: 50%;
  background: #cc0000; color: #fff; border: 0; cursor: pointer;
  font-size: 11px; display: inline-flex; align-items: center; justify-content: center;
  padding: 0; transition: background .12s, transform .08s;
}
button.ppbtn:hover { background: #e01212; }
button.ppbtn:active { transform: scale(0.94); }
button.ppbtn.loading { background: #4a4a52; }
/* .bar is the seek target: a tall, easy-to-tap hitbox around a thin visual
   line, so it works on a phone without needing a precise touch. */
.bar {
  flex: 1 1 auto; min-width: 0; height: 28px; display: flex; align-items: center;
  cursor: pointer; touch-action: manipulation; -webkit-tap-highlight-color: transparent;
}
.bar .track {
  position: relative; width: 100%; height: 6px; border-radius: 3px;
  background: #26262b; overflow: hidden;
}
.bar .fill {
  position: absolute; left: 0; top: 0; bottom: 0; width: 0%;
  background: #cc0000; border-radius: 3px;
}
.bar:hover .track { background: #303038; }
.bar:focus-visible { outline: 2px solid #6ea8ff; outline-offset: 3px; border-radius: 4px; }
.trow.playing .bar .fill { background: #ff3b3b; }
.time {
  flex: none; font-variant-numeric: tabular-nums; font-size: 11px;
  color: #6f6f78; width: 84px; text-align: right;
}
a.ytlink {
  flex: none; color: #6a6a72; text-decoration: none; font-size: 14px;
  width: 26px; height: 26px; display: inline-flex; align-items: center;
  justify-content: center; border-radius: 5px;
}
a.ytlink:hover { color: #e8e8ea; background: #202027; }
.trow.errored .bar { opacity: .45; cursor: not-allowed; pointer-events: none; }
.trow.errored .time { color: #9a5a5a; }
.none { color: #5f5f68; font-size: 13px; }
footer {
  margin-top: 40px; padding-top: 14px; border-top: 1px solid #1c1c21;
  color: #5f5f68; font-size: 12px;
}
footer a { color: #6ea8ff; }
@media (max-width: 520px) {
  .rec img { width: 52px; height: 52px; }
  .time { width: 66px; font-size: 10.5px; }
  button.ppbtn { width: 32px; height: 32px; }
  .bar { height: 32px; }
}
"""

# One shared, hidden YouTube player is the actual audio engine; every bar is
# just a click target that tells it what to load and where to seek. This is
# the only way to get a Spotify-style scrubber out of YouTube, since a real
# waveform isn't available -- YouTube doesn't expose audio data to embedders,
# cross-origin, so the bar is an even fill rather than a true waveform.
#
# The player is created once, on page load, rather than on first click.
# Creating it lazily would mean the very first tap has to build the iframe
# from scratch before it can play, which is slow AND risks losing the
# "user gesture" browsers require before they'll allow audio to start,
# especially on mobile Safari. Pre-built, a tap only has to call
# loadVideoById/seekTo, which happens synchronously inside the click handler.
PLAYER_JS = """
(function () {
  var player = null, ytReady = false, pendingInit = null;
  var activeBar = null, activeVideoId = null, pendingSeekFraction = null;
  var pollTimer = null;

  function fmt(t) {
    if (!isFinite(t) || t < 0) t = 0;
    var m = Math.floor(t / 60), s = Math.floor(t % 60);
    return m + ':' + (s < 10 ? '0' : '') + s;
  }

  function row(bar) { return bar.closest('.trow'); }

  function paint(bar, fraction, curLabel, durLabel, state) {
    var fillEl = bar.querySelector('.fill');
    if (fillEl) fillEl.style.width = (Math.max(0, Math.min(1, fraction)) * 100) + '%';
    bar.setAttribute('aria-valuenow', Math.round(fraction * 100));
    var r = row(bar);
    if (!r) return;
    r.classList.toggle('playing', state === 'playing');
    var t = r.querySelector('.time');
    if (t) t.textContent = curLabel + ' / ' + durLabel;
    var btn = r.querySelector('.ppbtn');
    if (btn) {
      btn.classList.toggle('loading', state === 'loading');
      btn.textContent = state === 'playing' ? '\\u23F8' : '\\u25B6';
    }
  }

  function knownDuration(bar) {
    return parseInt(bar.getAttribute('data-dur'), 10) || 0;
  }

  function durLabel(bar) {
    var known = knownDuration(bar);
    return known ? fmt(known) : '--:--';
  }

  function resetBar(bar) {
    if (!bar) return;
    paint(bar, 0, '0:00', durLabel(bar), 'idle');
  }

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  function poll() {
    if (!player || !activeBar) return;
    var dur = 0, cur = 0, state = -1;
    try {
      dur = player.getDuration() || 0;
      cur = player.getCurrentTime() || 0;
      state = player.getPlayerState();
    } catch (err) { return; }
    if (pendingSeekFraction !== null && dur > 0) {
      var target = pendingSeekFraction; pendingSeekFraction = null;
      player.seekTo(target * dur, true);
      cur = target * dur;
    }
    var effectiveDur = dur || knownDuration(activeBar);
    var frac = effectiveDur > 0 ? cur / effectiveDur : 0;
    paint(activeBar, frac, fmt(cur), dur > 0 ? fmt(dur) : durLabel(activeBar),
          state === 1 ? 'playing' : (state === 3 ? 'loading' : 'paused'));
  }

  function ensurePlayer(cb) {
    if (player) { cb(); return; }
    if (!ytReady) { pendingInit = cb; return; }
    var host = document.getElementById('yt-audio-host');
    if (!host) return;
    player = new YT.Player(host, {
      height: '113', width: '200',
      playerVars: { playsinline: 1, controls: 0, disablekb: 1, rel: 0, modestbranding: 1 },
      events: {
        onReady: function () { cb(); },
        onStateChange: function (ev) {
          if (ev.data === YT.PlayerState.ENDED && activeBar) {
            var d = player.getDuration() || knownDuration(activeBar);
            paint(activeBar, 1, fmt(d), fmt(d), 'paused');
            stopPolling();
          }
        },
        onError: function () {
          if (activeBar) {
            var r = row(activeBar);
            if (r) r.classList.add('errored');
          }
        }
      }
    });
  }

  window.onYouTubeIframeAPIReady = function () {
    ytReady = true;
    if (pendingInit) { var cb = pendingInit; pendingInit = null; cb(); }
  };

  function playFrom(bar, fraction) {
    var videoId = bar.getAttribute('data-yt');
    if (!videoId) return;
    ensurePlayer(function () {
      if (activeBar && activeBar !== bar) resetBar(activeBar);
      if (activeVideoId !== videoId) {
        activeVideoId = videoId;
        activeBar = bar;
        var known = knownDuration(bar);
        pendingSeekFraction = fraction > 0.01 ? fraction : null;
        player.loadVideoById(videoId);
        // Best-effort immediate jump using Discogs' own track length, so a
        // click deep into a bar does not sit at 0:00 waiting for YouTube's
        // own metadata to arrive. The poll loop re-seeks once the real
        // duration is confirmed, in case this fires before load is ready.
        if (known > 0 && pendingSeekFraction !== null) {
          try { player.seekTo(pendingSeekFraction * known, true); } catch (err) {}
        }
      } else {
        activeBar = bar;
        var dur = player.getDuration() || knownDuration(bar);
        if (dur > 0) player.seekTo(fraction * dur, true);
        player.playVideo();
      }
      stopPolling();
      pollTimer = setInterval(poll, 250);
      paint(bar, fraction, fmt(fraction * (player.getDuration() || knownDuration(bar))),
            durLabel(bar), 'loading');
    });
  }

  function togglePlayPause(bar) {
    var videoId = bar.getAttribute('data-yt');
    if (activeVideoId === videoId && player) {
      var state = player.getPlayerState();
      if (state === 1) { player.pauseVideo(); } else { player.playVideo(); }
    } else {
      playFrom(bar, 0);
    }
  }

  function fractionFromEvent(bar, ev) {
    var rect = bar.getBoundingClientRect();
    var x = (ev.clientX !== undefined && ev.clientX !== 0) ? ev.clientX
          : (ev.changedTouches && ev.changedTouches[0] ? ev.changedTouches[0].clientX : rect.left);
    return Math.min(1, Math.max(0, (x - rect.left) / rect.width));
  }

  document.addEventListener('click', function (ev) {
    var btn = ev.target.closest ? ev.target.closest('button.ppbtn') : null;
    if (btn) {
      ev.preventDefault();
      var pbar = row(btn).querySelector('.bar');
      if (pbar) togglePlayPause(pbar);
      return;
    }
    if (ev.target.closest && ev.target.closest('a.ytlink')) return; // let it navigate
    var bar = ev.target.closest ? ev.target.closest('.bar') : null;
    if (bar) {
      ev.preventDefault();
      playFrom(bar, fractionFromEvent(bar, ev));
    }
  });

  document.addEventListener('keydown', function (ev) {
    var el = document.activeElement;
    if (!el || !el.classList || !el.classList.contains('bar')) return;
    if (ev.key === ' ' || ev.key === 'Enter') {
      ev.preventDefault();
      togglePlayPause(el);
    } else if (ev.key === 'ArrowRight' || ev.key === 'ArrowLeft') {
      ev.preventDefault();
      if (!player || activeBar !== el) return;
      var dur = player.getDuration() || knownDuration(el);
      var cur = player.getCurrentTime() || 0;
      player.seekTo(Math.min(dur, Math.max(0, cur + (ev.key === 'ArrowRight' ? 5 : -5))), true);
    }
  });

  var tag = document.createElement('script');
  tag.src = 'https://www.youtube.com/iframe_api';
  document.head.appendChild(tag);
})();
"""


def archive_links(directory: str, current_stamp: str) -> list[str]:
    """Dated pages already in the archive, newest first, minus today's.

    Without this the older pages are unreachable unless you still have the
    email that linked to them.
    """
    if not os.path.isdir(directory):
        return []
    stamps = []
    for name in os.listdir(directory):
        match = ARCHIVE_NAME_RE.match(name)
        if match and match.group(1) != current_stamp:
            stamps.append(match.group(1))
    return sorted(stamps, reverse=True)


def render_player_page(sections, cutoff: datetime, genres: list[str],
                       stats: dict, generated: datetime,
                       archive: list[str] | None = None,
                       base_url: str = "") -> str:
    """A standalone web page with a click-to-seek play bar per track.

    This is the thing the email cannot be: a real page, so it can run script
    and drive a player. The email links here.

    base_url matters because this same HTML is written to two different
    places: docs/archive/<date>.html, and a copy of today's page at
    docs/index.html (so the bare site URL shows something). A link like
    href="2026-08-17.html" is only correct from inside archive/ -- copied
    into docs/ root it silently points one directory too high. Passing
    base_url makes every internal link absolute instead, so both copies of
    the page work identically regardless of where they end up on disk.
    """
    e = html.escape
    filter_text = ", ".join(genres) if genres else "everything (no filter)"
    total = sum(len(items) for _, items, _ in sections)

    def archive_href(stamp: str) -> str:
        return f"{base_url}/archive/{stamp}.html" if base_url else f"{stamp}.html"

    out = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="robots" content="noindex, nofollow">',
        f'<title>Record digest &mdash; {e(generated.strftime("%d %b %Y"))}</title>',
        f'<style>{PLAYER_CSS}</style></head><body>',
        # Single shared, hidden audio engine -- see the comment above PLAYER_JS.
        # Positioned off-screen rather than display:none, because a fully
        # unrendered iframe is more likely to be blocked from playing by
        # mobile browsers' autoplay heuristics.
        '<div style="position:fixed;left:-9999px;top:0;width:200px;height:113px;">'
        '<div id="yt-audio-host"></div></div>',
        '<div class="wrap">',
        '<h1>New on Discogs</h1>',
        f'<p class="sub">{total} record(s) listed since '
        f'{e(cutoff.strftime("%a %d %b %Y, %H:%M"))} UTC &middot; {e(filter_text)}</p>',
    ]

    if not sections:
        out.append('<p class="none">No new matching listings.</p>')

    for store, items, genre_label in sections:
        note = (
            f' <span style="color:#6f6f78;font-weight:normal;font-size:12px;">'
            f'&mdash; filter: {e(genre_label)}</span>'
            if genre_label is not None else ''
        )
        out.append(f'<h2>{e(store)} &middot; {len(items)}{note}</h2>')
        for item in items:
            thumb = (f'<img src="{e(item["thumb"])}" alt="" loading="lazy">'
                     if item.get("thumb") else '<img alt="">')

            bits = [e(item["price"]), e(item["condition"])]
            if item.get("formats"):
                bits.append(e(", ".join(item["formats"])))
            if item.get("label") or item.get("catno"):
                bits.append(e(" - ".join(p for p in (item.get("label"),
                                                     item.get("catno")) if p)))

            tracks = []
            for video in (item.get("videos") or []):
                if not video.get("yt"):
                    continue
                title = video["title"]
                if len(title) > 70:
                    title = title[:69].rstrip() + "…"
                dur = video.get("dur") or 0
                watch_url = f"https://www.youtube.com/watch?v={video['yt']}"
                tracks.append(
                    '<li class="track"><div class="trow">'
                    f'<button class="ppbtn" aria-label="Play {e(title)}">&#9654;</button>'
                    '<div class="bar" tabindex="0" role="slider" aria-valuemin="0" '
                    f'aria-valuemax="100" aria-valuenow="0" aria-label="{e(video["title"])}" '
                    f'data-yt="{e(video["yt"])}" data-dur="{dur}">'
                    '<div class="track"><div class="fill"></div></div></div>'
                    f'<span class="time">0:00 / {fmt_mmss(dur) if dur else "--:--"}</span>'
                    f'<a class="ytlink" href="{e(watch_url)}" target="_blank" '
                    f'rel="noopener noreferrer" title="Watch on YouTube" '
                    f'aria-label="Watch {e(title)} on YouTube">&#8599;</a>'
                    '</div>'
                    f'<div class="tname">{e(title)}</div></li>'
                )

            if tracks:
                track_html = '<ul class="tracks">' + "".join(tracks) + '</ul>'
            else:
                track_html = (
                    f'<p class="none">No clips attached &mdash; '
                    f'<a class="buy" style="margin:0" '
                    f'href="{e(youtube_search_url(item["description"]))}">'
                    'search YouTube</a></p>'
                )

            out.append(
                '<div class="rec">' + thumb + '<div class="body">'
                f'<div class="title"><a href="{e(item["url"])}">'
                f'{e(item["description"])}</a></div>'
                f'<div class="meta">{" &middot; ".join(bits)}'
                f'<a class="buy" href="{e(item["url"])}">Buy on Discogs &rarr;</a></div>'
                f'<div class="tags">{e(item["tags"])}</div>'
                + track_html + '</div></div>'
            )

    if stats.get("unchecked"):
        out.append(f'<p class="none">{stats["unchecked"]} listing(s) left '
                   'unchecked &mdash; a store bulk-listed.</p>')

    footer = [
        f'<footer>Generated {e(generated.strftime("%d %b %Y %H:%M"))} UTC. '
        'Tap a bar to play from that point; only one track plays at a time. '
        'The arrow opens the original video on YouTube.'
    ]
    if archive:
        links = " &middot; ".join(
            f'<a href="{e(archive_href(stamp))}">{e(stamp)}</a>' for stamp in archive[:14]
        )
        footer.append(f'<div style="margin-top:10px;">Earlier: {links}</div>')
    footer.append('</footer>')
    out.append("".join(footer))
    out.append(f'</div><script>{PLAYER_JS}</script></body></html>')
    return "\n".join(out)


ARCHIVE_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.html$")


def prune_archive(directory: str, keep_days: int, today: datetime) -> int:
    """Delete dated player pages older than keep_days. Returns how many went.

    Only touches files matching YYYY-MM-DD.html, so index.html and anything
    else in the directory is left alone. File mtimes are useless here because
    a fresh git checkout resets them, so the date comes from the name.
    """
    if keep_days <= 0 or not os.path.isdir(directory):
        return 0

    oldest = (today - timedelta(days=keep_days)).date()
    removed = 0
    for name in os.listdir(directory):
        match = ARCHIVE_NAME_RE.match(name)
        if not match:
            continue
        try:
            stamp = datetime.strptime(match.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        if stamp < oldest:
            try:
                os.remove(os.path.join(directory, name))
                removed += 1
            except OSError as exc:
                LOG.warning("Could not remove old archive page %s: %s", name, exc)
    return removed


def render_html(sections, cutoff: datetime, genres: list[str], stats: dict,
                player_url: str = "") -> str:
    e = html.escape
    filter_text = ", ".join(genres) if genres else "everything (no filter)"

    out = [
        '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
        'font-size:15px;line-height:1.45;color:#111;max-width:680px;margin:0 auto;">',
        '<h1 style="font-size:20px;margin:0 0 4px;">New on Discogs</h1>',
        f'<p style="margin:0 0 20px;color:#666;font-size:13px;">'
        f'Listed since {e(cutoff.strftime("%a %d %b %Y, %H:%M"))} UTC &middot; '
        f'filter: {e(filter_text)}</p>',
    ]

    # The email itself can never play audio, so point at the page that can.
    if player_url and sections:
        out.append(
            f'<p style="margin:0 0 24px;"><a href="{e(player_url)}" '
            'style="display:inline-block;background:#111;color:#fff;'
            'text-decoration:none;font-size:14px;font-weight:600;'
            'padding:11px 18px;border-radius:6px;">'
            '&#127911; Open the player</a>'
            '<span style="color:#888;font-size:12px;display:block;margin-top:6px;">'
            'Play every track from one page, without leaving for YouTube each time.'
            '</span></p>'
        )

    if not sections:
        out.append(
            '<p style="background:#f5f5f5;padding:14px;border-radius:6px;">'
            'No new matching listings today.</p>'
        )
    else:
        for store, items, genre_label in sections:
            note = (
                f' <span style="color:#888;font-weight:normal;font-size:11px;">'
                f'&mdash; filter: {e(genre_label)}</span>'
                if genre_label is not None else ''
            )
            out.append(
                f'<h2 style="font-size:16px;margin:26px 0 10px;padding-bottom:6px;'
                f'border-bottom:2px solid #111;">{e(store)} '
                f'<span style="color:#888;font-weight:normal;">({len(items)})</span>{note}</h2>'
            )
            for item in items:
                # Clicking the sleeve plays the record where possible, since
                # that is the thing you most want to do with a new listing.
                videos = item.get("videos") or []
                play_target = playlist_url(videos) or (videos[0]["uri"] if videos else "")

                thumb = ""
                if item["thumb"]:
                    image = (
                        f'<img src="{e(item["thumb"])}" width="60" height="60" alt="" '
                        f'style="display:block;border-radius:4px;background:#eee;">'
                    )
                    if play_target:
                        image = (f'<a href="{e(play_target)}" '
                                 f'style="text-decoration:none;">{image}</a>')
                    thumb = (
                        '<td width="64" style="padding:0 12px 0 0;vertical-align:top;">'
                        f'{image}</td>'
                    )

                meta = f'{e(item["price"])} &middot; {e(item["condition"])}'
                if item["sleeve"]:
                    meta += f' / {e(item["sleeve"])} sleeve'
                if item.get("formats"):
                    meta += f' &middot; {e(", ".join(item["formats"]))}'

                catno = ""
                if item["label"] or item["catno"]:
                    parts = [p for p in (item["label"], item["catno"]) if p]
                    catno = (f'<div style="color:#888;font-size:12px;">'
                             f'{e(" - ".join(parts))}</div>')

                out.append(
                    '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
                    'style="width:100%;margin:0 0 20px;"><tr>'
                    + thumb +
                    '<td style="vertical-align:top;">'
                    f'<a href="{e(item["url"])}" '
                    f'style="color:#0b5fff;text-decoration:none;font-weight:600;">'
                    f'{e(item["description"])}</a>'
                    f'<div style="color:#444;font-size:13px;margin-top:2px;">{meta}</div>'
                    + catno +
                    f'<div style="color:#888;font-size:12px;">{e(item["tags"])}</div>'
                    + listen_html(item, e) +
                    '</td></tr></table>'
                )

    if stats.get("unchecked"):
        out.append(
            '<p style="margin-top:24px;padding:10px;background:#fffbe6;'
            'border-left:3px solid #d90;font-size:13px;color:#7a5c00;">'
            f'A store bulk-listed today: {stats["unchecked"]} listing(s) were left '
            'unchecked after this run hit its lookup budget. Narrow '
            'LOOKBACK_HOURS, or raise MAX_RELEASE_LOOKUPS.</p>'
        )

    if stats["failed_sellers"]:
        out.append(
            '<p style="margin-top:24px;padding:10px;background:#fff4f4;'
            'border-left:3px solid #c00;font-size:13px;color:#900;">'
            'Could not check: ' + e(", ".join(stats["failed_sellers"])) +
            '. See the GitHub Actions log.</p>'
        )

    out.append(
        f'<p style="margin-top:28px;color:#999;font-size:12px;border-top:1px solid #eee;'
        f'padding-top:10px;">{stats["considered"]} new listing(s) seen, '
        f'{stats["matched"]} matched.</p></div>'
    )
    return "\n".join(out)


def render_text(sections, cutoff: datetime, genres: list[str], stats: dict,
                player_url: str = "") -> str:
    """Plain-text alternative. Improves deliverability and keeps the mail
    readable in clients that block HTML."""
    filter_text = ", ".join(genres) if genres else "everything (no filter)"
    lines = [
        "NEW ON DISCOGS",
        f'Listed since {cutoff.strftime("%a %d %b %Y, %H:%M")} UTC',
        f"Filter: {filter_text}",
        "",
    ]
    if player_url and sections:
        lines += [f"Play them all here: {player_url}", ""]
    if not sections:
        lines.append("No new matching listings today.")
    for store, items, genre_label in sections:
        header = f"{store} ({len(items)})"
        if genre_label is not None:
            header += f" -- filter: {genre_label}"
        lines.append(header)
        lines.append("-" * len(header))
        for item in items:
            lines.append(f"  {item['description']}")
            lines.append(f"    {item['price']} | {item['condition']} | {item['tags']}")
            lines.append(f"    {item['url']}")
            videos = item.get("videos") or []
            playlist = playlist_url(videos)
            if playlist:
                lines.append(f"    Play all {len(videos)}: {playlist}")
            elif videos:
                lines.append(f"    Listen: {videos[0]['uri']}")
        lines.append("")
    if stats.get("unchecked"):
        lines.append(f"NOTE: {stats['unchecked']} listing(s) left unchecked - "
                     f"lookup budget spent (a store bulk-listed).")
    if stats["failed_sellers"]:
        lines.append(f"Could not check: {', '.join(stats['failed_sellers'])}")
    lines.append(f"{stats['considered']} new listing(s) seen, {stats['matched']} matched.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def send_email(subject: str, html_body: str, text_body: str) -> None:
    """Send via any SMTP provider. Port 465 uses implicit SSL, anything else
    (587, 25) connects plain and upgrades with STARTTLS."""
    host = os.environ["SMTP_HOST"]
    port = env_int("SMTP_PORT", 465)
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    mail_from = env_str("MAIL_FROM", user)
    recipients = [r.strip() for r in os.environ["MAIL_TO"].split(",") if r.strip()]

    if not recipients:
        raise RuntimeError("MAIL_TO did not contain any email address")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    LOG.info("Sending to %s via %s:%s", ", ".join(recipients), host, port)
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=HTTP_TIMEOUT) as server:
            server.login(user, password)
            server.send_message(msg, from_addr=mail_from, to_addrs=recipients)
    else:
        with smtplib.SMTP(host, port, timeout=HTTP_TIMEOUT) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(user, password)
            server.send_message(msg, from_addr=mail_from, to_addrs=recipients)
    LOG.info("Email sent.")


def require_env(names: list[str]) -> None:
    missing = [n for n in names if not os.environ.get(n, "").strip()]
    if missing:
        raise SystemExit(
            "Missing required environment variable(s): " + ", ".join(missing) +
            "\nSet them as GitHub repo secrets (Settings > Secrets and variables > "
            "Actions), or export them locally. See README.md."
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Build the digest but print it instead of emailing. "
                             "Needs only DISCOGS_TOKEN.")
    parser.add_argument("--out", metavar="FILE",
                        help="Also write the HTML digest to this file.")
    parser.add_argument("--player-dir", metavar="DIR",
                        help="Write the playable web page into this directory as "
                             "<YYYY-MM-DD>.html, then prune older dated pages.")
    parser.add_argument("--keep-days", type=int, default=ARCHIVE_KEEP_DAYS,
                        metavar="N",
                        help=f"Days of dated player pages to keep "
                             f"(default {ARCHIVE_KEEP_DAYS}).")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    dry_run = args.dry_run or env_flag("DRY_RUN")

    require_env(["DISCOGS_TOKEN"])
    if not dry_run:
        require_env(["SMTP_HOST", "SMTP_USER", "SMTP_PASS", "MAIL_TO"])

    sellers = env_sellers("SELLERS", SELLERS)
    genres = env_csv_list("GENRES_INCLUDE", GENRES_INCLUDE)
    formats = env_csv_list("FORMATS_INCLUDE", FORMATS_INCLUDE)
    genres_by_store = env_genres_by_store("GENRES_BY_STORE", GENRES_BY_STORE)
    lookback = env_int("LOOKBACK_HOURS", LOOKBACK_HOURS)
    if lookback <= 0:
        LOG.warning("LOOKBACK_HOURS must be positive - using %s", LOOKBACK_HOURS)
        lookback = LOOKBACK_HOURS

    if not sellers:
        raise SystemExit("No sellers configured - nothing to check.")

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback)
    LOG.info("Lookback %sh (cutoff %s UTC)", lookback, cutoff.strftime("%Y-%m-%d %H:%M"))
    LOG.info("Genre filter: %s", ", ".join(genres) if genres else "(none - keeping everything)")
    LOG.info("Format filter: %s", ", ".join(formats) if formats else "(none - keeping everything)")
    for override_user, override_genres in genres_by_store.items():
        LOG.info("  per-store override for %s: %s", override_user,
                 ", ".join(override_genres) if override_genres else "(none - keeping everything)")
    LOG.info("Sellers: %s", ", ".join(sellers))
    LOG.info("Caps: %d page(s)/seller (%d listings), %d release lookup(s)/run",
             env_int("MAX_PAGES", MAX_PAGES), env_int("MAX_PAGES", MAX_PAGES) * PER_PAGE,
             env_int("MAX_RELEASE_LOOKUPS", MAX_RELEASE_LOOKUPS))

    api = Discogs(
        os.environ["DISCOGS_TOKEN"].strip(),
        env_str("USER_AGENT", USER_AGENT),
        env_int("MAX_RELEASE_LOOKUPS", MAX_RELEASE_LOOKUPS),
        env_int("MAX_VIDEOS_PER_RELEASE", MAX_VIDEOS_PER_RELEASE),
        env_int("MAX_PAGES", MAX_PAGES),
    )

    started = time.monotonic()
    sections, stats = build_digest(api, sellers, cutoff, genres, formats, genres_by_store)
    elapsed = time.monotonic() - started

    LOG.info(
        "Done in %.0fs: %d new listing(s) seen, %d matched, across %d store(s). "
        "%d API request(s).",
        elapsed, stats["considered"], stats["matched"], len(sections), api.request_count,
    )
    if stats["unchecked"]:
        LOG.warning(
            "%d listing(s) left unchecked - the release-lookup budget ran out. "
            "A store bulk-listed. Lower LOOKBACK_HOURS or raise MAX_RELEASE_LOOKUPS.",
            stats["unchecked"],
        )

    # The player page and the link the email points at must agree on the
    # filename, so both are derived from the same timestamp here.
    generated = datetime.now(timezone.utc)
    stamp = generated.strftime("%Y-%m-%d")

    player_url = ""
    base_url = env_str("PAGES_BASE_URL", "").rstrip("/")
    if base_url:
        player_url = f"{base_url}/archive/{stamp}.html"

    if args.player_dir:
        try:
            os.makedirs(args.player_dir, exist_ok=True)

            # Prune first, so the "earlier digests" nav only lists pages that
            # still exist after this run.
            removed = prune_archive(args.player_dir, args.keep_days, generated)
            if removed:
                LOG.info("Pruned %d archived page(s) older than %d days",
                         removed, args.keep_days)

            earlier = archive_links(args.player_dir, stamp)
            page_path = os.path.join(args.player_dir, f"{stamp}.html")
            with open(page_path, "w", encoding="utf-8") as handle:
                handle.write(render_player_page(
                    sections, cutoff, genres, stats, generated, earlier, base_url
                ))
            LOG.info("Wrote player page to %s (%d earlier page(s) linked)",
                     page_path, len(earlier))
        except OSError as exc:
            # A failed page must not cost you the email.
            LOG.error("Could not write the player page: %s", exc)
            player_url = ""

    if player_url:
        LOG.info("Player page will be at %s", player_url)
    elif args.player_dir:
        LOG.warning("PAGES_BASE_URL is not set - the email will have no player link")

    html_body = render_html(sections, cutoff, genres, stats, player_url)
    text_body = render_text(sections, cutoff, genres, stats, player_url)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(html_body)
        LOG.info("Wrote HTML digest to %s", args.out)

    subject = (
        f"Record digest - {stats['matched']} new"
        if stats["matched"]
        else "Record digest - nothing new"
    )
    subject += f" - {datetime.now(timezone.utc).strftime('%d %b %Y')}"

    if dry_run:
        LOG.info("Dry run - not sending email. Subject would be: %s", subject)
        print("\n" + text_body)
    else:
        try:
            send_email(subject, html_body, text_body)
        except (smtplib.SMTPException, OSError) as exc:
            LOG.error("Sending the email failed: %s", exc)
            return 1

    # Non-zero exit if any store could not be checked, so the run shows red
    # in GitHub Actions and you actually notice.
    if stats["failed_sellers"]:
        LOG.error("Failed sellers: %s", ", ".join(stats["failed_sellers"]))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
