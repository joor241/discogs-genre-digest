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
MAX_PAGES = 20              # safety cap on inventory pages per seller per run
PER_PAGE = 100              # max allowed by the API

# How many "listen" links to show per release.
#
# Discogs videos are community-contributed, so a well-known record can carry
# dozens -- one Steve Bug 12" had 56, which would swamp the email. This caps
# the list after deduplication. Override with MAX_VIDEOS_PER_RELEASE.
MAX_VIDEOS_PER_RELEASE = 6

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


def env_genres(name: str, default: list[str]) -> list[str]:
    """Comma-separated genre list, e.g. GENRES_INCLUDE="techno,deep house,electro".

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
                 video_limit: int = MAX_VIDEOS_PER_RELEASE) -> None:
        self.session = requests.Session()
        headers = {"User-Agent": user_agent}
        if token:
            headers["Authorization"] = f"Discogs token={token}"
        self.session.headers.update(headers)
        self._last_request_at = 0.0
        self.request_count = 0
        self.lookup_budget = lookup_budget
        self.video_limit = video_limit
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
            # Free: this is the same response we already fetch for genres.
            "videos": extract_videos(data.get("videos"), self.video_limit),
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
    while page <= MAX_PAGES:
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
        "Raise MAX_PAGES if this store really lists >%d items a day.",
        username, MAX_PAGES, MAX_PAGES * PER_PAGE,
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
        videos.append({
            "title": ((item.get("title") or "").strip() or "Listen"),
            "uri": uri,
            "yt": vid,
        })
        if len(videos) >= limit:
            break
    return videos


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


def collect_seller(api: Discogs, username: str, display_name: str,
                   cutoff: datetime, wanted_norm: list[str]) -> tuple[list[dict], int, int]:
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
                 genres: list[str]) -> tuple[list[tuple[str, list[dict]]], dict]:
    wanted_norm = [n for n in (normalise_tag(g) for g in genres) if n]
    sections: list[tuple[str, list[dict]]] = []
    stats = {"considered": 0, "matched": 0, "unchecked": 0, "failed_sellers": []}

    for username, display_name in sellers.items():
        LOG.info("Checking %s (%s)...", display_name, username)
        try:
            matched, considered, unchecked = collect_seller(
                api, username, display_name, cutoff, wanted_norm
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
            sections.append((display_name, matched))

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


def render_html(sections, cutoff: datetime, genres: list[str], stats: dict) -> str:
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

    if not sections:
        out.append(
            '<p style="background:#f5f5f5;padding:14px;border-radius:6px;">'
            'No new matching listings today.</p>'
        )
    else:
        for store, items in sections:
            out.append(
                f'<h2 style="font-size:16px;margin:26px 0 10px;padding-bottom:6px;'
                f'border-bottom:2px solid #111;">{e(store)} '
                f'<span style="color:#888;font-weight:normal;">({len(items)})</span></h2>'
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


def render_text(sections, cutoff: datetime, genres: list[str], stats: dict) -> str:
    """Plain-text alternative. Improves deliverability and keeps the mail
    readable in clients that block HTML."""
    filter_text = ", ".join(genres) if genres else "everything (no filter)"
    lines = [
        "NEW ON DISCOGS",
        f'Listed since {cutoff.strftime("%a %d %b %Y, %H:%M")} UTC',
        f"Filter: {filter_text}",
        "",
    ]
    if not sections:
        lines.append("No new matching listings today.")
    for store, items in sections:
        lines.append(f"{store} ({len(items)})")
        lines.append("-" * len(f"{store} ({len(items)})"))
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
    genres = env_genres("GENRES_INCLUDE", GENRES_INCLUDE)
    lookback = env_int("LOOKBACK_HOURS", LOOKBACK_HOURS)
    if lookback <= 0:
        LOG.warning("LOOKBACK_HOURS must be positive - using %s", LOOKBACK_HOURS)
        lookback = LOOKBACK_HOURS

    if not sellers:
        raise SystemExit("No sellers configured - nothing to check.")

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback)
    LOG.info("Lookback %sh (cutoff %s UTC)", lookback, cutoff.strftime("%Y-%m-%d %H:%M"))
    LOG.info("Genre filter: %s", ", ".join(genres) if genres else "(none - keeping everything)")
    LOG.info("Sellers: %s", ", ".join(sellers))

    api = Discogs(
        os.environ["DISCOGS_TOKEN"].strip(),
        env_str("USER_AGENT", USER_AGENT),
        env_int("MAX_RELEASE_LOOKUPS", MAX_RELEASE_LOOKUPS),
        env_int("MAX_VIDEOS_PER_RELEASE", MAX_VIDEOS_PER_RELEASE),
    )

    started = time.monotonic()
    sections, stats = build_digest(api, sellers, cutoff, genres)
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

    html_body = render_html(sections, cutoff, genres, stats)
    text_body = render_text(sections, cutoff, genres, stats)

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
