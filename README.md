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

All three were verified against the live Discogs API on 2026-08-18.

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

Every digest links to a **player page**: one web page with a play bar per
track, so you can audition the whole day's records without leaving it.

- Live site: **https://joor241.github.io/discogs-genre-digest/**
- Each email links to its own dated page under `/archive/YYYY-MM-DD.html`, so
  an old email still opens the records it was actually about.
- Pages older than `ARCHIVE_KEEP_DAYS` (30) are pruned automatically. Each page
  links to the surviving earlier ones at the bottom.

Clicking a track loads a YouTube embed inline. Only one plays at a time —
starting another stops the first, and clicking the active one again removes the
player entirely, so nothing keeps playing in the background. Esc also stops.

Players are only created when you click. Building the page with 30+ embeds
loaded up front would make it crawl.

**Why a separate page, and not just play inside the email:** every mail client
strips `<script>`, `<iframe>` and `<audio>` before rendering — Gmail included —
so no embedded player can work in any of them. A real web page has no such
limit. The email therefore carries a link, plus per-track YouTube links and a
**Play all** button as a fallback if you would rather not open the page.

> **The player page is public.** GitHub Pages on a free plan is only available
> for public repositories, and the site is readable by anyone who knows the URL
> (private Pages is an Enterprise feature). The page contains public Discogs
> listings and YouTube embeds — no secrets. Your Discogs token and SMTP
> password live in encrypted GitHub Secrets and are never in the repository.
> The pages carry `noindex, nofollow` so search engines skip them, which is not
> access control, just tidiness.

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
| Same record on several days running | Expected with the default 72h window. Set `LOOKBACK_HOURS` to `26` for one-appearance-only. See [Why there's no database](#why-theres-no-database). |

---

## Files

| File | Purpose |
|---|---|
| `discogs_genre_digest.py` | The whole thing. Config block at the top. |
| `requirements.txt` | One dependency: `requests`. |
| `.github/workflows/daily-digest.yml` | The daily schedule and the manual-run form. |
| `.gitignore` | Keeps `.env` and generated HTML out of git. |
