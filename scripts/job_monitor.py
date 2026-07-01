#!/usr/bin/env python3
"""
Legal Job Monitor — fetches internship/associate listings relevant to a
zero-to-one PQE corporate law transition (M&A, IBC/NCLT, SEBI, contracts),
scores them with Claude, and emails a daily digest.

Sources (all bot-friendly — no LinkedIn scraping, which violates ToS):
  - Internshala, swept across 9 keyword categories
  - Lawctopus (RSS, HTML fallback)
  - LawFoyer (RSS, HTML fallback)
  - Manupatra Academy (best-effort; usually empty, see README)
  - Bar and Bench, LiveLaw, SCC Online Blog — job-filtered RSS from legal
    news/journal sites most people don't think to check for postings
  - A user-defined list of firm career pages (career_pages.json) — the
    highest-signal source for postings that never reach any aggregator

Delivery:
  - Writes results to output/digest.md (always)
  - Optionally emails via Resend API if RESEND_API_KEY is set

Resilience:
  - Each source fetch is retried with backoff and isolated from the others
    (run_fetcher_safely) — one bad source can't crash the whole run
  - Failures/empty-results are surfaced in the digest itself via
    report_issue(), not just left in logs nobody reads
  - seen_listings.json entries older than SEEN_LISTING_TTL_DAYS are pruned
    automatically so the tracking file doesn't grow forever

Run: python scripts/job_monitor.py
"""

import os
import json
import re
import sys
import time
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output"
SEEN_FILE = ROOT / "data" / "seen_listings.json"
CAREER_PAGES_FILE = ROOT / "data" / "career_pages.json"
PROFILE_FILE = ROOT / "data" / "profile.json"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")  # optional
DIGEST_TO_EMAIL = os.environ.get("DIGEST_TO_EMAIL")  # optional, required if using Resend
DIGEST_FROM_EMAIL = os.environ.get("DIGEST_FROM_EMAIL", "digest@resend.dev")

ANTHROPIC_MODEL = "claude-sonnet-5"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

# A real browser UA. The generic "compatible; ...; personal use" UA gets
# soft-blocked (empty/redirect responses) by several sites in this list.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

MAX_LISTINGS_PER_SOURCE = 40
REQUEST_TIMEOUT = 20
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 3
SEEN_LISTING_TTL_DAYS = 60  # prune entries older than this so the file doesn't grow forever


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return default
    return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def listing_id(url, title):
    return hashlib.sha256(f"{url}|{title}".encode()).hexdigest()[:16]


# Collects (source_name, message) for anything that failed or came back
# empty, so the digest itself can surface it instead of it only living in
# Actions logs nobody checks.
RUN_ISSUES = []


def report_issue(source, message):
    print(f"  [warn] {source}: {message}", file=sys.stderr)
    RUN_ISSUES.append((source, message))


def safe_get(url, retries=MAX_RETRIES, **kwargs):
    """GET with retries + exponential backoff. Treats a 200 with a
    suspiciously tiny body (common soft-block / interstitial pattern) as a
    failure worth retrying too, not just network-level exceptions."""
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, **kwargs)
            resp.raise_for_status()
            if len(resp.text) < 500:
                last_error = f"response body suspiciously small ({len(resp.text)} chars) — possible block page"
                raise requests.RequestException(last_error)
            return resp
        except requests.RequestException as e:
            last_error = str(e)
            if attempt < retries:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue
    print(f"  [warn] fetch failed for {url} after {retries} attempts: {last_error}", file=sys.stderr)
    return None


# ---------------------------------------------------------------------------
# Source fetchers
# ---------------------------------------------------------------------------

# Internshala keyword slugs to sweep. Kept broad on purpose — "hidden"
# postings often sit under an adjacent category (e.g. "company-secretary",
# "compliance") rather than the obvious "legal" or "corporate-law" slugs,
# and a vague "Legal Intern at a startup" posting can be filed under any
# of these. Duplicate listings across keywords are de-duped by the seen-
# listings hash later, so overlap here is free.
INTERNSHALA_KEYWORDS = [
    "legal", "corporate-law", "legal-research", "law", "compliance",
    "contract", "company-secretary", "paralegal", "intellectual-property-law",
]


