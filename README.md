# Discogs record-store genre digest

Once a day, checks a list of record stores' Discogs marketplace inventories for
newly listed records, keeps only the genres you care about, and emails you an
HTML digest.

Runs on GitHub Actions. No server, no database, nothing to maintain.

Currently tracking:

| Store | Website | Discogs seller username |
|---|---|---|
| Rush Hour | rushhour.nl | `RushHour` |
| Clone | clone.nl | `clone.nl` |
| Offbeat Records | — | `offbeat__records` (two underscores) |
| Decks | decks.de | `decks.de` — huge, very active (~40,000 items) |

All four were verified against the live Discogs API (Rush Hour/Clone/Offbeat on
2026-08-18, Decks on 2026-08-23).

Three more stores are checked directly on their own sites rather than through
Discogs — Clone.nl's own new-arrivals RSS feed, a scrape of deejay.de's "News"
page (deejay.de has no feed and their Discogs account is empty), and
Yoyaku's public WordPress/WooCommerce REST API, which gives real genre tags
and direct MP3 previews without any scraping at all. See
[Non-Discogs sources](#non-discogs-sources-clonenl-rss-deejayde-and-yoyakuio)
below.

> **Note on Redlight Records:** their old domain no longer hosts a record shop.
> The Discogs account `Red_Light_Records` exists but had **0 items for sale**
> when checked, so it was left out. If they restock, add them — see
> [Adding a store](#adding-a-store).

---

## What you need

Three accounts, all free:

1. **GitHub** — runs the script on a schedule.
2. **Discogs** — for an API token, so you can read the shops' inventories.
3. **An email account that can send mail** — Gmail is fine.

---

## Setup

### Step 1 — Get this folder onto GitHub

The script only runs automatically once it lives in a GitHub repository.

1. Create a new repository at [github.com/new](https://github.com/new). Give it
   a name like `discogs-genre-digest`. **Private is fine** — scheduled Actions
   work on private repos too.
2. Don't tick "Add a README" — this folder already has one.
3. In this folder, run:

```bash
git init -b main
git config user.name "Your Name"
git config user.email "you@example.com"
git add .
git commit -m "Daily Discogs genre digest"
git remote add origin https://github.com/YOUR-USERNAME/discogs-genre-digest.git
git push -u origin main
```

Replace `YOUR-USERNAME` with your GitHub username. Git will ask you to sign in
on first push.

> `.gitignore` already excludes `.env` and generated HTML, so you won't
> accidentally commit anything secret.

### Step 2 — Get a Discogs API token

1. Sign in to Discogs, then go to
   [discogs.com/settings/developers](https://www.discogs.com/settings/developers).
2. Click **Generate new token** under *Personal access token*.
3. Copy the string. You'll paste it into GitHub in Step 4.

This token only reads public marketplace data. It raises your rate limit from
25 to 60 requests per minute, which is why the script needs it.

### Step 3 — Set up email sending (SMTP)

**What SMTP is:** it's just the standard way a program logs into an email
account and asks it to send a message. You give the script an email address and
a password, and it sends the digest to you from that address. Nothing is
installed — it connects over the internet like your phone's mail app does.

You can use any email provider. **Gmail is the easiest**, but Gmail won't let a
script use your normal password — you need an *App Password*, which is a
16-character password that works only for this one purpose and can be revoked
at any time.

**Getting a Gmail App Password:**

1. Your Google account must have **2-Step Verification turned on**
   ([myaccount.google.com/signinoptions/twosv](https://myaccount.google.com/signinoptions/twosv)).
   App Passwords do not exist without it.
2. Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
3. Type a name like `Discogs digest`, click **Create**.
4. Copy the 16-character password it shows you. Spaces don't matter.

That gives you these values for Step 4:

| Setting | Value for Gmail |
|---|---|
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `465` |
| `SMTP_USER` | your full Gmail address |
| `SMTP_PASS` | the 16-character App Password (**not** your Google password) |
| `MAIL_TO` | where the digest goes — your own address is fine |

<details>
<summary>Using a provider other than Gmail</summary>

The script isn't tied to Gmail. Look up your provider's "SMTP settings" and use
those. Common ones:

| Provider | Host | Port |
|---|---|---|
| Gmail | `smtp.gmail.com` | 465 |
| Fastmail | `smtp.fastmail.com` | 465 |
| Outlook / Microsoft 365 | `smtp-mail.outlook.com` | 587 |
| iCloud Mail | `smtp.mail.me.com` | 587 |
| Proton Mail | requires Proton Mail Bridge | 1025 |

Port **465** connects over SSL. Anything else (**587**, 25) connects and then
upgrades with STARTTLS. The script picks the right one from the port number, so
you only need to set `SMTP_PORT` correctly.

Most providers with 2FA require an app-specific password, same as Gmail.
</details>

### Step 4 — Add the secrets to GitHub

In your repository: **Settings → Secrets and variables → Actions → Secrets tab
→ New repository secret**. Add one at a time:

| Secret name | What to put in it | Required |
|---|---|---|
| `DISCOGS_TOKEN` | the token from Step 2 | yes |
| `SMTP_HOST` | e.g. `smtp.gmail.com` | yes |
| `SMTP_USER` | the email address sending the digest | yes |
| `SMTP_PASS` | the App Password from Step 3 | yes |
| `MAIL_TO` | where the digest goes. Comma-separate for several | yes |
| `SMTP_PORT` | defaults to `465` if you leave it out | no |
| `MAIL_FROM` | defaults to `SMTP_USER` if you leave it out | no |

Secrets are write-only — GitHub will never show them again, and they're masked
in logs. That's the right place for these.

### Step 5 — Test it

Don't wait for tomorrow morning.

1. Go to the **Actions** tab. If GitHub asks you to enable workflows, do that.
2. Pick **Daily Discogs digest** in the left sidebar.
3. Click **Run workflow**. A small form drops down.
4. **First run: tick `dry_run`.** That builds the digest and prints it in the
   log without sending any email — so you can confirm the Discogs half works
   before debugging the email half.
5. Click the green **Run workflow** button and watch the log.

If the dry run looks right, run it again with `dry_run` unticked. The email
should arrive within a minute. **Check your spam folder** on the first one.

Every run also attaches the rendered digest as a downloadable `digest-html`
artifact at the bottom of the run page, so you can see exactly what the email
looked like.

---

## Changing what you get

### The settings page — edit everything from your phone

**https://joor241.github.io/discogs-genre-digest/settings.html**

A page for adding/removing stores and genres and triggering a run, without
opening GitHub's own UI at all. It's part of the same site as the player page
(link to it from there too), works on mobile, and needs
[the player page turned on](#turning-the-player-page-on) first since it's
served the same way.

It edits the exact same repo Variables the two methods below describe by hand
(`GENRES_INCLUDE`, `FORMATS_INCLUDE`, `SELLERS`, `LOOKBACK_HOURS`,
`MAX_RELEASE_LOOKUPS`) — so use
whichever is more convenient, they're not different systems. Store rows have a
**Check** button that queries Discogs live and shows how many items that
seller currently has for sale, the same sanity check described in
[Adding a store](#adding-a-store), without leaving the page.

**Why it needs a token, and what that means:** the page itself is static
(just a file GitHub serves, like the player page) — it has no server of its
own, so the *only* way it can change your repo's settings is by talking to
GitHub's API directly from your browser, which needs a GitHub access token.
The page asks for one on first use and explains exactly how to create it. Two
things worth knowing:

- The token is a **fine-grained personal access token, scoped to only this one
  repository**, with permission to read/write its Variables and trigger Action
  runs — nothing else. It cannot touch any other repo, and cannot read code,
  issues, or anything not explicitly granted.
- It's sent straight from your browser to `api.github.com` and stored only in
  that browser's own local storage (or session storage if you don't tick
  "remember") — never to any server this project runs, because there isn't
  one. Since **the page is public** (same reasoning as the player page — see
  the note in [Listening to the records](#listening-to-the-records)), anyone
  with physical or malware access to that specific browser profile could in
  principle read it back out of storage. If that's ever a concern, click
  **Forget token**, and revoke it at
  [github.com/settings/tokens](https://github.com/settings/tokens) — since
  it's scoped to one repo, the blast radius of a leaked token is small either
  way.

### Genres — from the GitHub UI, no code editing

Genre filtering matches **whole words, case-insensitively**, against the
release's genres *and* styles. So `house` catches Deep House, Acid House, Tech
House and Hard House; `techno` catches Dub Techno, Minimal Techno and Hard
Techno. Punctuation is ignored, so `italo` catches Italo-Disco.

> **Why whole words and not plain substrings:** `electro` is a substring of
> `Electronic`, and Discogs tags nearly every dance record with the genre
> `Electronic`. Under substring matching, adding `electro` to your list would
> silently match *everything* and switch your filter off without any error.
> Whole-word matching means `electro` gets you Electro and Electro House, but
> not every record tagged Electronic.
>
> The flip side: terms only match complete words. `dub` matches Dub and Dub
> Techno but not Dubstep — add `dubstep` separately if you want it.

The shipped default covers what these three shops actually stock:

```
techno, house, electro, acid, disco, italo, new beat, ebm, breakbeat, trance
```

Three ways to change it, in priority order:

**1. For one run only** — Actions → Run workflow → type into the `genres` box:

```
techno, deep house, electro
```

**2. Permanently, from the web UI** — Settings → Secrets and variables →
Actions → **Variables** tab → New repository variable:

- Name: `GENRES_INCLUDE`
- Value: `techno, house, electro, disco`

This is a *variable*, not a secret — it's not sensitive, and unlike a secret you
can read and edit it later. This is the setting to use for day-to-day tweaking.

**3. In the code** — edit `GENRES_INCLUDE` near the top of
`discogs_genre_digest.py` and push.

To turn filtering off entirely and receive **everything** these shops list, set
the value to `all`. Be warned: all three shops list daily and Offbeat has 16,000+
items, so expect a very long email.

The same pattern works for `LOOKBACK_HOURS` (how many hours back to look) and
`SELLERS`.

#### Different genres for different stores

`GENRES_INCLUDE` applies to every store by default. To use different terms for
one store instead — e.g. Offbeat leans trance/new-beat while Clone leans house —
set the `GENRES_BY_STORE` repo variable:

```
offbeat__records: trance, new beat; clone.nl: house
```

Format: `username: term, term; username2: term`, semicolons between stores,
commas between that store's terms. A store **not** listed here just keeps using
the global `GENRES_INCLUDE` filter — this replaces it for that store, it doesn't
add to it. Give a store's list as `all` to turn off genre filtering for just
that one store while everyone else keeps the global filter:

```
offbeat__records: all
```

When a store has its own filter, both the emailed digest and the player page
show a small "— filter: ..." note under that store's heading, so it's visible
at a glance which stores are using something different from the rest.

The settings page (`settings.html`) has a **Genres** column per store in the
same editor used for adding stores — leave it blank to use the global filter.

### Formats — filtering to vinyl

Every listing shows its format(s) (Vinyl, CD, Cassette, File, ...) right next
to the price and condition. By default, only **Vinyl** is kept.

This comes from the release's own `formats` field on Discogs — the same
release-detail lookup already made for genre filtering, so checking format
costs no extra API calls. A release can legitimately carry more than one
format (e.g. a 12" that ships with a download code is tagged both `Vinyl` and
`File`) — it's kept if it matches *any* of the formats you list.

Change it the same three ways as genres — repo variable `FORMATS_INCLUDE`,
or a one-off in the code. There's no per-run dispatch-form input for it (the
manual "Run workflow" form only overrides genres/lookback), since format is
more of a set-once-and-forget preference than something you'd want to try
differently on a single run.

```
FORMATS_INCLUDE = Vinyl              # default: vinyl only
FORMATS_INCLUDE = Vinyl, CD          # vinyl or CD
FORMATS_INCLUDE = all                # no format filtering at all
```

Matching is case-insensitive but otherwise exact against Discogs' own format
names — a small fixed vocabulary, so unlike genres there's no whole-word
subtlety to worry about.

### Adding a store

You need the store's **Discogs seller username**, which is often *not* their
website name.

1. Search for the shop on Discogs, or open any record they're selling.
2. Click through to their shop. The URL looks like
   `discogs.com/seller/SOMENAME/profile` — `SOMENAME` is what you need.
3. Sanity-check it in a browser:
   `https://api.discogs.com/users/SOMENAME/inventory?per_page=1`
   You should get JSON back. If `pagination.items` is `0`, they have nothing
   for sale and there's no point adding them.

Then either edit `SELLERS` in the script:

```python
SELLERS = {
    "RushHour": "Rush Hour",
    "clone.nl": "Clone",
    "offbeat__records": "Offbeat Records",
    "SOMENAME": "Their Shop Name",   # <- new
}
```

…or set a `SELLERS` repo variable without touching code, using
`username=Display Name` pairs:

```
RushHour=Rush Hour, clone.nl=Clone, SOMENAME=Their Shop Name
```

Note the variable **replaces** the built-in list rather than adding to it, so
include every store you want.

### Non-Discogs sources: clone.nl RSS, deejay.de and yoyaku.io

Three more sources run alongside the Discogs sellers, using the same genre and
format filters, but fetched directly from each shop's own site rather than
through the Discogs API:

- **Clone.nl new arrivals** — their own RSS feed
  (`clone.nl/rss/all`). This is a real, publisher-provided feed, separate from
  whatever `clone.nl` shows under Sellers above (that's their Discogs
  marketplace listings — a different, smaller catalogue). Both can appear as
  separate sections without duplicating each other.
- **deejay.de new arrivals** — scraped from their "All / News" page.
  deejay.de has **no RSS/Atom feed** (checked directly against the live site)
  and their Discogs seller account exists but has 0 items for sale, so
  scraping their page's HTML was the only option that actually works. That
  makes this source meaningfully more fragile than everything else here: it
  depends on deejay.de's page markup rather than a format they've committed to
  keeping stable, and **will** break if they redesign that page. If it does,
  the log shows a specific warning ("scraper found 0 items on a page that
  always has some") rather than failing silently, so it's noticeable. Turn it
  off with the `DEEJAY_ENABLED` variable (below) if that happens and you don't
  want to wait for a fix. Their tracklist DOES play inline, real audio, same
  as clone.nl — each track's own MP3 lives at a predictable static URL
  (confirmed with a real browser network capture, then verified live over
  plain HTTP), so it's fetched directly rather than needing anything
  special. For the rare item with no tracklist of its own, this falls back
  to cross-referencing Discogs' release search and borrowing its
  community-submitted YouTube links when a confident match is found (see
  [Listening to the records](#listening-to-the-records)) — that fallback
  path needs `DISCOGS_TOKEN`; items that hit neither just keep the
  existing "search YouTube" fallback link.
- **Yoyaku (Paris) new arrivals** — their public WordPress/WooCommerce REST
  API. This is the best-behaved source here and the only one that needs no
  scraping at all: everything comes from documented JSON endpoints rather
  than from regexes over markup that can be redesigned away. It also gives
  three things the other two can't:
  - **Real genre tags.** Yoyaku tags every release with its own *Styles*
    ("House", "Deep House", "Electro", …), so this source filters on actual
    metadata, exactly like Discogs does — not on whether a word happens to
    appear in the blurb.
  - **Server-side date filtering.** The lookback window costs one request no
    matter how much they listed that day.
  - **Direct MP3s.** Their player's own endpoint hands back per-track MP3
    URLs on a CDN — plain GET, no session needed, and it supports byte
    ranges, so the playbar can seek inside them.

  One caveat: their API exposes no **format** field (7"/12"/LP appears only
  in the item page's HTML, which this deliberately never fetches), so
  everything from Yoyaku is reported as **Vinyl**. Merch is still excluded —
  slipmats, needles, t-shirts and gift cards carry no Styles tag, and that's
  what's used to tell a record from an accessory — but a rare CD-only
  release would slip through a Vinyl-only filter. That's the cost of not
  fetching an extra page per item just to read one word.

clone.nl and deejay.de have no Discogs-style structured genre/style data, so
for those two genre filtering runs against each item's title and description
**text** instead (the same whole-word matching, just fed prose instead of a
tags array) — functionally the same filtering, just working from different
raw material. Their `tags` line in the digest shows which genre term actually
matched, rather than a real genre/style list, so it's clear the match came
from text rather than metadata. Yoyaku items show their real Styles instead,
same as Discogs items show their real genres.

Freshness also isn't identical across all sources:

| Source | Freshness signal | Precision |
|---|---|---|
| Discogs marketplace | `posted` timestamp | seconds |
| Clone.nl RSS | `pubDate` | whole days (every item observed was midnight) |
| deejay.de scrape | none — tracked explicitly instead | per article id |
| Yoyaku API | `date_gmt` | seconds |

deejay.de's own per-item date looks like a release date rather than "when
added to the shop" (some items literally say "Release unknown"), so it can't
be used for a `LOOKBACK_HOURS`-style cutoff the way the other three sources
are. Instead, `docs/deejay_seen.json` remembers every article id already
shown — committed by the workflow alongside the player pages, same
git-as-state pattern as `docs/likes.json` — and each run skips anything
already in it. So "new" here means "not shown before", not "posted
recently": an id is only ever shown once, full stop, however long ago it
first appeared. Ids are forgotten after `DEEJAY_SEEN_KEEP_DAYS` (default
`90`) days, long enough that they'll have scrolled off deejay.de's own
"News" listing by then regardless.

**Discogs sellers, clone.nl's RSS feed and Yoyaku get the same "shown once"
treatment, for a different reason.** They do have a real per-item date, so
`LOOKBACK_HOURS` alone bounds what gets checked each run — but a listing
posted, say, 20 hours ago is *inside* both today's 48-hour window and
tomorrow's, so it would otherwise show up twice purely because the windows
overlap. `docs/discogs_seen.json`, `docs/clone_seen.json` and
`docs/yoyaku_seen.json` close that gap the same way `deejay_seen.json` does:
once a listing has appeared in a digest, it never appears again, no matter
how the lookback window keeps sliding over it. Same `DEEJAY_SEEN_KEEP_DAYS`
retention applies to all four files.

**Turning any of them off** — set a repo Variable to `false`:

- `CLONE_RSS_ENABLED` = `false`
- `DEEJAY_ENABLED` = `false`
- `YOYAKU_ENABLED` = `false`

All three default to on.

**deejay.de failing doesn't fail the whole run.** Observed live, repeatedly:
deejay.de answers in under a second from a plain connection, but the exact
same request from GitHub Actions has failed with a connect timeout several
times in a row, then started working again on its own a run or two later —
consistent with the runner's datacenter IP range being rate-limited or
blocked rather than deejay.de actually being down. Retrying more or waiting
longer doesn't help a deliberate block, and it isn't something you can fix
from here, so `DEEJAY_SOFT_FAIL` (default **on**) keeps this source's
failures from flipping the run red every time it happens: still logged as an
error, still shown in the digest as "Could not check: deejay.de", just not
urgent. Set it to `false` to go back to a hard failure for deejay.de too. `DEEJAY_MAX_ITEMS` (default `60`) caps how many of the
page's items are considered each run. `CLONE_AUDIO_MAX_ITEMS` (default `30`)
caps how many clone.nl items get their tracklist fetched for inline playback
each run — each one costs an extra request beyond the single feed fetch, so
this bounds the worst case on a very broad filter. Items beyond the cap still
appear in the digest, just without playable tracks. `DEEJAY_DISCOGS_LOOKUP_MAX_ITEMS`
(default `20`) does the same for how many deejay.de items get cross-referenced
against Discogs per run.

Yoyaku has the same two caps: `YOYAKU_MAX_ITEMS` (default `120`) bounds how
many of the window's releases are considered if they bulk-list, and
`YOYAKU_AUDIO_MAX_ITEMS` (default `30`) bounds how many get their MP3s
fetched. Everything else about that source is a fixed number of requests
regardless of window size.

Per-store genre overrides (`GENRES_BY_STORE`, see above) work for these too —
use the keys `clone-rss`, `deejay` and `yoyaku`:

```
clone-rss: house; deejay: techno, breakbeat; yoyaku: deep house, minimal
```

Yoyaku's key is worth using: because it filters on real Styles rather than
blurb text, narrow terms behave precisely there. Their vocabulary is the
list at [yoyaku.io/style/](https://yoyaku.io/style/) — House, Techno, Deep
House, Minimal, Electro, Tech House, Breakbeat, Ambient, Acid, Dub Techno
and so on.

#### Stock status (pre-order vs. actually available)

clone.nl and deejay.de's "new arrivals" feeds mix items that are genuinely in
stock with pre-orders and sold-out listings that just haven't been removed
yet — checked live: 9 of 10 sampled clone.nl items, and every sampled
deejay.de item, were "preorder" or "out of stock", not immediately buyable.
Discogs marketplace listings never have this problem (a seller's own listing
is always real, owned inventory), so this only applies to those two sources.

When a matched item isn't immediately available, both the emailed digest and
the player page show a badge next to it — amber "Pre-order" (with an expected
date when the site gives one) or muted red "Out of stock". An item with
neither badge is available now. Nothing is filtered out based on this; it's
shown so you know before clicking through, not hidden.

This is free for clone.nl (the same item page already fetched for inline
audio also carries the stock status) but costs one extra request per item for
deejay.de, where it's only available on the item's own page. Capped by
`DEEJAY_STOCK_CHECK_MAX_ITEMS` (default `20`); set it to `0` to turn the
check off for deejay.de specifically if you'd rather not pay that cost.

Yoyaku costs nothing extra: WooCommerce reports stock in the same batched
call that supplies prices, and "Forthcoming"/"Pre-Order" come from the
product's own categories (a pre-order is technically "in stock" as far as
WooCommerce is concerned, so the flag alone would wrongly call it available
now — both signals are checked).

#### "Vinyl Only" highlight

A gold "★ Vinyl Only" badge marks releases that ship with nothing bundled --
no download code, no CD. This only shows where there's real evidence, never
a guess:

- **Discogs sellers**: derived for free from data already fetched -- a
  release tagged `["Vinyl"]` alone is vinyl-only; `["Vinyl", "File"]` ships
  with a download code, so isn't.
- **deejay.de**: their own "Vinyl Only" feature tag, read from the same
  detail-page fetch already made for the stock check above -- free.
- **clone.nl**: no badge. Checked live and found no equivalent marker on
  their item pages, so this is left unflagged rather than guessed at.
- **Yoyaku**: no badge either. Their API carries no format field at all, so
  there's nothing that would count as evidence — same call as clone.nl.

### Changing the time it runs

Edit the `cron` line in `.github/workflows/daily-digest.yml`:

```yaml
- cron: "0 7 * * *"
```

That's 07:00 **UTC** = 09:00 Amsterdam in summer, 08:00 in winter. GitHub cron
is always UTC and does not follow daylight saving, so the local time shifts by
an hour twice a year. [crontab.guru](https://crontab.guru) is handy for editing
the expression.

GitHub may start scheduled runs a few minutes late when it's busy. The lookback
window absorbs that — see [Why there's no database](#why-theres-no-database)
for how the window and the schedule should be kept in step.

---

## How it works

1. For each store, fetch their inventory sorted newest-listed-first
   (`GET /users/{username}/inventory?sort=listed&sort_order=desc`).
2. Walk down the list until a record older than the cutoff appears, then stop.
   Because it's sorted, everything after that is older too — so a typical run
   reads one page per store, not the whole inventory.
3. The inventory response **doesn't include genre**, so for each record that
   passed the cutoff, fetch `GET /releases/{id}` to read its `genres` and
   `styles`, and keep it if it matches your filter.
4. Render the survivors as HTML and email them, with listen links.

### Listening to the records

Every digest links to a **player page**: one web page with a click-to-seek play
bar per track — tap anywhere along the bar and it plays from that point, like a
podcast app scrubber, rather than opening a YouTube video frame.

- Live site: **https://joor241.github.io/discogs-genre-digest/**
- Each email links to its own dated page under `/archive/YYYY-MM-DD.html`, so
  an old email still opens the records it was actually about.
- Pages older than `ARCHIVE_KEEP_DAYS` (30) are pruned automatically. Each page
  links to the surviving earlier ones at the bottom.

**How the bar works:** tapping it seeks to that point and plays immediately —
for a track you've already tapped once, this is instant; for a track you
haven't touched yet, there's a brief (sub-second, usually) load while YouTube
fetches it, same as the first tap on any streaming app. The bar shows the real
track length up front, taken from Discogs' own video metadata, not guessed.
Only one track plays at a time — starting another stops the first outright.
A small arrow icon next to each bar opens the original video on YouTube, for
when you'd rather watch than just listen. Works with touch as well as a mouse,
and the tap target is taller than the visible line so it's comfortable on a
phone.

There's no real waveform drawn on the bar — YouTube doesn't expose audio data
to embedded players, so a true waveform isn't something this can build. It's
an even progress fill, same idea as any basic scrubber.

Under the hood, one hidden YouTube player (invisible, not a video frame you
see) is created once when the page loads and reused for every track — clicking
a bar just tells it what to load and where to seek, rather than building a new
embed per click. This is also what makes seeking on an already-loaded track
truly instant.

**Not every track plays through YouTube.** Discogs releases only have
community-submitted YouTube links to work with, but clone.nl, deejay.de and
Yoyaku all give up real, direct MP3 preview clips instead — confirmed live on
all three, not assumed — so those tracks play through a plain, native
`<audio>` element: no video platform involved, no metadata-loading delay,
instant seeking on first tap rather than only after the first play. Yoyaku is
the easy one: their player plugin has a public endpoint that simply returns
the MP3 URLs, so there's nothing to parse or guess.
deejay.de's case took real digging: their tracklist "Play" buttons route
through a session-gated internal API in their own player JS, which looked
like a hard wall, but a real browser network capture showed each track's MP3
sits at a predictable, static URL underneath that needs no session at all —
sharded by the item's own numeric id, the same pattern deejay.de already uses
for its cover image URLs.

Whichever backend a track uses is invisible from the outside — same bar, same
tap-to-seek behaviour — except direct-MP3 tracks skip the "watch on YouTube"
arrow, since there's no video to link to. Only one track plays at a time
regardless of which backend it's using; starting a YouTube track stops a
playing MP3 clip and vice versa. The rare deejay.de item with no tracklist of
its own falls back to cross-referencing Discogs' release search and borrowing
its YouTube links when a confident match is found; failing that, it's the
"search YouTube" link, same as any item nobody has attached video links to.
See [Non-Discogs sources](#non-discogs-sources-clonenl-rss-deejayde-and-yoyakuio)
for how those work.

**Why a separate page, and not just play inside the email:** every mail client
strips `<script>`, `<iframe>` and `<audio>` before rendering — Gmail included —
so no embedded player can work in any of them. A real web page has no such
limit. The email itself still carries per-track YouTube links and a **Play
all** button as a fallback if you'd rather not open the page at all.

> **The player page is public.** GitHub Pages on a free plan is only available
> for public repositories, and the site is readable by anyone who knows the URL
> (private Pages is an Enterprise feature). The page contains public Discogs
> listings and YouTube embeds — no secrets. Your Discogs token and SMTP
> password live in encrypted GitHub Secrets and are never in the repository.
> The pages carry `noindex, nofollow` so search engines skip them, which is not
> access control, just tidiness.

**The button under each item's price says where it actually goes.** It reads
"Buy on Discogs" only for Discogs listings; clone.nl, deejay.de and Yoyaku
items say "View on Clone.nl" / "View on Deejay.de" / "View on Yoyaku.io"
instead, since that's genuinely where they link — the label is derived from the link's own URL, so it can never
drift out of sync with where it actually points.

#### Likes, synced across your devices

Tap the heart on any record to save it — visible from a separate **Likes**
page (linked at the top of the player page), grouped by store, on any device.

This needed real syncing rather than a per-browser "favourites" list, since
the point is opening the same likes on your phone and your laptop. There's no
backend to hold that, so likes are stored the same way the player pages
themselves are: as a file in this repo (`docs/likes.json`), read and written
through GitHub's own API rather than a database you'd have to run.

**Viewing likes needs nothing** — `likes.html` just fetches the public JSON
file, so anyone can see what's liked, from any device, without setting
anything up. **Saving or removing a like needs a token** — same one used by
the [settings page](#the-settings-page--edit-everything-from-your-phone),
plus one extra permission: set
**Contents** to **Read and write** too when creating it (see
[Turning the player page on](#turning-the-player-page-on) for the rest of
that walkthrough). Tap a heart without a token connected and it opens the
Likes page instead of silently doing nothing, so it's obvious what's needed.

Each like carries its own snapshot of the record's title, price, thumbnail,
store **and its playable tracks** — not just a link — so the Likes page
renders and plays without needing to re-fetch anything, even after that
record has scrolled out of every digest and archive page.

Tracks play on the Likes page exactly as they do in the digest: same bars,
same keyboard shortcuts, same floating transport. That is not a second
implementation — `docs/assets/player.{css,js}` is written fresh by every
run from the same source the digest pages inline, and `likes.html` loads
it. The player installs its own transport if a page doesn't ship one, so
adding it to another page needs nothing but those two tags.

Likes saved before this existed have no tracks stored, so they show without
a tracklist rather than breaking the row; re-liking the record from a digest
fills it in.

#### Turning the player page on

The player is built and committed on every run, but the **email only links to
it once you switch it on**. Until then the button is simply omitted, so no
digest ever contains a dead link. The per-track YouTube links work regardless.

Three steps, once:

1. **Make the repository public.** Settings → General → Danger Zone → *Change
   visibility* → *Change to public*. GitHub asks you to confirm twice, and the
   second step wants you to type the repository name.
   *Free plans can only serve Pages from a public repository.*
2. **Enable Pages.** Settings → Pages → under *Build and deployment*, set
   **Source** to `Deploy from a branch`, **Branch** to `main`, folder to
   `/docs`, then *Save*. The first build takes a minute or so.
3. **Point the email at it.** Settings → Secrets and variables → Actions →
   **Variables** → *New repository variable*, named `PAGES_BASE_URL`:

   ```
   https://joor241.github.io/discogs-genre-digest
   ```

Then run the workflow once with `dry_run` ticked and check the log says
`Player page will be at ...`. Opening that URL should show the play bars.

To turn the player back off, delete the `PAGES_BASE_URL` variable — the emails
go back to plain YouTube links with no other change.

#### How publishing works

The workflow writes `docs/archive/<date>.html`, copies it to `docs/index.html`,
and pushes to `main`; Pages serves the `docs/` folder. Keeping the site in the
repo means the dated archive persists in git for free, with no second branch.

Two consequences worth knowing:

- Pages takes 30–60 seconds to build after a push, so a link opened the instant
  the email lands may 404 briefly. Reload.
- The publish step runs on dry runs too, so the whole pipeline is testable
  without emailing yourself. A dry run with experimental genres will briefly
  put an odd page on the site; the next run overwrites it.

The links come from the `videos` array on the release, which Discogs users
attach themselves. It arrives in the same API response already fetched for
genre filtering, so this costs **no extra requests**. Coverage measured on a
sample of these shops' listings was 8 out of 8 — when a release has nothing
attached, the link falls back to a YouTube search for the artist and title.

**Play all** uses YouTube's `watch_videos` endpoint, which builds a throwaway
playlist from a list of video ids, so one click plays the record end to end.

Two quirks of community-contributed data are handled: the same clip is often
listed twice (deduplicated by video id), and popular records can carry dozens —
one Steve Bug 12" had 56 — so the list is capped by `MAX_VIDEOS_PER_RELEASE`
(default 6). Raise it with a repo variable if you want more.

Note the clips are whatever people uploaded, so they can include remixes or
live takes that are not on the record itself.

### Why there's no database

Instead of remembering which listings it has already seen, the script asks
"was this listed in the last `LOOKBACK_HOURS` hours?". No state file, nothing
to commit back into the repo from inside the Action.

The trade-off is **repeats**: anything still inside the window when the next
run happens gets sent again. So the window and the schedule are linked.

| `LOOKBACK_HOURS` on a daily schedule | Result |
|---|---|
| `26` (24h + buffer) | Each record appears exactly once. Nothing missed. |
| `72` (the shipped default) | Fuller emails, but a record can appear in 3 digests in a row. |

72 is the default because Rush Hour lists in batches every few days rather than
daily, so a 26-hour window often shows nothing from them.

Worth knowing: a 26-hour window on a daily schedule **never misses anything** —
the windows cover every hour of the day. Widening it doesn't surface extra
records, it re-shows the same ones for longer. If the repeats bother you, the
clean fix is to run less often and keep the window matched to it:

```yaml
- cron: "0 7 */3 * *"   # every 3 days, pairs with LOOKBACK_HOURS = 74
```

That gives fuller emails with no repeats at all.

### Rate limits

Discogs allows 60 requests/minute with a token. The script paces itself to about
one request per second, reads the `X-Discogs-Ratelimit-Remaining` header on
every response, and pauses when the window is nearly spent. It also retries on
429s and 5xx errors with backoff, and caches release lookups so the same record
listed by two shops is only fetched once.

A normal run is a handful of requests and finishes in well under a minute.

### Seeing everything, even thousands

Two safety caps exist purely to stop one bulk-listing seller from making a run
take forever or blow past its API budget:

| Cap | Default | What it bounds |
|---|---|---|
| `MAX_PAGES` | 20 | Inventory pages read per seller (100 listings/page, so 2,000 listings). |
| `MAX_RELEASE_LOOKUPS` | 400 | Release-detail lookups (genre/format checks) per run, across all sellers. |

Both are ordinary repo Variables, same as everything else — set them from the
[settings page](#the-settings-page--edit-everything-from-your-phone) or by
hand. Raise them high enough and effectively nothing gets left out, even for a
seller with a huge catalog (Demonfuzz Records, one of the shops this instance
tracks, has 120,000+ items listed in total).

The real trade-off is time, not money: at Discogs' ~54 req/min pace, checking
a few thousand releases can take over an hour. That's fine — GitHub Actions
minutes are unlimited and free for a public repo no matter how long a job
runs, and the workflow's `timeout-minutes` is set generously (180) to give
a large run room to actually finish rather than getting killed partway through.
If you raise the caps enough that even that isn't enough, raise
`timeout-minutes` in `daily-digest.yml` too.

When a run does hit `MAX_RELEASE_LOOKUPS` before finishing, nothing is silently
dropped without you knowing — the email says how many listings were left
unchecked, and the log names the seller responsible.

### If one store breaks

A store that gets renamed, deleted, or is temporarily failing is logged and
skipped — the other stores still get through and you still get your email, with
a note at the bottom listing what couldn't be checked. The workflow run is
marked red so you notice.

---

## Running it locally

Useful for testing filter changes without waiting for GitHub.

```bash
pip install -r requirements.txt
```

Then, in PowerShell:

```powershell
$env:DISCOGS_TOKEN="your_token_here"
python discogs_genre_digest.py --dry-run --out digest.html
```

`--dry-run` needs only `DISCOGS_TOKEN` — no email settings — and prints the
digest instead of sending it. `--out digest.html` writes the HTML so you can
open it in a browser.

To test the email path too, also set `SMTP_HOST`, `SMTP_USER`, `SMTP_PASS` and
`MAIL_TO`, and drop `--dry-run`.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Missing required environment variable(s): ...` | A secret isn't set, or its name is misspelled. Names are case-sensitive. |
| `HTTP 401 ... check DISCOGS_TOKEN` | Token wrong, expired, or pasted with extra characters. Generate a new one. |
| `HTTP 404 ... check the seller username` | The seller username is wrong or the shop was deleted. Verify with the API URL in [Adding a store](#adding-a-store). |
| `HTTP 403 ... Discogs may be blocking the User-Agent` | Discogs blocks generic user agents. The workflow sets a distinctive one automatically; if you run locally, set `USER_AGENT` to anything identifying. |
| `SMTPAuthenticationError` | For Gmail: you used your Google password instead of an App Password, or 2-Step Verification isn't on. |
| Email never arrives | Check spam. Then check `MAIL_TO` for typos — SMTP accepts the message and drops it silently if the address doesn't exist. |
| Digest is empty every day | Filter may be too narrow, or the shops genuinely listed nothing overnight. Run with `genres` set to `all` once to confirm the pipeline works. |
| Scheduled run never fires | GitHub disables scheduled workflows in repos with no activity for 60 days — push a commit or click Run workflow to re-enable. |
| Same record on several days running | Shouldn't happen any more — `discogs_seen.json`/`clone_seen.json`/`deejay_seen.json` mean every item is shown exactly once, regardless of lookback overlap. If it does, check the workflow log for "Could not write the player page" (a failed publish means that run's seen-file update never got committed, so the next run doesn't know it already showed that item). |
| Tapping the heart on a record does nothing, or reverts with no explanation | Almost always a token that predates the Likes feature and so lacks the `Contents` permission it needs — the failure now shows a clear alert saying exactly this. On Settings, forget the token and generate a new one with `Contents: Read and write` enabled (see [Likes, synced across your devices](#likes-synced-across-your-devices)). |

---

## Files

| File | Purpose |
|---|---|
| `discogs_genre_digest.py` | The whole thing. Config block at the top. |
| `requirements.txt` | One dependency: `requests`. |
| `.github/workflows/daily-digest.yml` | The daily schedule, the manual-run form, and publishing to `docs/`. |
| `.gitignore` | Keeps `.env` and generated HTML out of git. |
| `docs/settings.html` | The live [settings page](#the-settings-page--edit-everything-from-your-phone) — static, no build step. |
| `docs/index.html`, `docs/archive/*.html` | Generated by the workflow on every run. Don't hand-edit — they're overwritten. |
