# Legal Job Monitor

Runs automatically once a day, finds legal internship/associate listings
relevant to a corporate-law transition, scores them with a free LLM, and
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

### b) Get a (free) LLM API key for scoring
Scoring is low-stakes, so it runs on free tiers — **no credit card needed**.
The script tries these in order and uses the first that answers, so you only
need ONE, and adding more makes it resilient to rate-limits / free-roster
changes:

1. **Google Gemini (recommended, most generous free tier)** — go to
   https://aistudio.google.com/apikey, click "Create API key", no card.
   Add it as `GEMINI_API_KEY`. Default model `gemini-2.5-flash`.
2. **Groq** — https://console.groq.com/keys → Create API key, no card.
   Add it as `GROQ_API_KEY`. Default model `llama-3.3-70b-versatile`.
3. **OpenRouter** (your existing setup) — https://openrouter.ai/keys → Create Key.
   The script now defaults to the **free** model `openrouter/free`
   (auto-picks an available `:free` model), which needs **zero credits** — this
   fixes the old `402 Payment Required` error, which happened because a *paid*
   model (`anthropic/claude-sonnet-5`) was called with an empty balance.

Tip: a free OpenRouter account is capped at ~50 free-model requests/day. One
daily run only needs a few batches, so that's fine — but if you add a Gemini
key too, the run simply falls back to Gemini whenever OpenRouter is throttled.

### c) (Optional but recommended) Set up email delivery via Gmail
1. Enable 2-Step Verification on your Google account.
2. Generate an App Password at
   https://myaccount.google.com/apppasswords (16 chars, used only by this tool).
3. Set `GMAIL_SENDER` to your Gmail address, `GMAIL_APP_PASSWORD` to that App
   Password, and `DIGEST_TO_EMAIL` to wherever you want the digest delivered.

If you skip this step, the digest still gets written to `output/digest.md`
in your repo every day — you can just check the repo instead of email.

### d) Add secrets to your GitHub repo
Go to your repo → **Settings → Secrets and variables → Actions → New repository secret**.
Add:

| Secret name | Value |
|---|---|
| `GEMINI_API_KEY` | (recommended) your Google AI Studio key — free tier, no card |
| `GROQ_API_KEY` | (optional) your Groq key — free tier, no card |
| `OPENROUTER_API_KEY` | (optional) your OpenRouter key — works with free models, no credits needed |
| `OPENROUTER_MODEL` | (optional, a repo variable) any OpenRouter model slug; defaults to `openrouter/free` |
| `GMAIL_SENDER` | your Gmail address (optional) |
| `GMAIL_APP_PASSWORD` | 16-char Google App Password (optional) |
| `DIGEST_TO_EMAIL` | your personal email address (optional) |

At least one scoring key (`GEMINI_API_KEY`, `GROQ_API_KEY`, or
`OPENROUTER_API_KEY`) must be set, otherwise every listing comes back
"Not scored".

### e) Customize your profile
Edit `data/profile.json` to tune what the scoring model ranks highly — update if your
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