def _parse_internshala_card(card):
    """Try several selector generations — Internshala re-themes periodically
    and old selectors go stale without erroring, they just silently match
    nothing. Each tuple is (title_selectors, link_selectors, company_selectors,
    location_selectors); first one that finds a title+link wins."""
    selector_generations = [
        ("a.job-title-href, h3.job-internship-name", "a.job-title-href",
         "p.company-name, a.link_display_like_text", "a#location_names, span.location-names"),
        ("div.heading_4_5.profile", "a.view_detail_button",
         "div.company_name", "div.locations"),
        ("a[href*='/internship/detail/']", "a[href*='/internship/detail/']",
         "[class*='company']", "[class*='location']"),
    ]

    for title_sel, link_sel, company_sel, loc_sel in selector_generations:
        title_el = card.select_one(title_sel)
        if not title_el:
            continue
        link_el = card.select_one(link_sel) or title_el
        href = link_el.get("href") if link_el else None
        if not href:
            continue
        title = title_el.get_text(strip=True)
        if not title:
            continue
        company_el = card.select_one(company_sel)
        location_el = card.select_one(loc_sel)
        return {
            "title": title,
            "href": href,
            "company": company_el.get_text(strip=True) if company_el else "Unknown",
            "location": location_el.get_text(strip=True) if location_el else "Not specified",
        }
    return None


def fetch_internshala(keyword="legal"):
    """Internshala public search results (no login required)."""
    url = f"https://internshala.com/internships/{keyword}-internship"
    resp = safe_get(url)
    if not resp:
        report_issue(f"Internshala/{keyword}", "fetch failed after retries")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    listings = []
    cards = soup.select("div.individual_internship, div[id^='job_'], div.internship_meta")[:MAX_LISTINGS_PER_SOURCE]

    for card in cards:
        try:
            parsed = _parse_internshala_card(card)
            if not parsed:
                continue
            href = parsed["href"]
            full_url = href if href.startswith("http") else f"https://internshala.com{href}"
            listings.append({
                "source": f"Internshala ({keyword})",
                "title": parsed["title"],
                "company": parsed["company"],
                "location": parsed["location"],
                "url": full_url,
            })
        except Exception as e:
            print(f"  [warn] parse error on Internshala/{keyword} card: {e}", file=sys.stderr)
            continue

    if not listings and cards:
        report_issue(f"Internshala/{keyword}", "cards found but none parsed — selectors likely stale, needs a look")
    elif not cards:
        report_issue(f"Internshala/{keyword}", "0 cards matched — page structure may have changed")

    print(f"  -> {len(listings)} listings ({keyword})")
    return listings


def fetch_wp_rss(source_name, feed_url, keyword_filter=None):
    """Generic fetcher for WordPress-style legal blogs via their RSS feed.

    RSS is dramatically more stable than scraping the HTML listing page —
    it's a structured, versioned format the site has to keep working for
    its own feed subscribers, so it doesn't break on theme/redesign changes
    the way CSS selectors do. Every WordPress site (Lawctopus, LawFoyer,
    Bar & Bench, LiveLaw, SCC Blog) exposes one at /feed/.

    keyword_filter: optional list of lowercase substrings; if given, only
    items whose title contains at least one are kept. Use this for sources
    that cover more than just jobs (Bar & Bench, LiveLaw, SCC Blog) so we
    don't pull in unrelated case-law news.
    """
    resp = safe_get(feed_url)
    if not resp:
        report_issue(source_name, "RSS fetch failed after retries")
        return []

    listings = []
    try:
        # RSS is XML; use the stdlib parser (no extra dependency like lxml
        # required). Strip a possible BOM/leading whitespace some feeds emit.
        import xml.etree.ElementTree as ET
        root = ET.fromstring(resp.text.strip())
        items = root.findall(".//item")
    except Exception as e:
        report_issue(source_name, f"RSS parse error: {e}")
        return []

    if not items:
        report_issue(source_name, "feed returned 0 items — feed URL may have moved")
        return []

    for item in items[:MAX_LISTINGS_PER_SOURCE]:
        try:
            title_el = item.find("title")
            link_el = item.find("link")
            title = title_el.text.strip() if title_el is not None and title_el.text else None
            href = link_el.text.strip() if link_el is not None and link_el.text else None
            if not (title and href):
                continue
            if keyword_filter and not any(k in title.lower() for k in keyword_filter):
                continue
            listings.append({
                "source": source_name,
                "title": title,
                "company": "See listing",
                "location": "See listing",
                "url": href,
            })
        except Exception as e:
            print(f"  [warn] parse error on {source_name} item: {e}", file=sys.stderr)
            continue

    print(f"  -> {len(listings)} listings")
    return listings


