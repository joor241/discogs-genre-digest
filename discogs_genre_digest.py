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
import json
import logging
import os
import re
import smtplib
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from urllib.parse import quote, quote_plus, urlparse

import requests

# ---------------------------------------------------------------------------
# CONFIG -- edit these, or override them with environment variables
# ---------------------------------------------------------------------------

# Discogs seller username -> display name used in the email.
# The key must be the store's Discogs SELLER USERNAME, not their website.
# Find it in the URL of their shop page: discogs.com/seller/<username>/profile
# Verified against the live API on 2026-08-18. decks.de added 2026-08-23:
# 39,915 items for sale, updating in real time -- like Rush Hour, expect it
# to occasionally bulk-list and push the run into the MAX_RELEASE_LOOKUPS /
# MAX_PAGES safety caps rather than checking every listing.
SELLERS = {
    "RushHour": "Rush Hour",             # rushhour.nl
    "clone.nl": "Clone",                 # clone.nl
    "offbeat__records": "Offbeat Records",  # note: two underscores
    "decks.de": "Decks",                 # decks.de -- huge, very active seller
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

# Non-Discogs sources: fetched directly from a shop's own site rather than
# via the Discogs API, so neither carries structured genre/style/format data
# the way a Discogs release does. Genre matching for these runs against the
# item's title + description TEXT instead (same whole-word engine, just fed
# prose instead of a genres/styles array), and format is classified from
# whatever format signal each source actually exposes -- see
# classify_clone_format() / classify_deejay_format() for exactly what that
# means for each one. Both respect GENRES_INCLUDE/GENRES_BY_STORE (using the
# *_KEY below) and FORMATS_INCLUDE like every other source.

# clone.nl's own new-arrivals RSS feed -- a different catalogue from their
# Discogs marketplace listings (if "clone.nl" is also in SELLERS above), so
# both can legitimately appear as separate sections without duplicating.
# Verified live and real (not every shop exposes one -- deejay.de below does
# not). pubDate is day-precision only (always midnight in every item
# observed), coarser than Discogs' second-precision `posted` field.
CLONE_RSS_URL = "https://clone.nl/rss/all"
CLONE_RSS_KEY = "clone-rss"  # use this as a key in GENRES_BY_STORE to override just this source
CLONE_RSS_ENABLED = True

# clone.nl's own item pages embed direct MP3 preview clips per track (not
# YouTube), e.g. clone.nl/platen/mp3/84483/1 Release It.mp3 -- confirmed live
# by fetching a real item page, not assumed. That means real inline playback
# for this source, same as Discogs releases get via YouTube, just backed by
# a plain <audio> element instead of a hidden YouTube player.
#
# Getting it costs one extra HTTP request per MATCHED item (not per item in
# the feed -- only for the ones that already passed genre/format filtering),
# so this is capped independently of MAX_VIDEOS_PER_RELEASE to bound worst
# case request volume on a broad-filter run. Items beyond the cap still
# appear in the digest, just without playable tracks.
CLONE_AUDIO_MAX_ITEMS = 30

# deejay.de has no RSS/Atom feed (checked directly: no <link rel="alternate">
# anywhere, and every guessed feed path returns their normal 200 HTML rather
# than a real feed). This scrapes their "All / News" page's HTML instead,
# which is NOT a publisher-provided contract the way RSS is -- it WILL break
# silently if deejay.de redesigns that page. fetch_deejay_html() logs a loud
# warning if it ever finds zero items, since that page realistically always
# has some, so a scraper break is noticeable rather than just going quiet.
#
# There is also no reliable "when was this added to the shop" signal: the
# page's own per-item date looks like a release date, not an arrival date
# (some items literally show "Release unknown"), so unlike every other
# source here, this one can't use a time-based lookback cutoff at all.
# Instead, article ids are remembered explicitly: docs/deejay_seen.json
# (committed by the workflow, same git-as-state pattern as docs/likes.json)
# tracks every id already shown, and each run skips anything already in it
# -- so "new" here means "not shown before", checked once and forgotten,
# rather than derived from a timestamp. See fetch_deejay_html().
DEEJAY_URL = "https://www.deejay.de/m_All/sm_News/lang_en"
DEEJAY_KEY = "deejay"
DEEJAY_ENABLED = True
DEEJAY_MAX_ITEMS = 60  # how many of the page's items to consider each run

# Observed live, repeatedly: deejay.de is reachable in well under a second
# from a plain residential connection, but GitHub Actions runs against the
# exact same URL have failed with a connect timeout multiple times in a
# row -- consistent with the runner's datacenter IP range being blocked or
# rate-limited specifically, which more retries or a longer timeout cannot
# fix (a deliberate block just keeps timing out regardless). Since this
# isn't something you can act on day to day, a deejay.de failure no longer
# fails the whole run's exit code when this is on -- it's still logged as
# an ERROR and still shown in the digest as "Could not check: deejay.de",
# just not treated as urgent. Set DEEJAY_SOFT_FAIL=false to go back to a
# hard failure (red run) for this source too.
DEEJAY_SOFT_FAIL = True

# How many days to remember a deejay.de article id before pruning it from
# docs/deejay_seen.json, bounding the file's growth. Long enough that the
# id has certainly scrolled off the page's own "News" listing by then (it
# only ever shows recent stock), so nothing is lost by forgetting it.
DEEJAY_SEEN_KEEP_DAYS = 90

# deejay.de items DO get real audio: each track's own MP3 lives at a
# predictable, static URL sharded by the article id's own last two digits
# (streamit/{tens}/{units}/{id}{letter}.mp3 -- same pattern as their cover
# image URLs), confirmed directly fetchable over plain HTTP with no session
# or cookie required. Their player JS routes playback through a
# session-gated AJAX call, which looked like a hard wall at first, but a
# real browser network capture showed that call is for play-count tracking,
# not access control -- the file itself needs no ticket. See
# extract_deejay_tracks() / deejay_stream_url().
#
# For the rarer item with no tracklist of its own (e.g. a single with
# nothing listed), this falls back to cross-referencing Discogs' release
# search and borrowing its community-submitted YouTube links when a
# confident match is found (never a guess -- see confident_discogs_match()).
# That costs one extra Discogs API search per fallback item (plus a
# release-detail fetch on a hit, counted against MAX_RELEASE_LOOKUPS same as
# everything else), so it's capped independently.
DEEJAY_DISCOGS_LOOKUP_MAX_ITEMS = 20

# clone.nl and deejay.de's "new arrivals" feeds mix items that are actually
# in stock with pre-orders and sold-out listings still shown for a while --
# confirmed live: 9 of 10 sampled clone.nl items were "preorder", not
# available now. Both sites only expose this on the item's own detail page,
# not the list/feed view, so checking it costs one extra request per item --
# free for clone.nl (same page already fetched for audio) but a genuinely
# new fetch for deejay.de, hence its own cap. 0 disables the deejay.de check.
DEEJAY_STOCK_CHECK_MAX_ITEMS = 20

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


class FeedError(RuntimeError):
    """A non-Discogs source (RSS feed or scraped page) failed."""


# Fuller, more browser-like headers than User-Agent alone. Doesn't change
# anything observed from a residential IP (deejay.de and clone.nl both
# answer in well under a second either way, no rate-limit or CDN headers
# either way) -- kept anyway since it's a real, free reduction in how
# obviously automated a request looks, and costs nothing.
def http_headers(user_agent: str) -> dict[str, str]:
    return {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }


def http_get_text(url: str, user_agent: str, timeout: int = HTTP_TIMEOUT,
                  attempts: int = 3, backoff: tuple[float, ...] = (3, 10)) -> str:
    """Plain GET with retries and growing backoff. Used by the non-Discogs
    sources, which don't need the Discogs client's rate-limit and paging
    machinery -- that's specific to a 60/min marketplace budget.

    Observed live and repeatedly: deejay.de has failed with a CONNECT
    timeout from GitHub Actions specifically -- the TCP handshake itself
    never completes -- while answering the identical request in under a
    second from elsewhere, and the SAME workflow has then succeeded again a
    run or two later with nothing changed. That pattern (fails, then
    recovers on its own) is exactly what backoff-and-retry is for: a longer
    timeout on one attempt would not help a connection that is not merely
    slow, but retrying again after a real pause can, if what's blocking it
    is a transient or rate-limit condition that clears on its own. The
    default here is deliberately modest (per-item detail fetches use it too,
    and should fail fast rather than slow down a run with many items) --
    callers making the one big per-run feed fetch pass more attempts and
    longer backoff explicitly, since that single call is worth spending
    more time on.
    """
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.get(url, headers=http_headers(user_agent), timeout=timeout)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < attempts:
                wait = backoff[min(attempt - 1, len(backoff) - 1)]
                LOG.warning("GET %s failed (attempt %d/%d): %s - retrying in %ss",
                           url, attempt, attempts, exc, wait)
                time.sleep(wait)
    raise FeedError(f"GET {url} failed after {attempts} attempt(s): {last_exc}")


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

    def search_release(self, query: str) -> list[dict]:
        """Discogs' own release search, used to cross-reference a release
        from a source that has no video data of its own (deejay.de) against
        Discogs' community-submitted YouTube links. Not counted against
        lookup_budget -- that budget is specifically for /releases/{id}
        detail calls, and search is a distinct, comparatively cheap step."""
        try:
            data = self.get("/database/search", params={"q": query, "type": "release", "per_page": 5})
        except DiscogsError:
            return []
        return data.get("results") or []


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


def buy_button_label(url: str) -> str:
    """The label always matches where the button actually goes, rather than
    hardcoding "Buy on Discogs" for every item regardless of source -- clone.nl
    and deejay.de items link to their own site, not Discogs, and saying
    otherwise there is just wrong. Derived from the URL itself rather than a
    separate per-item "source" field, so it can never drift out of sync with
    the actual link."""
    host = (urlparse(url).netloc or "").removeprefix("www.")
    if not host:
        return "View listing"
    if "discogs.com" in host:
        return "Buy on Discogs"
    return f"View on {host[0].upper()}{host[1:]}"


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


def matching_terms(text: str, wanted_norm: list[str]) -> list[str]:
    """Which of the wanted genre terms actually matched inside free text.

    Used only by the non-Discogs sources, to show *why* an item appeared in
    place of real genre/style tags they don't have. Showing nothing there
    would be less honest than Discogs items showing their actual tags.
    """
    if not wanted_norm:
        return []
    hay = f" {normalise_tag(text)} "
    return [w for w in wanted_norm if f" {w} " in hay]


def confident_discogs_match(candidate_title: str, artist: str, title: str) -> bool:
    """True only if a Discogs search result is confidently the same release
    as (artist, title) -- requires the candidate's own title to contain the
    artist (whole-word, every word of a multi-word name) AND at least one
    real word from the title. This is deliberately an ALL-of check, not
    matches_genre()'s ANY-of semantics: a wrong record borrowed here would
    show the wrong audio entirely, which is worse than showing none.

    Catalog number alone is not trustworthy for this: verified live that a
    short code like "PS01" matches 2,157 unrelated Discogs releases on its
    own, while "artist + PS01" narrows to exactly the right one -- so this
    is only ever called on results from a query that already included the
    artist, not on catalog number in isolation.
    """
    hay = f" {normalise_tag(candidate_title)} "

    artist_norm = normalise_tag(artist)
    artist_words = [w for w in artist_norm.split() if len(w) > 1]
    if not artist_words or not all(f" {w} " in hay for w in artist_words):
        return False

    title_words = [w for w in normalise_tag(title).split() if len(w) > 2]
    if title_words and not any(f" {w} " in hay for w in title_words[:4]):
        return False

    return True


def find_discogs_videos(api: "Discogs", artist: str, title: str, catno: str = "") -> list[dict]:
    """Best-effort: borrow Discogs' community-submitted YouTube links for a
    release that came from a source with no video data of its own (currently
    deejay.de). Returns [] rather than a guess when not confident -- see
    confident_discogs_match() for what "confident" means here.

    Reuses release_info() for the actual release fetch once a candidate id
    is found, so this gets caching and the lookup_budget cap for free and
    produces videos in exactly the shape extract_videos() already builds --
    no separate item-shape handling needed for this path.
    """
    query = " ".join(p for p in (artist, title, catno) if p).strip()
    if not query:
        return []

    try:
        results = api.search_release(query)
    except Exception:  # noqa: BLE001 - a failed cross-reference must not lose the item
        return []

    for result in results[:5]:
        candidate_title = result.get("title") or ""
        if not confident_discogs_match(candidate_title, artist, title):
            continue
        release_id = result.get("id")
        if not release_id:
            continue
        try:
            info = api.release_info(release_id)
        except DiscogsError:
            continue
        if info and info.get("videos"):
            return info["videos"]

    return []


BARE_AMP_RE = re.compile(r'&(?!amp;|lt;|gt;|quot;|apos;|#\d+;|#x[0-9a-fA-F]+;)')

CLONE_TITLE_RE = re.compile(r'^(?P<desc>.*?)\s*\((?P<format>[^)]+)\)\s*(?:-\s*(?P<catno>\S.*))?$')
CLONE_IMG_RE = re.compile(r'<img[^>]+src="([^"]+)"')
CLONE_STATUS_RE = re.compile(
    r'<td class="col-xs-2 status">\s*<span class="hidden-xs">\s*([^<]*?)\s*</span>'
)
CLONE_TRACK_TAG_RE = re.compile(r'<a class="preview"[^>]*>', re.I)
HREF_ATTR_RE = re.compile(r'href="([^"]+)"')
TITLE_ATTR_RE = re.compile(r'title="([^"]*)"')


def extract_clone_tracks(item_page_html: str, limit: int = CLONE_AUDIO_MAX_ITEMS) -> list[dict]:
    """Direct MP3 preview links from a clone.nl item page's tracklist.

    Confirmed live: `<a class="preview" itemprop="audio" href="...mp3" ...>`
    per track. The href isn't percent-encoded (spaces appear literally in the
    URL), so it's encoded here before use.
    """
    tracks: list[dict] = []
    for tag in CLONE_TRACK_TAG_RE.findall(item_page_html):
        href_match = HREF_ATTR_RE.search(tag)
        if not href_match:
            continue
        title_match = TITLE_ATTR_RE.search(tag)
        title = html.unescape(title_match.group(1)) if title_match else "Listen"
        src = quote(html.unescape(href_match.group(1)), safe=":/")
        tracks.append({"title": title, "src": src, "uri": src, "yt": None, "dur": 0})
        if len(tracks) >= limit:
            break
    return tracks


def classify_clone_format(format_text: str) -> list[str]:
    """Best-effort classification from the format shown in clone.nl's title
    text -- there is no structured format field in the feed. Unrecognised
    text defaults to Vinyl: clone.nl badges itself a vinyl specialist and
    every item sampled while building this was vinyl, so an unrecognised
    token here is far more likely to be a formatting variant ("2x12inch",
    "10inch") than a genuinely different medium.
    """
    t = format_text.lower()
    if "cd" in t:
        return ["CD"]
    if any(w in t for w in ("cassette", "tape", "mc")):
        return ["Cassette"]
    if any(w in t for w in ("download", "digital", "file", "mp3", "flac")):
        return ["File"]
    return ["Vinyl"]


def classify_clone_stock(status_text: str) -> tuple[str, str]:
    """(status_code, human note) from clone.nl's item-detail status cell.

    Confirmed live across 10 sampled items: the cell holds "preorder" or
    "out of stock" when either applies. No "in stock" text was ever observed
    -- clone.nl appears to only render this cell for the exceptional states,
    leaving it empty/absent for a normal, immediately-available item. An
    empty match is therefore treated as in stock, not as unknown.
    """
    t = status_text.strip().lower()
    if not t:
        return "in_stock", ""
    if "preorder" in t or "pre-order" in t or "pre order" in t:
        return "preorder", "Pre-order"
    if "out of stock" in t:
        return "out_of_stock", "Out of stock"
    if "backorder" in t or "back order" in t:
        return "preorder", "Back order"
    return "", ""  # unrecognised text -- say nothing rather than guess


def fetch_clone_rss(cutoff: datetime, wanted_norm: list[str], wanted_formats: list[str],
                    user_agent: str, url: str = CLONE_RSS_URL,
                    audio_max_items: int = CLONE_AUDIO_MAX_ITEMS,
                    seen: dict[str, str] | None = None,
                    now: datetime | None = None,
                    ) -> tuple[list[dict], int, dict[str, str]]:
    """New arrivals from clone.nl's own webshop RSS feed. See the CLONE_RSS_*
    comments near the top of the file for what is and isn't reliable here.

    `seen` (item link -> first-seen-timestamp, from docs/clone_seen.json)
    stops the same item reappearing purely because it's still inside the
    lookback window on a later run -- same reasoning and pattern as
    collect_seller's discogs_seen and fetch_deejay_html's deejay_seen.
    """
    seen = dict(seen or {})
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat()
    updated_seen = dict(seen)
    already_seen = 0

    # The one fetch this whole source lives or dies on -- worth spending
    # real time retrying, unlike the per-item detail fetches below.
    raw = http_get_text(url, user_agent, attempts=5, backoff=(5, 15, 30, 60))

    # clone.nl's feed generator does not escape bare "&" in artist names
    # (observed live: "Phat Kat & Jon Doe"), which is invalid XML. "&" is
    # common in collab artist names in this genre, so this needs real
    # handling, not a one-off workaround.
    raw = BARE_AMP_RE.sub("&amp;", raw)

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise FeedError(f"clone.nl feed was not parseable XML even after sanitising: {exc}")

    ns = {"content": "http://purl.org/rss/1.0/modules/content/"}
    matched: list[dict] = []
    considered = 0

    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue

        posted = parse_posted((item.findtext("pubDate") or "").strip())
        if posted is None:
            continue
        if posted < cutoff:
            # Feed is newest-first (verified), so nothing after this can be
            # newer -- same early-stop trick as the Discogs inventory pages.
            break

        link = (item.findtext("link") or "").strip()
        seen_key = link or title
        if seen_key in seen:
            already_seen += 1
            continue
        updated_seen[seen_key] = now_iso
        considered += 1

        description_html = item.findtext("description") or ""
        blurb = item.findtext("content:encoded", namespaces=ns) or ""

        title_match = CLONE_TITLE_RE.match(title)
        desc_text = title_match.group("desc").strip() if title_match else title
        format_text = title_match.group("format") if title_match else ""
        catno = (title_match.group("catno") or "").strip() if title_match else ""

        formats = classify_clone_format(format_text) if format_text else ["Vinyl"]
        if not matches_format(formats, wanted_formats):
            continue

        haystack = f"{title} {blurb}"
        if not matches_genre([haystack], wanted_norm):
            continue
        hit_terms = matching_terms(haystack, wanted_norm)

        img_match = CLONE_IMG_RE.search(description_html)

        tracks: list[dict] = []
        stock_status, stock_note = "", ""
        item_url = link or url
        if link and audio_max_items > 0 and len(matched) < audio_max_items:
            try:
                item_html = http_get_text(link, user_agent)
                tracks = extract_clone_tracks(item_html)
                # Free: same page already fetched above for tracks, so
                # stock status costs nothing extra here.
                status_match = CLONE_STATUS_RE.search(item_html)
                if status_match:
                    stock_status, stock_note = classify_clone_stock(status_match.group(1))
            except FeedError as exc:
                # One item's page failing to load must not lose the item
                # itself -- it just shows up without playable tracks.
                LOG.warning("[clone-rss] could not fetch tracklist for %s: %s", link, exc)
            time.sleep(0.5)  # be a reasonable citizen -- this is an extra
                             # per-item request beyond the single feed fetch

        matched.append({
            "description": f"{desc_text} ({format_text})" if format_text else desc_text,
            "price": "price on request",
            "condition": "New",
            "sleeve": "",
            "url": item_url,
            "tags": ", ".join(hit_terms) if hit_terms else "new arrival",
            "thumb": img_match.group(1) if img_match else "",
            "label": "",
            "catno": catno,
            "videos": tracks,
            "formats": formats,
            "stock_status": stock_status,
            "stock_note": stock_note,
        })

    if already_seen:
        LOG.info("[clone-rss] %d already shown in a prior digest, skipped", already_seen)
    return matched, considered, updated_seen


DEEJAY_ARTICLE_RE = re.compile(r'<article id="a(\d+)"[^>]*>(.*?)</article>', re.S)
DEEJAY_ARTIST_RE = re.compile(r'<h2 class="artist[^>]*>.*?<a href="[^"]*">([^<]+)</a>', re.S)
DEEJAY_TITLE_RE = re.compile(r'<h3 class="title[^>]*><a href="([^"]+)">([^<]+)</a>')
DEEJAY_MEDIUM_RE = re.compile(r'<span class="medium[^"]*">([^<]+)</span>')
DEEJAY_LABELCAT_RE = re.compile(
    r'<span class="musiclabel[^>]*>\s*<strong>([^<]*)</strong>\s*(?:<br\s*/?>)?\s*'
    r'(?:<a[^>]*>([^<]*)</a>)?', re.S
)
DEEJAY_DATE_RE = re.compile(r'<span class="date">([^<]+)</span>')
DEEJAY_IMG_RE = re.compile(r'<img src="([^"]+)"')
DEEJAY_PRICE_RE = re.compile(r'<span class="price">([\d.,]+)')
DEEJAY_FORMAT_TOKEN_RE = re.compile(r'_([A-Za-z0-9]+)__\d+$')
DEEJAY_TAG_STRIP_RE = re.compile(r"<[^>]+>")
DEEJAY_TRACK_RE = re.compile(
    r'<a class="track[^"]*"[^>]*id="playTrack_(\d+)_([a-z])"[^>]*>\s*<b>([^<]*)</b>:\s*([^<]*)</a>'
)
# Only present on the item's own detail page, not the list page this module
# otherwise scrapes -- confirmed live across a real presale item ("ships from
# {date}"), a real pre-order item ("pre-order now {date}"), and several
# "In Stock" items with no date in the second span.
DEEJAY_STOCKSTATUS_RE = re.compile(
    r'<div class="stockstatus"><span class="first">([^<]*)</span>'
    r'<span class="second">([^<]*)</span></div>'
)
# Also detail-page-only. Confirmed live: holds text like "Vinyl Only" or
# "Vinyl Only, 180g" when present, but is frequently absent entirely (its
# absence means nothing either way -- just that deejay.de didn't add that
# particular marketing callout for this release, not that it ships with a
# download code).
DEEJAY_FEATURE_RE = re.compile(r'<span class="feature"><b>Features:</b>\s*([^<]*)</span>')


def classify_deejay_stock(first_text: str, second_text: str) -> tuple[str, str]:
    """(status_code, human note) from deejay.de's item-detail stockstatus."""
    first = first_text.strip().lower()
    date = second_text.strip()
    if "pre-order" in first or "preorder" in first:
        return "preorder", f"Pre-order — expected {date}" if date else "Pre-order"
    if "ships from" in first:
        return "preorder", f"Ships from {date}" if date else "Ships soon"
    if "in stock" in first:
        return "in_stock", ""
    if "out of stock" in first:
        return "out_of_stock", "Out of stock"
    return "", ""  # unrecognised text -- say nothing rather than guess


def deejay_stream_url(article_id: str, letter: str) -> str:
    """The direct MP3 URL for one track, sharded by the article id's own
    last two digits -- the same pattern deejay.de uses for its cover image
    URLs (images/l/{tens}/{units}/{id}.jpg). Confirmed live: this file is
    directly fetchable over plain HTTP, no session or cookie required,
    despite their player JS routing playback through a session-gated AJAX
    call -- that call turned out to be for play-count tracking, not access
    control (verified with a real browser network capture)."""
    padded = article_id.zfill(2)
    return f"https://www.deejay.de/streamit/{padded[-2]}/{padded[-1]}/{article_id}{letter}.mp3"


def extract_deejay_tracks(block: str, limit: int = MAX_VIDEOS_PER_RELEASE) -> list[dict]:
    """Direct MP3 preview links for one deejay.de item, built entirely from
    the page block already scraped for everything else -- no extra request
    needed, unlike clone.nl's per-item tracklist fetch."""
    tracks: list[dict] = []
    for article_id, letter, position, title_text in DEEJAY_TRACK_RE.findall(block):
        clean_title = html.unescape(title_text.strip())
        title = f"{position}: {clean_title}" if clean_title else position
        url = deejay_stream_url(article_id, letter)
        tracks.append({"title": title, "src": url, "uri": url, "yt": None, "dur": 0})
        if len(tracks) >= limit:
            break
    return tracks


def classify_deejay_format(medium_text: str, url_path: str) -> list[str]:
    """deejay.de's own "medium" badge is usually a real format (12inch, LP,
    CD...), but shop-exclusive items show "excl" instead -- observed live,
    not assumed. When the badge isn't a recognised format, fall back to the
    format token embedded in the item's own URL slug (e.g.
    ..._Vinyl__1239340), which was present even on the "excl"-badged items.
    """
    t = (medium_text or "").lower().strip()
    if "inch" in t or t in ("lp", "box", "flexi", "picture disc"):
        return ["Vinyl"]
    if t == "cd" or ("cd" in t and "inch" not in t):
        return ["CD"]
    if t in ("mc", "cassette", "tape"):
        return ["Cassette"]
    if t in ("download", "digital", "file", "mp3", "flac", "wav"):
        return ["File"]

    token_match = DEEJAY_FORMAT_TOKEN_RE.search(url_path)
    if token_match:
        token = token_match.group(1).lower()
        if "vinyl" in token:
            return ["Vinyl"]
        if token == "cd":
            return ["CD"]
    return [medium_text] if medium_text else ["Unknown"]


def fetch_deejay_html(wanted_norm: list[str], wanted_formats: list[str], user_agent: str,
                      api: "Discogs | None" = None, url: str = DEEJAY_URL,
                      max_items: int = DEEJAY_MAX_ITEMS,
                      discogs_lookup_max_items: int = DEEJAY_DISCOGS_LOOKUP_MAX_ITEMS,
                      stock_check_max_items: int = DEEJAY_STOCK_CHECK_MAX_ITEMS,
                      seen: dict[str, str] | None = None,
                      now: datetime | None = None,
                      ) -> tuple[list[dict], int, dict[str, str]]:
    """New arrivals scraped from deejay.de's "All / News" page.

    Unlike every other source here, this page carries no per-item posted
    timestamp at all -- there is nothing to compare against a lookback
    cutoff. So "new" is tracked explicitly instead: `seen` is the
    article-id -> first-seen-timestamp map from the previous run (persisted
    to docs/deejay_seen.json and committed by the workflow, the same
    git-as-state pattern already used for docs/likes.json). An article id
    already in `seen` is skipped outright, before genre/format filtering,
    matching how a Discogs listing ages out of the lookback window
    regardless of whether it would otherwise match today's filter -- so
    changing your genre filter later never resurfaces something already
    shown. The third return value is the updated map to persist for next
    time; the caller is responsible for actually writing it (this function
    has no side effects on disk)."""
    # The one fetch this whole source lives or dies on -- worth spending
    # real time retrying, unlike the per-item detail fetches below. This is
    # specifically the fetch that has failed from GitHub Actions with a
    # connect timeout and then succeeded again on a later run with nothing
    # changed -- more attempts, spaced further apart, give a real block or
    # rate limit more chances to have cleared within the same run.
    html = http_get_text(url, user_agent, attempts=5, backoff=(5, 15, 30, 60))
    articles = DEEJAY_ARTICLE_RE.findall(html)

    if not articles:
        LOG.warning(
            "[deejay.de] scraper found 0 items on a page that always has "
            "some -- deejay.de likely redesigned the page and this scraper "
            "needs updating, rather than the shop genuinely having nothing new."
        )

    seen = dict(seen or {})
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat()
    updated_seen = dict(seen)

    matched: list[dict] = []
    considered = 0
    already_seen = 0

    for article_id, block in articles[:max_items]:
        if article_id in seen:
            already_seen += 1
            continue
        updated_seen[article_id] = now_iso
        considered += 1

        title_match = DEEJAY_TITLE_RE.search(block)
        if not title_match:
            continue
        url_path, raw_title = title_match.group(1), title_match.group(2).strip()

        artist_match = DEEJAY_ARTIST_RE.search(block)
        artist = artist_match.group(1).strip() if artist_match else ""
        desc_text = f"{artist} - {raw_title}" if artist else raw_title

        medium_match = DEEJAY_MEDIUM_RE.search(block)
        medium_text = medium_match.group(1).strip() if medium_match else ""
        formats = classify_deejay_format(medium_text, url_path)
        if not matches_format(formats, wanted_formats):
            continue

        # No structured genre data here either -- match against artist,
        # title, tracklist and blurb text instead, same as clone.nl.
        text_block = DEEJAY_TAG_STRIP_RE.sub(" ", block)
        haystack = f"{desc_text} {text_block}"
        if not matches_genre([haystack], wanted_norm):
            continue
        hit_terms = matching_terms(haystack, wanted_norm)

        labelcat_match = DEEJAY_LABELCAT_RE.search(block)
        catno = (labelcat_match.group(1) or "").strip() if labelcat_match else ""
        label = (labelcat_match.group(2) or "").strip() if labelcat_match else ""
        date_match = DEEJAY_DATE_RE.search(block)
        release_note = date_match.group(1).strip() if date_match else ""

        price_match = DEEJAY_PRICE_RE.search(block)
        price = "price on request"
        if price_match:
            try:
                price = f'{float(price_match.group(1).replace(",", ".")):.2f} EUR'
            except ValueError:
                pass

        img_match = DEEJAY_IMG_RE.search(block)

        label_line = " - ".join(p for p in (label, catno) if p)
        if release_note and release_note.lower() != "release unknown":
            label_line = f"{label_line} - released {release_note}" if label_line else f"released {release_note}"

        # Native tracklist first -- free, since the data's already in the
        # block we scraped, and it's deejay.de's own actual audio rather
        # than a possibly-different mix borrowed from Discogs. The Discogs
        # cross-reference only runs as a fallback for the rarer item that
        # has no tracklist of its own (e.g. a single with no listed tracks).
        tracks = extract_deejay_tracks(block)
        if not tracks and api is not None and discogs_lookup_max_items > 0 \
                and len(matched) < discogs_lookup_max_items:
            tracks = find_discogs_videos(api, artist, raw_title, catno)

        item_url = ("https://www.deejay.de" + url_path) if url_path.startswith("/") else url_path

        # Stock status only exists on the item's own detail page -- unlike
        # tracks, this is a genuinely new request, not free, hence its own cap.
        stock_status, stock_note = "", ""
        vinyl_only = False
        if stock_check_max_items > 0 and len(matched) < stock_check_max_items:
            try:
                detail_html = http_get_text(item_url, user_agent)
                stock_match = DEEJAY_STOCKSTATUS_RE.search(detail_html)
                if stock_match:
                    stock_status, stock_note = classify_deejay_stock(*stock_match.groups())
                # Free: same page fetch as the stock check above.
                feature_match = DEEJAY_FEATURE_RE.search(detail_html)
                if feature_match:
                    vinyl_only = "vinyl only" in feature_match.group(1).lower()
            except FeedError as exc:
                # A stock-check failure must not lose the item itself.
                LOG.warning("[deejay] could not check stock for %s: %s", item_url, exc)
            time.sleep(0.5)  # extra per-item request beyond the single page fetch

        matched.append({
            "description": f"{desc_text} ({medium_text})" if medium_text else desc_text,
            "price": price,
            "condition": "New",
            "sleeve": "",
            "url": item_url,
            "tags": ", ".join(hit_terms) if hit_terms else "new arrival",
            "thumb": img_match.group(1) if img_match else "",
            "label": label_line,
            "catno": "",
            "videos": tracks,
            "formats": formats,
            "stock_status": stock_status,
            "stock_note": stock_note,
            "vinyl_only": vinyl_only,
        })

    if already_seen:
        LOG.info("[deejay.de] %d already shown in a prior digest, skipped", already_seen)

    return matched, considered, updated_seen


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
                   wanted_norm: list[str], wanted_formats: list[str],
                   seen: dict[str, str] | None = None,
                   now: datetime | None = None,
                   ) -> tuple[list[dict], int, int, dict[str, str]]:
    """Return (matching items, listings past the date cutoff, listings left
    unchecked because the release-lookup budget ran out, updated seen-map).

    The lookback window alone means the same listing can land in two
    consecutive digests just because the windows overlap -- `seen` (the
    Discogs listing-id -> first-seen-timestamp map from
    docs/discogs_seen.json, same git-as-state pattern as deejay.de's) makes
    "already shown" explicit and permanent instead, so nothing repeats
    purely because less than LOOKBACK_HOURS has passed since it last did.
    """
    seen = dict(seen or {})
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat()
    updated_seen = dict(seen)

    matched: list[dict] = []
    considered = 0
    unchecked = 0
    already_seen = 0

    for listing in fetch_recent_listings(api, username, cutoff):
        listing_id = str(listing.get("id") or "")
        if listing_id and listing_id in seen:
            already_seen += 1
            continue
        if listing_id:
            updated_seen[listing_id] = now_iso
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
            # Vinyl with nothing else bundled -- a release tagged ["Vinyl",
            # "File"] ships with a download code, so is not vinyl-only.
            "vinyl_only": (info.get("formats") or []) == ["Vinyl"],
        })

    if already_seen:
        LOG.info("[%s] %d already shown in a prior digest, skipped", username, already_seen)
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
    return matched, considered, unchecked, updated_seen


