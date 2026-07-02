# Legal Job Monitor

Runs automatically once a day, finds legal internship/associate listings
relevant to a corporate-law transition, scores them with Claude, and
emails you a digest. No manual searching required after setup.

**What it does NOT do:** scrape or automate LinkedIn. LinkedIn actively
blocks and penalizes automated access, and doing so risks account
restriction. Use LinkedIn's native **Job Alerts** feature to get LinkedIn
matches by email — this tool covers everything else (Internshala across 9
categories, Lawctopus, LawFoyer, Bar and Bench, LiveLaw, SCC Online Blog,
and any firm career pages you add).

---

## 1. One-time setup (15–20 minutes)

### a) Create a GitHub repo
1. Create a new **private** repo (e.g. `legal-job-monitor`).
2. Upload all files in this folder, preserving the structure:
   ```
   .github/workflows/daily-job-digest.yml
   scripts/job_monitor.py
   data/profile.json
   data/career_pages.json
   data/seen_listings.json
   requirements.txt
   README.md
   ```

### b) Get an OpenRouter API key
1. Go to https://openrouter.ai/keys → Create Key.
2. Add credit to your OpenRouter account (pay-as-you-go). This is billed
   separately from any Anthropic/claude.ai subscription — each daily run
   scores well under 200 short listings on Claude Sonnet 5 (the default
   model, called via OpenRouter's `anthropic/claude-sonnet-5` slug), typically
   a few cents per day at most. Swap to a cheaper slug like
   `anthropic/claude-haiku-4.5` (set as the optional `OPENROUTER_MODEL`
   secret below) if you want to cut that further.

### c) (Optional but recommended) Set up email delivery via Resend
1. Sign up free at https://resend.com (free tier: 100 emails/day, 3,000/month
   — far more than you need for one daily digest).
2. Verify a sending domain, OR use their default `onboarding@resend.dev`
   sender for testing (works immediately, no domain needed).
3. Create an API key in the Resend dashboard.

If you skip this step, the digest still gets written to `output/digest.md`
in your repo every day — you can just check the repo instead of email.

### d) Add secrets to your GitHub repo
Go to your repo → **Settings → Secrets and variables → Actions → New repository secret**.
Add:

| Secret name | Value |
|---|---|
| `OPENROUTER_API_KEY` | your OpenRouter API key |
| `OPENROUTER_MODEL` | (optional) an OpenRouter model slug, e.g. `anthropic/claude-haiku-4.5`; defaults to `anthropic/claude-sonnet-5` if unset |
| `RESEND_API_KEY` | your Resend API key (optional) |
| `DIGEST_TO_EMAIL` | your personal email address (optional) |
| `DIGEST_FROM_EMAIL` | `onboarding@resend.dev` or your verified sender (optional) |

### e) Customize your profile
Edit `data/profile.json` to tune what Claude scores highly — update if your
target areas or preferences shift.

`data/career_pages.json` ships with 10 verified Tier-1/Tier-2 firm career
pages already filled in (CAM, Shardul Amarchand Mangaldas, Trilegal,
Khaitan & Co, AZB, JSA, IndusLaw, Saraf and Partners, IC Universal Legal,
and Elevate). **This is the single highest-signal source** — it catches
postings that never make it to any aggregator. Add more firms to the list
in the same `{"name": ..., "url": ...}` shape; the digest will flag in its
issues section if this file is ever empty, since an empty list silently
contributes zero listings.

---

## 2. Test it manually

Before waiting for the schedule, trigger it by hand:
1. Go to your repo → **Actions** tab → **Daily Legal Job Digest** → **Run workflow**.
2. Wait ~1-2 minutes, then check `output/digest.md` in your repo (or your
   inbox if email is configured).

---

## 3. How it runs going forward

- Fires automatically every day at 03:00 UTC (08:30 IST) via GitHub Actions —
  completely free for a private repo at this frequency (GitHub gives 2,000
  free Action-minutes/month; this uses a few minutes/day, well under the
  free tier).
- Only **new** listings (not seen in previous runs) get scored and included,
  so you won't get the same posting twice. Entries older than 60 days are
  pruned from the tracking file automatically so it doesn't grow forever.
- Each listing gets a 0-10 fit score + one-line reasoning from Claude.
- Listings scoring 5+ are shown up top; lower-scoring ones are collapsed
  below so you can still skim them if you want.
- If a listing's score genuinely fails to come back (e.g. a transient
  OpenRouter/model API error), it's shown in its own "needs manual review"
  section at the top — never silently buried as if it scored 0.
- If any source fails, comes back empty unexpectedly, or a fetcher crashes
  outright, that's now surfaced as a collapsible "issues this run" section
  at the top of the digest itself (and in the email subject line) — you
  don't have to go dig through Actions logs to notice something broke.
- One failing source can no longer take down the whole run — every source
  fetch is isolated, retried up to 3 times with backoff, and any failure is
  logged as an issue rather than crashing the script.

**Sources currently covered:**
- **Internshala** — swept across 9 keyword categories (legal, corporate-law,
  legal-research, law, compliance, contract, company-secretary, paralegal,
  IP law) rather than just "legal", so postings filed under adjacent
  categories aren't missed.
- **Lawctopus** and **LawFoyer** — via their RSS feeds (far more stable than
  scraping HTML, since RSS structure doesn't break when a site changes
  theme), with an HTML-scrape fallback if a feed ever moves.
- **Manupatra Academy** — best-effort; see known limitation below.
- **Bar and Bench**, **LiveLaw**, and **SCC Online Blog** — these are news/
  journal sites, filtered down to just job-relevant titles. Most people
  never think to check these for postings, so listings caught here face
  less competition than the same posting on Internshala.
- **Firm career pages** (`data/career_pages.json`) — direct scrape of each
  firm's own careers page, filtered to job-shaped link text rather than
  generic nav links. This is the source most likely to surface a listing
  before it appears anywhere else.

**Known limitation — Manupatra Academy:** that site loads its listing table
via JavaScript after the page loads, and gates full details behind a login.
A plain fetch (what this tool does) usually can't see the actual listings —
you'll see a `0 listings` log line for that source most days. It's included
so the pipeline is ready if Manupatra ever exposes a public data endpoint,
but for now, check
https://www.manupatracademy.com/internships/law-student-opportunities
manually every so often, or sign in there directly.

To change the schedule, edit the `cron` line in
`.github/workflows/daily-job-digest.yml` (uses standard cron syntax, UTC).

---

## 4. Adding more sources later

Each source is its own function in `scripts/job_monitor.py`
(`fetch_internshala`, `fetch_lawctopus`, etc.). To add a new bot-friendly
source, write a new `fetch_x()` function that returns a list of dicts shaped
like:
```python
{"source": "SiteName", "title": "...", "company": "...", "location": "...", "url": "..."}
```
then add a call to it inside `main()`, wrapped in `run_fetcher_safely(...)`
so a bug in your new fetcher can't take down the whole run.

If the new source is a WordPress-style blog/news site, check for a `/feed/`
RSS endpoint first (most WordPress sites have one) and use
`fetch_wp_rss(source_name, feed_url, keyword_filter=[...])` — it's already
built and is far more stable than scraping HTML, since RSS structure
doesn't break on theme changes the way CSS selectors do.

Call `report_issue(source_name, message)` from inside your fetcher whenever
something looks wrong (0 results, a parse failure) — it'll automatically
show up in the digest's issues section instead of only living in logs.

**Do not** add LinkedIn scraping — it's against their Terms of Service and
can get your account flagged or restricted. Use LinkedIn Job Alerts instead
and check those separately.