def fetch_lawctopus():
    """Lawctopus jobs/internships via RSS (falls back to HTML scrape if the
    feed ever goes away, since the HTML page structure is a known quantity
    too — belt and suspenders)."""
    print("Fetching Lawctopus...")
    listings = fetch_wp_rss("Lawctopus", "https://www.lawctopus.com/lawctopus-law-jobs/feed/")
    if listings:
        return listings

    # Fallback: HTML scrape of the listings page
    resp = safe_get("https://www.lawctopus.com/lawctopus-law-jobs/")
    if not resp:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    listings = []
    articles = soup.select("article, div.post")[:MAX_LISTINGS_PER_SOURCE]
    for art in articles:
        try:
            link_el = art.select_one("h2 a, h3 a, a.entry-title-link")
            if not link_el:
                continue
            title = link_el.get_text(strip=True)
            href = link_el.get("href")
            if title and href:
                listings.append({
                    "source": "Lawctopus", "title": title,
                    "company": "See listing", "location": "See listing", "url": href,
                })
        except Exception as e:
            print(f"  [warn] parse error on Lawctopus item: {e}", file=sys.stderr)
            continue
    if not listings:
        report_issue("Lawctopus", "both RSS and HTML fallback returned 0 — needs a look")
    print(f"  -> {len(listings)} listings (HTML fallback)")
    return listings


def fetch_lawfoyer():
    """LawFoyer opportunities via RSS, HTML fallback."""
    print("Fetching LawFoyer...")
    listings = fetch_wp_rss("LawFoyer", "https://lawfoyer.in/category/opportunities/feed/")
    if listings:
        return listings

    resp = safe_get("https://lawfoyer.in/category/opportunities/")
    if not resp:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    listings = []
    articles = soup.select("article, div.post")[:MAX_LISTINGS_PER_SOURCE]
    for art in articles:
        try:
            link_el = art.select_one("h2 a, h3 a, a.entry-title-link")
            if not link_el:
                continue
            title = link_el.get_text(strip=True)
            href = link_el.get("href")
            if title and href:
                listings.append({
                    "source": "LawFoyer", "title": title,
                    "company": "See listing", "location": "See listing", "url": href,
                })
        except Exception as e:
            print(f"  [warn] parse error on LawFoyer item: {e}", file=sys.stderr)
            continue
    if not listings:
        report_issue("LawFoyer", "both RSS and HTML fallback returned 0 — needs a look")
    print(f"  -> {len(listings)} listings (HTML fallback)")
    return listings


def fetch_bar_and_bench_jobs():
    """Bar & Bench's jobs/opportunities feed. High-signal, often posts
    boutique/corporate firm openings before they hit the aggregator sites —
    a genuine 'hidden gem' source most manual job searches skip because
    people read Bar & Bench for news, not its jobs tag."""
    print("Fetching Bar and Bench (jobs/internships)...")
    return fetch_wp_rss(
        "Bar and Bench",
        "https://www.barandbench.com/feed",
        keyword_filter=["intern", "associate", "hiring", "vacancy", "recruit", "job", "opportunit"],
    )


def fetch_livelaw_jobs():
    """LiveLaw's feed, filtered to job-relevant titles. Same rationale as
    Bar & Bench — mostly a news site, so most people never think to check
    it for postings, meaning less competition on anything found here."""
    print("Fetching LiveLaw (jobs/internships)...")
    return fetch_wp_rss(
        "LiveLaw",
        "https://www.livelaw.in/feed",
        keyword_filter=["intern", "associate", "hiring", "vacancy", "recruit", "job", "opportunit"],
    )


def fetch_scc_blog_jobs():
    """SCC Online Blog feed, filtered to job-relevant titles."""
    print("Fetching SCC Online Blog (jobs/internships)...")
    return fetch_wp_rss(
        "SCC Online Blog",
        "https://www.scconline.com/blog/feed/",
        keyword_filter=["intern", "associate", "hiring", "vacancy", "recruit", "job", "opportunit"],
    )