def build_digest(api: Discogs, sellers: dict[str, str], cutoff: datetime,
                 genres: list[str], formats: list[str],
                 genres_by_store: dict[str, list[str]] | None = None,
                 user_agent: str = USER_AGENT,
                 clone_rss_enabled: bool = CLONE_RSS_ENABLED,
                 clone_audio_max_items: int = CLONE_AUDIO_MAX_ITEMS,
                 deejay_enabled: bool = DEEJAY_ENABLED,
                 deejay_max_items: int = DEEJAY_MAX_ITEMS,
                 deejay_discogs_max_items: int = DEEJAY_DISCOGS_LOOKUP_MAX_ITEMS,
                 deejay_stock_max_items: int = DEEJAY_STOCK_CHECK_MAX_ITEMS,
                 deejay_seen: dict[str, str] | None = None,
                 discogs_seen: dict[str, str] | None = None,
                 clone_seen: dict[str, str] | None = None,
                 now: datetime | None = None,
                 ) -> tuple[list[tuple[str, list[dict], str | None]], dict]:
    """Each section is (display_name, matched_items, genre_label).

    genre_label is None when the source used the global genre filter (the
    common case, rendered with no extra note), or a string describing its
    own override when genres_by_store applies to it -- so the digest can
    show, right under that heading, that it was filtered differently rather
    than leaving that silent.

    Discogs sellers, clone.nl's RSS feed and the deejay.de scrape all feed
    into the same sections/stats here, because they all end up producing the
    same item shape (see collect_seller / fetch_clone_rss / fetch_deejay_html)
    -- which is what lets render_html/render_player_page/render_text stay
    completely unaware that three different fetch mechanisms exist.
    """
    genres_by_store = genres_by_store or {}
    default_norm = [n for n in (normalise_tag(g) for g in genres) if n]
    wanted_formats = [f.lower() for f in formats if f]
    sections: list[tuple[str, list[dict], str | None]] = []
    stats = {"considered": 0, "matched": 0, "unchecked": 0, "failed_sellers": [],
             "deejay_seen": dict(deejay_seen or {}),
             "discogs_seen": dict(discogs_seen or {}),
             "clone_seen": dict(clone_seen or {})}

    def genre_filter_for(key: str) -> tuple[list[str], str | None]:
        override = genres_by_store.get(key)
        if override is not None:
            norm = [n for n in (normalise_tag(g) for g in override) if n]
            label = ", ".join(override) if override else "everything (no genre filter)"
            return norm, label
        return default_norm, None

    def record(display_name: str, matched: list[dict], considered: int,
              unchecked: int, genre_label: str | None) -> None:
        stats["considered"] += considered
        stats["matched"] += len(matched)
        stats["unchecked"] += unchecked
        if matched:
            sections.append((display_name, matched, genre_label))

    for username, display_name in sellers.items():
        seller_norm, genre_label = genre_filter_for(username)
        LOG.info("Checking %s (%s)%s...", display_name, username,
                 f" [genres: {genre_label}]" if genre_label is not None else "")
        try:
            matched, considered, unchecked, updated_discogs_seen = collect_seller(
                api, username, display_name, cutoff, seller_norm, wanted_formats,
                seen=stats["discogs_seen"], now=now,
            )
            stats["discogs_seen"] = updated_discogs_seen
        except DiscogsError as exc:
            # A dead store must not take the whole digest down.
            LOG.error("[%s] FAILED: %s", username, exc)
            stats["failed_sellers"].append(username)
            continue
        except Exception as exc:  # noqa: BLE001 - last line of defence per seller
            LOG.exception("[%s] unexpected error: %s", username, exc)
            stats["failed_sellers"].append(username)
            continue
        record(display_name, matched, considered, unchecked, genre_label)

    if clone_rss_enabled:
        norm, genre_label = genre_filter_for(CLONE_RSS_KEY)
        LOG.info("Checking Clone.nl (new arrivals, RSS)%s...",
                 f" [genres: {genre_label}]" if genre_label is not None else "")
        try:
            matched, considered, updated_clone_seen = fetch_clone_rss(
                cutoff, norm, wanted_formats, user_agent, audio_max_items=clone_audio_max_items,
                seen=stats["clone_seen"], now=now,
            )
            stats["clone_seen"] = updated_clone_seen
            record("Clone.nl (new arrivals)", matched, considered, 0, genre_label)
            LOG.info("[clone-rss] %d new listing(s) in window, %d matched",
                     considered, len(matched))
        except FeedError as exc:
            LOG.error("[clone-rss] FAILED: %s", exc)
            stats["failed_sellers"].append("clone.nl RSS")
        except Exception as exc:  # noqa: BLE001
            LOG.exception("[clone-rss] unexpected error: %s", exc)
            stats["failed_sellers"].append("clone.nl RSS")

    if deejay_enabled:
        norm, genre_label = genre_filter_for(DEEJAY_KEY)
        LOG.info("Checking deejay.de (new arrivals, scraped)%s...",
                 f" [genres: {genre_label}]" if genre_label is not None else "")
        try:
            matched, considered, updated_seen = fetch_deejay_html(
                norm, wanted_formats, user_agent, api=api, max_items=deejay_max_items,
                discogs_lookup_max_items=deejay_discogs_max_items,
                stock_check_max_items=deejay_stock_max_items,
                seen=deejay_seen, now=now,
            )
            stats["deejay_seen"] = updated_seen
            record("deejay.de (new arrivals)", matched, considered, 0, genre_label)
            LOG.info("[deejay] %d item(s) considered, %d matched", considered, len(matched))
        except FeedError as exc:
            LOG.error("[deejay] FAILED: %s", exc)
            stats["failed_sellers"].append("deejay.de")
        except Exception as exc:  # noqa: BLE001
            LOG.exception("[deejay] unexpected error: %s", exc)
            stats["failed_sellers"].append("deejay.de")

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
.price { font-weight: 700; font-size: 15px; color: #e8e8ea; margin-right: 7px; }
.badge {
  display: inline-block; font-size: 11px; font-weight: 600;
  padding: 3px 8px; border-radius: 10px; margin: 0 5px 4px 0;
  background: #202027; color: #9a9aa2;
}
.badge.format { background: #14202b; color: #7fb3e8; }
.badge.preorder { background: #2b2210; color: #e0af52; }
.badge.outofstock { background: #2b1414; color: #e08a8a; }
.badge.vinylonly { background: #2b2410; color: #e0c452; font-weight: 700; }
.fineprint { color: #6f6f78; font-size: 11.5px; margin-top: 3px; }
.tags { color: #5f5f68; font-size: 12px; margin-top: 3px; margin-bottom: 9px; }
.buy {
  display: inline-block; font-size: 12px; color: #6ea8ff;
  text-decoration: none; margin-left: 10px;
}
button.likebtn {
  background: transparent; border: 0; cursor: pointer; font-size: 15px;
  color: #6a6a72; padding: 0 0 0 8px; line-height: 1; vertical-align: middle;
  font-family: inherit;
}
button.likebtn:hover { color: #e05a5a; }
button.likebtn.liked { color: #e05a5a; }
button.likebtn:disabled { opacity: .5; cursor: default; }
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

# Two playback backends share one page: a hidden YouTube player for Discogs
# releases (no direct audio file exists for those, only community-submitted
# YouTube links), and a plain <audio> element for sources that expose real
# MP3 preview clips directly (clone.nl). Every bar just says which backend it
# needs via data-yt or data-src; playFrom() below is the only place that
# needs to know both exist, so only one plays at a time regardless of which
# backend it came from.
#
# The <audio> element needs none of the YouTube player's setup ceremony --
# no async script load, no playsinline/autoplay-policy workaround, no
# "player ready" gate -- native <audio> just works. The YouTube player is
# still created once on page load rather than on first click, for the same
# reason as before: building the iframe from scratch on first tap is slow
# and risks losing the "user gesture" mobile browsers require before they'll
# allow audio to start.
PLAYER_JS = """
(function () {
  var ytPlayer = null, ytReady = false, ytPendingInit = null;
  var audioEl = new Audio();
  audioEl.preload = 'none';

  var activeEngine = null;  // 'yt' | 'audio' | null
  var activeBar = null, activeVideoId = null, activeSrc = null;
  var pendingSeekFraction = null;
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

  // ---- YouTube backend (Discogs releases) ----

  function pollYt() {
    if (!ytPlayer || !activeBar) return;
    var dur = 0, cur = 0, state = -1;
    try {
      dur = ytPlayer.getDuration() || 0;
      cur = ytPlayer.getCurrentTime() || 0;
      state = ytPlayer.getPlayerState();
    } catch (err) { return; }
    if (pendingSeekFraction !== null && dur > 0) {
      var target = pendingSeekFraction; pendingSeekFraction = null;
      ytPlayer.seekTo(target * dur, true);
      cur = target * dur;
    }
    var effectiveDur = dur || knownDuration(activeBar);
    var frac = effectiveDur > 0 ? cur / effectiveDur : 0;
    paint(activeBar, frac, fmt(cur), dur > 0 ? fmt(dur) : durLabel(activeBar),
          state === 1 ? 'playing' : (state === 3 ? 'loading' : 'paused'));
  }

  function ensureYt(cb) {
    if (ytPlayer) { cb(); return; }
    if (!ytReady) { ytPendingInit = cb; return; }
    var host = document.getElementById('yt-audio-host');
    if (!host) return;
    ytPlayer = new YT.Player(host, {
      height: '113', width: '200',
      playerVars: { playsinline: 1, controls: 0, disablekb: 1, rel: 0, modestbranding: 1 },
      events: {
        onReady: function () { cb(); },
        onStateChange: function (ev) {
          if (ev.data === YT.PlayerState.ENDED && activeEngine === 'yt' && activeBar) {
            var d = ytPlayer.getDuration() || knownDuration(activeBar);
            paint(activeBar, 1, fmt(d), fmt(d), 'paused');
            stopPolling();
          }
        },
        onError: function () {
          if (activeEngine === 'yt' && activeBar) {
            var r = row(activeBar);
            if (r) r.classList.add('errored');
          }
        }
      }
    });
  }

  window.onYouTubeIframeAPIReady = function () {
    ytReady = true;
    if (ytPendingInit) { var cb = ytPendingInit; ytPendingInit = null; cb(); }
  };

  function playYtFrom(bar, fraction) {
    var videoId = bar.getAttribute('data-yt');
    ensureYt(function () {
      if (activeBar && activeBar !== bar) resetBar(activeBar);
      if (activeEngine !== 'yt' || activeVideoId !== videoId) {
        activeEngine = 'yt'; activeVideoId = videoId; activeSrc = null; activeBar = bar;
        var known = knownDuration(bar);
        pendingSeekFraction = fraction > 0.01 ? fraction : null;
        ytPlayer.loadVideoById(videoId);
        // Best-effort immediate jump using Discogs' own track length, so a
        // click deep into a bar does not sit at 0:00 waiting for YouTube's
        // own metadata to arrive. The poll loop re-seeks once the real
        // duration is confirmed, in case this fires before load is ready.
        if (known > 0 && pendingSeekFraction !== null) {
          try { ytPlayer.seekTo(pendingSeekFraction * known, true); } catch (err) {}
        }
      } else {
        activeBar = bar;
        var dur = ytPlayer.getDuration() || knownDuration(bar);
        if (dur > 0) ytPlayer.seekTo(fraction * dur, true);
        ytPlayer.playVideo();
      }
      stopPolling();
      pollTimer = setInterval(pollYt, 250);
      paint(bar, fraction, fmt(fraction * (ytPlayer.getDuration() || knownDuration(bar))),
            durLabel(bar), 'loading');
    });
  }

  // ---- direct-audio backend (clone.nl, or any source with real MP3s) ----

  function pollAudio() {
    if (!activeBar) return;
    var dur = audioEl.duration || 0;
    var cur = audioEl.currentTime || 0;
    var effectiveDur = dur || knownDuration(activeBar);
    var frac = effectiveDur > 0 ? cur / effectiveDur : 0;
    var state = !audioEl.paused && !audioEl.ended ? 'playing'
               : (audioEl.readyState < 2 ? 'loading' : 'paused');
    paint(activeBar, frac, fmt(cur), dur > 0 ? fmt(dur) : durLabel(activeBar), state);
  }

  audioEl.addEventListener('ended', function () {
    if (activeEngine === 'audio' && activeBar) {
      var d = audioEl.duration || knownDuration(activeBar);
      paint(activeBar, 1, fmt(d), fmt(d), 'paused');
      stopPolling();
    }
  });
  audioEl.addEventListener('error', function () {
    if (activeEngine === 'audio' && activeBar) {
      var r = row(activeBar);
      if (r) r.classList.add('errored');
    }
  });

  function safePlay() {
    var p = audioEl.play();
    if (p && p.catch) p.catch(function () {});  // ignore benign AbortError on rapid re-clicks
  }

  function playAudioFrom(bar, fraction) {
    var src = bar.getAttribute('data-src');
    if (activeBar && activeBar !== bar) resetBar(activeBar);
    if (activeEngine !== 'audio' || activeSrc !== src) {
      activeEngine = 'audio'; activeSrc = src; activeVideoId = null; activeBar = bar;
      audioEl.src = src;
      if (fraction > 0.01) {
        var onMeta = function () {
          audioEl.currentTime = fraction * (audioEl.duration || 0);
          audioEl.removeEventListener('loadedmetadata', onMeta);
        };
        audioEl.addEventListener('loadedmetadata', onMeta);
      }
      safePlay();
    } else {
      activeBar = bar;
      if (audioEl.duration) audioEl.currentTime = fraction * audioEl.duration;
      safePlay();
    }
    stopPolling();
    pollTimer = setInterval(pollAudio, 200);
    paint(bar, fraction, fmt(fraction * (audioEl.duration || knownDuration(bar))),
          durLabel(bar), 'loading');
  }

  // ---- unified dispatch: only one backend plays at a time ----

  function playFrom(bar, fraction) {
    var isAudio = !!bar.getAttribute('data-src');
    if (isAudio) {
      if (activeEngine === 'yt' && ytPlayer) { try { ytPlayer.pauseVideo(); } catch (err) {} }
      playAudioFrom(bar, fraction);
    } else {
      if (activeEngine === 'audio') { audioEl.pause(); }
      playYtFrom(bar, fraction);
    }
  }

  function togglePlayPause(bar) {
    var isAudio = !!bar.getAttribute('data-src');
    var isActiveTrack = isAudio
      ? (activeEngine === 'audio' && activeSrc === bar.getAttribute('data-src'))
      : (activeEngine === 'yt' && activeVideoId === bar.getAttribute('data-yt'));
    if (!isActiveTrack) { playFrom(bar, 0); return; }
    if (isAudio) {
      if (audioEl.paused) { safePlay(); } else { audioEl.pause(); }
    } else if (ytPlayer) {
      var state = ytPlayer.getPlayerState();
      if (state === 1) { ytPlayer.pauseVideo(); } else { ytPlayer.playVideo(); }
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
      if (activeBar !== el) return;
      var delta = ev.key === 'ArrowRight' ? 5 : -5;
      if (activeEngine === 'audio') {
        audioEl.currentTime = Math.min(audioEl.duration || 1e9, Math.max(0, audioEl.currentTime + delta));
      } else if (activeEngine === 'yt' && ytPlayer) {
        var dur = ytPlayer.getDuration() || knownDuration(el);
        var cur = ytPlayer.getCurrentTime() || 0;
        ytPlayer.seekTo(Math.min(dur, Math.max(0, cur + delta)), true);
      }
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

    likes_href = f"{base_url}/likes.html" if base_url else "likes.html"

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
        f'<div class="topnav"><a href="{e(likes_href)}">&#9825; Likes</a></div>',
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
            # Eager, not lazy: these are small (60px) thumbnails, and lazy
            # loading meant anything below the fold only started fetching
            # once scrolled near -- felt like images loading "on click"
            # rather than as soon as the page opens.
            thumb = (f'<img src="{e(item["thumb"])}" alt="">'
                     if item.get("thumb") else '<img alt="">')

            # Same reasoning as the email: price gets weight, condition/format
            # become badges, label/catno drops to fine print -- rather than
            # one undifferentiated middot-joined line.
            condition_text = item["condition"]
            if item.get("sleeve") and item["sleeve"] != item["condition"]:
                condition_text += f' / {item["sleeve"]} sleeve'
            meta_html = (
                f'<span class="price">{e(item["price"])}</span>'
                f'<span class="badge">{e(condition_text)}</span>'
            )
            if item.get("formats"):
                meta_html += f'<span class="badge format">{e(", ".join(item["formats"]))}</span>'
            if item.get("vinyl_only"):
                meta_html += '<span class="badge vinylonly">&#9733; Vinyl Only</span>'
            if item.get("stock_note"):
                stock_class = "preorder" if item.get("stock_status") == "preorder" else "outofstock"
                meta_html += f'<span class="badge {stock_class}">{e(item["stock_note"])}</span>'

            fine_html = ""
            if item.get("label") or item.get("catno"):
                parts = [p for p in (item.get("label"), item.get("catno")) if p]
                fine_html = f'<div class="fineprint">{e(" - ".join(parts))}</div>'

            tracks = []
            for video in (item.get("videos") or []):
                # Two kinds of playable track share this markup: Discogs
                # releases carry a YouTube id (video["yt"]); clone.nl items
                # carry a direct MP3 URL (video["src"]) instead. Exactly one
                # of the two is set, and it decides which data-* attribute
                # the row gets -- PLAYER_JS picks the backend from that.
                yt_id = video.get("yt")
                audio_src = video.get("src")
                if not yt_id and not audio_src:
                    continue
                title = video["title"]
                if len(title) > 70:
                    title = title[:69].rstrip() + "…"
                dur = video.get("dur") or 0

                if yt_id:
                    engine_attr = f'data-yt="{e(yt_id)}"'
                    watch_url = f"https://www.youtube.com/watch?v={yt_id}"
                    watch_link = (
                        f'<a class="ytlink" href="{e(watch_url)}" target="_blank" '
                        f'rel="noopener noreferrer" title="Watch on YouTube" '
                        f'aria-label="Watch {e(title)} on YouTube">&#8599;</a>'
                    )
                else:
                    engine_attr = f'data-src="{e(audio_src)}"'
                    watch_link = ""

                tracks.append(
                    '<li class="track"><div class="trow">'
                    f'<button class="ppbtn" aria-label="Play {e(title)}">&#9654;</button>'
                    '<div class="bar" tabindex="0" role="slider" aria-valuemin="0" '
                    f'aria-valuemax="100" aria-valuenow="0" aria-label="{e(video["title"])}" '
                    f'{engine_attr} data-dur="{dur}">'
                    '<div class="track"><div class="fill"></div></div></div>'
                    f'<span class="time">0:00 / {fmt_mmss(dur) if dur else "--:--"}</span>'
                    + watch_link +
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

            like_payload = json.dumps({
                "description": item["description"],
                "price": item["price"],
                "url": item["url"],
                "store": store,
                "thumb": item.get("thumb", ""),
                "formats": item.get("formats") or [],
            }, ensure_ascii=False)
            like_btn = (
                '<button class="likebtn" type="button" aria-label="Like" '
                f'aria-pressed="false" data-like-key="{e(item["url"])}" '
                f'data-like-payload="{e(like_payload)}">&#9825;</button>'
            )

            out.append(
                '<div class="rec">' + thumb + '<div class="body">'
                f'<div class="title"><a href="{e(item["url"])}">'
                f'{e(item["description"])}</a></div>'
                f'<div class="meta">{meta_html}'
                f'<a class="buy" href="{e(item["url"])}">{e(buy_button_label(item["url"]))} &rarr;</a>'
                + like_btn + '</div>'
                + fine_html +
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
    # Always included, not gated on base_url, because the like buttons
    # above are always rendered -- gating this would leave those buttons
    # present but silently inert (no handler wired up) whenever
    # PAGES_BASE_URL is unset, e.g. during local testing.
    assets_href = f"{base_url}/assets/likes.js" if base_url else "assets/likes.js"
    likes_script = (
        f'<script>window.DIGEST_BASE_URL={json.dumps(base_url)};</script>'
        f'<script src="{e(assets_href)}"></script>'
    )
    out.append(f'</div><script>{PLAYER_JS}</script>{likes_script}</body></html>')
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


def load_deejay_seen(path: str) -> dict[str, str]:
    """article id -> first-seen ISO timestamp, from the previous run.

    Missing or corrupt file both mean "nothing seen yet" rather than an
    error -- the very first run, and any recovery after manually deleting
    the file, should just work rather than crash.
    """
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        LOG.warning("Could not read %s (%s) - treating as empty", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def save_deejay_seen(path: str, seen: dict[str, str], keep_days: int, today: datetime) -> None:
    """Write the seen-id map back, dropping entries older than keep_days so
    the file doesn't grow forever. An entry with an unparseable timestamp is
    kept rather than dropped -- corrupt data about *whether* something was
    seen should not make it visible again by accident."""
    if not path:
        return
    oldest = today - timedelta(days=keep_days)
    pruned = {}
    for article_id, ts in seen.items():
        parsed = parse_posted(ts)
        if parsed is None or parsed >= oldest:
            pruned[article_id] = ts
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(pruned, handle, indent=2, sort_keys=True)
        handle.write("\n")


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
            'Play every track and save likes from one page &mdash; an e-mail '
            'can\'t do either of those on its own.'
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

                # Price is the thing a reader scans for first, so it gets its
                # own weight; condition/format become small badges rather than
                # running text; label/catno drops to visibly lighter fine
                # print. All three were the same grey/size before, which made
                # the whole line read as one undifferentiated blob.
                price_html = (
                    f'<span style="font-size:15px;font-weight:700;color:#111;'
                    f'margin-right:7px;">{e(item["price"])}</span>'
                )
                condition_text = item["condition"]
                if item["sleeve"] and item["sleeve"] != item["condition"]:
                    condition_text += f' / {item["sleeve"]} sleeve'
                badge = ('display:inline-block;font-size:11px;font-weight:600;'
                         'padding:3px 8px;border-radius:10px;margin:0 5px 4px 0;')
                meta = price_html + (
                    f'<span style="{badge}background:#f0f0f0;color:#555;">'
                    f'{e(condition_text)}</span>'
                )
                if item.get("formats"):
                    meta += (
                        f'<span style="{badge}background:#eef4ff;color:#3a5f9e;">'
                        f'{e(", ".join(item["formats"]))}</span>'
                    )
                if item.get("vinyl_only"):
                    # A distinct gold accent so it reads as a step up from
                    # the neutral format badge, not just a repeat of "Vinyl".
                    meta += (
                        f'<span style="{badge}background:#fff4d6;color:#8a6d00;'
                        f'font-weight:700;">&#9733; Vinyl Only</span>'
                    )
                if item.get("stock_note"):
                    # Not-yet-available is the thing most worth flagging
                    # clearly -- amber for "coming soon", muted red for
                    # genuinely sold out, both visually distinct from the
                    # neutral condition/format badges above.
                    stock_bg, stock_fg = (
                        ("#fdf0d5", "#8a5a00") if item.get("stock_status") == "preorder"
                        else ("#fde8e8", "#a33")
                    )
                    meta += (
                        f'<span style="{badge}background:{stock_bg};color:{stock_fg};">'
                        f'{e(item["stock_note"])}</span>'
                    )

                catno = ""
                if item["label"] or item["catno"]:
                    parts = [p for p in (item["label"], item["catno"]) if p]
                    catno = (f'<div style="color:#999;font-size:11.5px;margin-top:5px;">'
                             f'{e(" - ".join(parts))}</div>')

                out.append(
                    '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
                    'style="width:100%;margin:0 0 20px;"><tr>'
                    + thumb +
                    '<td style="vertical-align:top;">'
                    f'<a href="{e(item["url"])}" '
                    f'style="color:#0b5fff;text-decoration:none;font-weight:600;">'
                    f'{e(item["description"])}</a>'
                    f'<div style="margin-top:5px;">{meta}</div>'
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
    clone_rss_enabled = env_flag("CLONE_RSS_ENABLED", CLONE_RSS_ENABLED)
    clone_audio_max_items = env_int("CLONE_AUDIO_MAX_ITEMS", CLONE_AUDIO_MAX_ITEMS)
    deejay_enabled = env_flag("DEEJAY_ENABLED", DEEJAY_ENABLED)
    deejay_max_items = env_int("DEEJAY_MAX_ITEMS", DEEJAY_MAX_ITEMS)
    deejay_discogs_max_items = env_int(
        "DEEJAY_DISCOGS_LOOKUP_MAX_ITEMS", DEEJAY_DISCOGS_LOOKUP_MAX_ITEMS
    )
    deejay_stock_max_items = env_int(
        "DEEJAY_STOCK_CHECK_MAX_ITEMS", DEEJAY_STOCK_CHECK_MAX_ITEMS
    )
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
    LOG.info("Extra sources: clone.nl RSS %s (audio for up to %d item(s)), deejay.de scrape %s",
             "on" if clone_rss_enabled else "off", clone_audio_max_items,
             "on" if deejay_enabled else "off")

    user_agent = env_str("USER_AGENT", USER_AGENT)
    api = Discogs(
        os.environ["DISCOGS_TOKEN"].strip(),
        user_agent,
        env_int("MAX_RELEASE_LOOKUPS", MAX_RELEASE_LOOKUPS),
        env_int("MAX_VIDEOS_PER_RELEASE", MAX_VIDEOS_PER_RELEASE),
        env_int("MAX_PAGES", MAX_PAGES),
    )

    # Computed here rather than after build_digest, because it doubles as
    # the "now" fed into the deejay.de seen-state below -- one timestamp for
    # the whole run rather than a second, separately-drifted one.
    generated = datetime.now(timezone.utc)
    stamp = generated.strftime("%Y-%m-%d")

    # Explicit "already shown" tracking, one file per source, all using the
    # same git-as-state pattern: deejay.de needs this because it has no
    # per-item posted date to filter on at all (see the DEEJAY_* comments
    # near the top); Discogs sellers and clone.nl's RSS feed DO have a
    # lookback window, but that alone lets the same item land in two
    # consecutive digests purely because the windows overlap -- these two
    # seen-files close that gap for them too. Only meaningful when docs/ is
    # actually being written -- a run with no --player-dir has nowhere to
    # persist the updated state, so nothing here is loaded or saved.
    docs_dir = os.path.dirname(args.player_dir.rstrip("/\\")) or "." if args.player_dir else ""
    deejay_seen_path = os.path.join(docs_dir, "deejay_seen.json") if docs_dir else ""
    discogs_seen_path = os.path.join(docs_dir, "discogs_seen.json") if docs_dir else ""
    clone_seen_path = os.path.join(docs_dir, "clone_seen.json") if docs_dir else ""

    deejay_seen = load_deejay_seen(deejay_seen_path)
    discogs_seen = load_deejay_seen(discogs_seen_path)
    clone_seen = load_deejay_seen(clone_seen_path)
    if docs_dir:
        LOG.info(
            "Already-shown ids loaded: %d deejay.de, %d Discogs, %d clone.nl RSS",
            len(deejay_seen), len(discogs_seen), len(clone_seen),
        )

    started = time.monotonic()
    sections, stats = build_digest(
        api, sellers, cutoff, genres, formats, genres_by_store,
        user_agent=user_agent,
        clone_rss_enabled=clone_rss_enabled,
        clone_audio_max_items=clone_audio_max_items,
        deejay_enabled=deejay_enabled,
        deejay_max_items=deejay_max_items,
        deejay_discogs_max_items=deejay_discogs_max_items,
        deejay_stock_max_items=deejay_stock_max_items,
        deejay_seen=deejay_seen,
        discogs_seen=discogs_seen,
        clone_seen=clone_seen,
        now=generated,
    )
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

            if deejay_seen_path:
                save_deejay_seen(deejay_seen_path, stats["deejay_seen"],
                                 DEEJAY_SEEN_KEEP_DAYS, generated)
            if discogs_seen_path:
                save_deejay_seen(discogs_seen_path, stats["discogs_seen"],
                                 DEEJAY_SEEN_KEEP_DAYS, generated)
            if clone_seen_path:
                save_deejay_seen(clone_seen_path, stats["clone_seen"],
                                 DEEJAY_SEEN_KEEP_DAYS, generated)
            if docs_dir:
                LOG.info(
                    "Already-shown ids now remembered: %d deejay.de, %d Discogs, %d clone.nl RSS",
                    len(stats["deejay_seen"]), len(stats["discogs_seen"]), len(stats["clone_seen"]),
                )
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
    # in GitHub Actions and you actually notice -- except deejay.de, which
    # has repeatedly failed with a connect timeout from Actions specifically
    # while reachable in under a second from elsewhere, then recovered on
    # its own a run or two later with no action taken. That pattern -- a
    # few failures, then fine again -- is what DEEJAY_SOFT_FAIL exists for:
    # still an ERROR in the log and still shown in the digest as "Could not
    # check", just not something to page you over every time it happens.
    deejay_soft_fail = env_flag("DEEJAY_SOFT_FAIL", DEEJAY_SOFT_FAIL)
    hard_failures = [
        s for s in stats["failed_sellers"]
        if not (deejay_soft_fail and s == "deejay.de")
    ]
    if hard_failures:
        LOG.error("Failed sellers: %s", ", ".join(hard_failures))
        return 1
    if stats["failed_sellers"]:
        LOG.warning("Not failing the run for: %s (DEEJAY_SOFT_FAIL is on)",
                   ", ".join(stats["failed_sellers"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