def fetch_manupatra():
    """Manupatra Academy internship portal.

    NOTE: this page renders its listing table client-side via JavaScript
    after an AJAX call, and the full table + direct apply links are gated
    behind a login. A plain HTML fetch (what this function does) will
    usually see the page shell (filters, empty table) but not the actual
    listing data. This function tries anyway and logs clearly when it comes
    back empty, rather than failing silently.

    If Manupatra exposes a public JSON endpoint for the listing AJAX call
    in future (visible via browser dev tools -> Network tab while the page
    loads), swap the URL below to hit that endpoint directly for real data.
    """
    print("Fetching Manupatra Academy...")
    url = "https://www.manupatracademy.com/internships/law-student-opportunities"
    resp = safe_get(url)
    if not resp:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    listings = []

    # Best-effort: look for any listing-like rows/cards if the markup ever
    # includes server-rendered data. Falls back to empty if JS-rendered.
    candidates = soup.select("div.internship-card, tr.internship-row, div.op-card")[:MAX_LISTINGS_PER_SOURCE]

    for card in candidates:
        try:
            title_el = card.select_one("a, h3, h4")
            title = title_el.get_text(strip=True) if title_el else None
            href = title_el.get("href") if title_el and title_el.name == "a" else None
            if title and href:
                full_url = href if href.startswith("http") else f"https://www.manupatracademy.com{href}"
                listings.append({
                    "source": "Manupatra Academy",
                    "title": title,
                    "company": "See listing",
                    "location": "See listing",
                    "url": full_url,
                })
        except Exception as e:
            print(f"  [warn] parse error on Manupatra card: {e}", file=sys.stderr)
            continue

    if not listings:
        # Known, documented limitation (JS-rendered + login-gated) — not a
        # regression, so this is a plain log line, not a RUN_ISSUES entry
        # that would make the digest flag it as something newly broken.
        print("  -> 0 listings (page is JavaScript-rendered; see docstring for a fix path). "
              "Check https://www.manupatracademy.com/internships/law-student-opportunities manually for now.")
    else:
        print(f"  -> {len(listings)} listings")

    return listings


def fetch_career_pages():
    """User-defined firm career pages. Best-effort generic link scrape —
    these pages vary wildly in structure, so this looks for any link whose
    text contains job-relevant keywords.

    This is the highest-signal source for "hidden" opportunities: postings
    that go straight on a firm's own site and never get aggregated anywhere
    else. It's only as good as career_pages.json, though — an empty or
    placeholder list here silently contributes zero listings.
    """
    pages = load_json(CAREER_PAGES_FILE, [])
    # Filter out the shipped placeholder so it doesn't burn a fetch + log
    # noise every single day if the user hasn't replaced it yet.
    pages = [p for p in pages if "example-lawfirm.com" not in p.get("url", "")]
    if not pages:
        report_issue(
            "Firm career pages",
            "career_pages.json is empty or still the placeholder — this is your "
            "highest-signal source for hidden postings and it's contributing 0 "
            "listings. Add real firm URLs.",
        )
        return []

    print(f"Fetching {len(pages)} firm career pages...")
    # Strong signal (actual job-posting phrasing) vs weak signal (generic nav
    # words that also match practice-area pages, "About" blurbs, etc). Weak
    # keywords alone don't count as a match — must co-occur with something
    # role-shaped, or the link text must be long enough to look like a real
    # posting title rather than a one-word nav item.
    strong_keywords = [
        "intern", "associate", "trainee", "vacanc", "opening", "hiring",
        "we're hiring", "join our team", "job opening", "career opportunit",
        "paralegal", "legal counsel", "legal officer",
    ]
    weak_keywords = ["legal", "corporate", "counsel"]
    from urllib.parse import urljoin

    listings = []
    for page in pages:
        name = page.get("name", page["url"])
        resp = safe_get(page["url"])
        if not resp:
            report_issue(f"Firm: {name}", "career page fetch failed after retries")
            continue
        soup = BeautifulSoup(resp.text, "html.parser")
        links = soup.find_all("a", href=True)
        found = 0
        for a in links:
            text = a.get_text(strip=True)
            if not text or len(text) < 5:
                continue
            text_lower = text.lower()
            is_strong = any(k in text_lower for k in strong_keywords)
            is_weak_but_titlelike = any(k in text_lower for k in weak_keywords) and len(text) > 25
            if not (is_strong or is_weak_but_titlelike):
                continue
            href = a["href"]
            if href.startswith("/"):
                href = urljoin(page["url"], href)
            if href.startswith("http"):
                listings.append({
                    "source": f"Firm: {name}",
                    "title": text,
                    "company": name,
                    "location": "Not specified",
                    "url": href,
                })
                found += 1
            if found >= 8:  # cap per firm to avoid noise; keep scanning other firms
                break
        time.sleep(0.5)  # be polite

    print(f"  -> {len(listings)} listings across firm pages")
    return listings


# ---------------------------------------------------------------------------
# Claude filtering
# ---------------------------------------------------------------------------

def load_profile():
    default_profile = {
        "background": (
            "Newly enrolled advocate, UP Bar Council. BA LLB (Hons.), University "
            "of Lucknow, 2020-2025. ~1 year PQE. Currently practicing litigation "
            "at District & Sessions Court, Lucknow, while transitioning to "
            "corporate law."
        ),
        "target_areas": [
            "M&A", "IBC / NCLT / insolvency", "SEBI compliance",
            "commercial contract drafting", "corporate/company law", "TMT/legaltech"
        ],
        "preferences": [
            "Remote or hybrid preferred (based in Lucknow)",
            "Open to Tier-1/Tier-2 Delhi-NCR firms",
            "Zero-to-one PQE level — entry associate or legal intern roles suited to recent qualification",
            "Not interested in pure litigation-only roles",
            "Not interested in jurisdictions outside India (e.g. immigration law, foreign qualification required)",
        ],
    }
    return load_json(PROFILE_FILE, default_profile)


def score_listings_with_claude(listings, profile):
    """Send listings to Claude in batches, get back fitment scores + reasoning."""
    if not listings:
        return []
    if not ANTHROPIC_API_KEY:
        print("  [warn] ANTHROPIC_API_KEY not set — skipping scoring, returning all listings unscored")
        for l in listings:
            l["fit_score"] = None
            l["fit_reason"] = "Not scored (no API key)"
        return listings

    batch_size = 15
    scored = []

    for i in range(0, len(listings), batch_size):
        batch = listings[i:i + batch_size]
        batch_input = [
            {"id": idx, "title": l["title"], "company": l["company"], "source": l["source"]}
            for idx, l in enumerate(batch)
        ]

        system_prompt = f"""You are a legal recruiting analyst. Score each job/internship listing
for fit against this candidate profile, on a 0-10 scale (10 = excellent fit).

CANDIDATE PROFILE:
Background: {profile['background']}
Target practice areas: {', '.join(profile['target_areas'])}
Preferences: {'; '.join(profile['preferences'])}

Score generously for anything corporate/transactional/contract-related even if
titles are vague (e.g. "Legal Intern" at a startup could be relevant). Score
low (0-3) for pure litigation, criminal law, family law, or roles requiring
qualifications the candidate doesn't have (foreign bar, 5+ PQE, etc).

Return ONLY a JSON array, no preamble, no markdown fences:
[{{"id": 0, "fit_score": 7, "fit_reason": "one sentence why"}}, ...]"""

        batch_scored = False
        last_error = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = requests.post(
                    ANTHROPIC_URL,
                    headers={
                        "x-api-key": ANTHROPIC_API_KEY,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": ANTHROPIC_MODEL,
                        "max_tokens": 2000,
                        "system": system_prompt,
                        "messages": [
                            {"role": "user", "content": json.dumps(batch_input, ensure_ascii=False)}
                        ],
                    },
                    timeout=60,
                )
                resp.raise_for_status()
                data = resp.json()
                text = "".join(
                    block.get("text", "") for block in data.get("content", [])
                    if block.get("type") == "text"
                )
                text = re.sub(r"^```json|```$", "", text.strip(), flags=re.MULTILINE).strip()
                results = json.loads(text)

                score_map = {r["id"]: r for r in results}
                for idx, l in enumerate(batch):
                    r = score_map.get(idx, {})
                    l["fit_score"] = r.get("fit_score", 0)
                    l["fit_reason"] = r.get("fit_reason", "No reason returned")
                    scored.append(l)
                batch_scored = True
                break

            except Exception as e:
                last_error = e
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF_SECONDS * attempt)

        if not batch_scored:
            report_issue("Claude scoring", f"batch {i} failed after {MAX_RETRIES} attempts: {last_error}")
            for l in batch:
                # Sentinel well outside the normal 0-10 range (not None, not
                # 0) so a scoring failure surfaces at the TOP of the digest
                # instead of getting silently buried in the low-relevance
                # collapsed section — a real 9/10 match that hit a transient
                # API error deserves your eyes, not burial.
                l["fit_score"] = None
                l["fit_reason"] = f"⚠ Not scored — API error, needs manual review: {last_error}"
                scored.append(l)

        time.sleep(1)  # gentle rate limiting

    return scored


# ---------------------------------------------------------------------------
# Digest generation + delivery
# ---------------------------------------------------------------------------

def build_digest_markdown(new_listings, min_score=5):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # fit_score is None only when scoring genuinely errored (see
    # score_listings_with_claude) — that's distinct from a real low score of
    # 0-4, and needs manual eyes rather than silent burial in "lower
    # relevance", since it might well be an excellent match.
    unscored = [l for l in new_listings if l.get("fit_score") is None]
    scored_listings = [l for l in new_listings if l.get("fit_score") is not None]
    relevant = [l for l in scored_listings if l["fit_score"] >= min_score]
    relevant.sort(key=lambda l: l["fit_score"], reverse=True)
    low_fit = [l for l in scored_listings if l["fit_score"] < min_score]

    lines = [f"# Legal Job Digest — {today}", ""]

    if RUN_ISSUES:
        lines.append(f"<details><summary>⚠ {len(RUN_ISSUES)} issue(s) this run — click to expand</summary>\n")
        for source, msg in RUN_ISSUES:
            lines.append(f"- **{source}:** {msg}")
        lines.append("\n</details>\n")

    if unscored:
        lines.append(f"## ⚠ {len(unscored)} listing(s) need manual review (scoring failed)\n")
        for l in unscored:
            lines.append(f"### [{l.get('title', 'Untitled')}]({l.get('url', '#')})")
            lines.append(f"- **Company:** {l.get('company', 'Unknown')}")
            lines.append(f"- **Source:** {l.get('source', 'Unknown')}")
            lines.append(f"- **Note:** {l.get('fit_reason', '')}")
            lines.append("")

    if not relevant:
        lines.append("No new listings scored above the relevance threshold today.")
    else:
        lines.append(f"**{len(relevant)} relevant new listing(s) found.**\n")
        for l in relevant:
            lines.append(f"### [{l.get('title', 'Untitled')}]({l.get('url', '#')})")
            lines.append(f"- **Company:** {l.get('company', 'Unknown')}")
            lines.append(f"- **Source:** {l.get('source', 'Unknown')}")
            lines.append(f"- **Fit score:** {l['fit_score']}/10")
            lines.append(f"- **Why:** {l.get('fit_reason', '')}")
            lines.append("")

    if low_fit:
        lines.append(f"\n<details><summary>{len(low_fit)} lower-relevance listings (click to expand)</summary>\n")
        for l in low_fit:
            lines.append(f"- [{l.get('title', 'Untitled')}]({l.get('url', '#')}) — {l.get('company', 'Unknown')} (score: {l['fit_score']})")
        lines.append("\n</details>")

    return "\n".join(lines)


def send_email_digest(markdown_content, subject):
    if not (RESEND_API_KEY and DIGEST_TO_EMAIL):
        print("  [info] Resend not configured (RESEND_API_KEY / DIGEST_TO_EMAIL missing) — skipping email")
        return False

    # Basic markdown -> HTML (good enough for a digest email)
    html = markdown_content
    html = re.sub(r"^# (.+)$", r"<h1>\1</h1>", html, flags=re.MULTILINE)
    html = re.sub(r"^### (.+)$", r"<h3>\1</h3>", html, flags=re.MULTILINE)
    html = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', html)
    html = re.sub(r"^- (.+)$", r"<li>\1</li>", html, flags=re.MULTILINE)
    html = html.replace("\n\n", "<br><br>")

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": DIGEST_FROM_EMAIL,
                "to": [DIGEST_TO_EMAIL],
                "subject": subject,
                "html": html,
            },
            timeout=20,
        )
        resp.raise_for_status()
        print("  [info] Email sent successfully")
        return True
    except requests.RequestException as e:
        print(f"  [warn] Email send failed: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def prune_seen_listings(seen):
    """Drop entries older than SEEN_LISTING_TTL_DAYS so this file doesn't
    grow forever. A listing not seen again within that window is either
    long expired or was a one-off; either way there's no cost to letting it
    resurface and get re-deduped fresh if it somehow reappears."""
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=SEEN_LISTING_TTL_DAYS)
    kept = {}
    for lid, meta in seen.items():
        try:
            first_seen = datetime.fromisoformat(meta["first_seen"])
        except (KeyError, ValueError, TypeError):
            kept[lid] = meta  # malformed entry — keep rather than risk data loss
            continue
        if first_seen >= cutoff:
            kept[lid] = meta
    pruned_count = len(seen) - len(kept)
    if pruned_count:
        print(f"Pruned {pruned_count} seen-listing entries older than {SEEN_LISTING_TTL_DAYS} days")
    return kept


def run_fetcher_safely(fetch_fn, *args, label=None):
    """Run a single source fetcher in isolation. If a fetcher raises
    (rather than cleanly returning []), the whole run used to crash and
    NOTHING got written — no digest, no email, no seen-listings update, and
    no visibility into which source did it. Now one bad source just gets
    logged as an issue and every other source still runs."""
    name = label or getattr(fetch_fn, "__name__", "unknown source")
    try:
        return fetch_fn(*args)
    except Exception as e:
        report_issue(name, f"fetcher crashed: {e}")
        return []


def main():
    print(f"=== Legal Job Monitor run at {datetime.now(timezone.utc).isoformat()} ===\n")
    RUN_ISSUES.clear()

    all_listings = []

    # Internshala: sweep every keyword slug, not just "legal"/"corporate-law",
    # so postings filed under adjacent categories (compliance, paralegal,
    # company-secretary, etc.) aren't missed. Cross-keyword duplicates are
    # free — they collapse at the seen-listings de-dupe step below.
    for kw in INTERNSHALA_KEYWORDS:
        all_listings += run_fetcher_safely(fetch_internshala, kw, label=f"Internshala/{kw}")

    all_listings += run_fetcher_safely(fetch_lawctopus, label="Lawctopus")
    all_listings += run_fetcher_safely(fetch_lawfoyer, label="LawFoyer")
    all_listings += run_fetcher_safely(fetch_manupatra, label="Manupatra Academy")

    # "Hidden gem" sources — news/journal sites most people never think to
    # check for postings, so listings found here face far less competition.
    all_listings += run_fetcher_safely(fetch_bar_and_bench_jobs, label="Bar and Bench")
    all_listings += run_fetcher_safely(fetch_livelaw_jobs, label="LiveLaw")
    all_listings += run_fetcher_safely(fetch_scc_blog_jobs, label="SCC Online Blog")

    # Highest-signal source: direct firm postings that never reach any
    # aggregator at all.
    all_listings += run_fetcher_safely(fetch_career_pages, label="Firm career pages")

    print(f"\nTotal listings fetched: {len(all_listings)}")

    seen = load_json(SEEN_FILE, {})
    seen = prune_seen_listings(seen)

    new_listings = []
    for l in all_listings:
        lid = listing_id(l["url"], l["title"])
        if lid not in seen:
            l["_id"] = lid
            new_listings.append(l)
            seen[lid] = {"first_seen": datetime.now(timezone.utc).isoformat(), "title": l["title"]}

    print(f"New (unseen) listings: {len(new_listings)}")

    profile = load_profile()
    scored = score_listings_with_claude(new_listings, profile)

    digest_md = build_digest_markdown(scored)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    digest_path = OUTPUT_DIR / "digest.md"
    digest_path.write_text(digest_md, encoding="utf-8")
    print(f"\nDigest written to {digest_path}")

    # Keep a dated archive too
    archive_path = OUTPUT_DIR / f"digest-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.md"
    archive_path.write_text(digest_md, encoding="utf-8")

    relevant_count = len([l for l in scored if l.get("fit_score") is not None and l["fit_score"] >= 5])
    unscored_count = len([l for l in scored if l.get("fit_score") is None])
    subject_bits = f"{relevant_count} relevant"
    if unscored_count:
        subject_bits += f", {unscored_count} need review"
    if RUN_ISSUES:
        subject_bits += f" — ⚠ {len(RUN_ISSUES)} issue(s)"
    subject = f"Legal Job Digest — {subject_bits} — {datetime.now(timezone.utc).strftime('%b %d')}"
    send_email_digest(digest_md, subject)

    # Always save seen-listings, even on a bad run, so a source that briefly
    # returned garbage doesn't get permanently re-flagged as "new" every day.
    save_json(SEEN_FILE, seen)

    if RUN_ISSUES:
        print(f"\n{len(RUN_ISSUES)} issue(s) this run:")
        for source, msg in RUN_ISSUES:
            print(f"  - {source}: {msg}")

    print("\nDone.")


if __name__ == "__main__":
    main()
