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
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
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

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
# Gmail SMTP delivery (optional — digest still written to output/digest.md if unset)
GMAIL_SENDER      = os.environ.get("GMAIL_SENDER")       # your Gmail address
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD") # 16-char Google App Password
DIGEST_TO_EMAIL   = os.environ.get("DIGEST_TO_EMAIL")    # recipient address

# OpenRouter-hosted model slug. "anthropic/claude-sonnet-5" calls the same
# model this script previously hit directly via the Anthropic API. Override
# via the OPENROUTER_MODEL secret/env var if you want to point at a cheaper
# model (e.g. "anthropic/claude-haiku-4.5") for this low-stakes scoring task,
# or at a non-Anthropic model.
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL") or "anthropic/claude-sonnet-5"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

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

# Internshala keyword slugs — kept to clearly law-specific categories only.
# 'compliance' and 'contract' were removed: they pull in HR compliance,
# financial compliance, contract staffing, logistics, and operations roles
# that have nothing to do with legal practice.
INTERNSHALA_KEYWORDS = [
    "legal", "corporate-law", "legal-research", "law",
    "company-secretary", "paralegal", "intellectual-property-law",
]

# Title-level whitelist: at least one of these must appear in the
# listing title (case-insensitive) for it to pass the law filter.
_LAW_TITLE_INDICATORS = [
    "legal", "law", "advocate", "attorney", "counsel", "solicitor",
    "paralegal", "litigation", "corporate", "contract draft", "legal draft",
    "legal research", "legal intern", "law intern", "llb", "llm",
    "company secretary", " cs ", "intellectual property", " ip ", "patent",
    "trademark", "copyright", "arbitration", "due diligence", "m&a",
    "merger", "acquisition", "compliance officer", "legal compliance",
    "regulatory", "sebi", "ibc", "insolvency", "nclt", "legal associate",
    "legal executive", "legal manager", "legal head",
]

# Title-level blacklist: if the title contains ANY of these and NONE of
# the law indicators, it is rejected outright.
_NON_LAW_TITLE_REJECTS = [
    # Accounting / finance
    "accountant", "accounting", "chartered accountant", "ca articleship",
    "articleship", "article assistant", "finance intern", "financial analyst",
    "fintech", "fin-tech", "banking intern", "investment banking",
    "equity research", "mutual fund", "stock market", "taxation intern",
    "gst intern", "audit intern", "tally",
    # Non-law white-collar
    "telecall", "telecaller", "bpo", "customer support", "customer service",
    "human resource", " hr ", "hr intern", "recruitment intern", "talent acqui",
    "payroll",
    # Ops / supply chain
    "logistics", "supply chain", "operations intern", "warehouse", "procurement",
    "inventory",
    # Marketing / media
    "marketing intern", "digital marketing", "social media", "seo intern",
    "graphic design", "ui/ux", "ux design", "content writ", "copywrite",
    "video edit", "brand",
    # Sales
    "sales intern", "business development", "e-commerce",
    # Engineering / tech
    "civil engineer", "mechanical", "electrical", "software develop",
    "python intern", "java intern", "web develop", "android",
    "machine learning", "data science", "data analyst", "cloud",
    "devops", "cybersecurity intern",
]


def _is_law_listing(title: str) -> bool:
    """Return True only if the listing title looks like a genuine law/legal role.

    Strategy: whitelist beats blacklist. A title passes if it contains any
    law indicator. A title fails if it contains a non-law reject term AND
    no law indicator. Titles that are completely neutral (neither list)
    are passed through so Claude can score them — better to let a borderline
    listing through than to silently drop a real opportunity.
    """
    t = title.lower()
    has_law   = any(ind in t for ind in _LAW_TITLE_INDICATORS)
    has_noise = any(rej in t for rej in _NON_LAW_TITLE_REJECTS)
    if has_law:
        return True   # clear law role — keep
    if has_noise:
        return False  # clearly not law — drop
    return True       # ambiguous — pass through for Claude to score


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
    """Internshala public search results (no login required).

    Each result is passed through _is_law_listing() to drop clearly
    non-legal roles (accounting, HR, telecalling, logistics, etc.) that
    bleed into adjacent keyword categories.
    """
    url = f"https://internshala.com/internships/{keyword}-internship"
    resp = safe_get(url)
    if not resp:
        report_issue(f"Internshala/{keyword}", "fetch failed after retries")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    listings = []
    rejected = 0
    cards = soup.select("div.individual_internship, div[id^='job_'], div.internship_meta")[:MAX_LISTINGS_PER_SOURCE]

    for card in cards:
        try:
            parsed = _parse_internshala_card(card)
            if not parsed:
                continue
            # Drop non-law listings before they ever reach the seen-listings
            # store or the Claude scoring step.
            if not _is_law_listing(parsed["title"]):
                rejected += 1
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

    if rejected:
        print(f"  [filter] dropped {rejected} non-law listing(s) from Internshala/{keyword}")
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
    """Lawctopus jobs/internships via RSS (falls back to HTML scrape of the
    current /jobs/ and /internships/ pages — the old /lawctopus-law-jobs/
    path is a 404 as of 2025)."""
    print("Fetching Lawctopus...")
    # Primary: site-wide RSS feed (covers both jobs and internships)
    listings = fetch_wp_rss("Lawctopus", "https://www.lawctopus.com/feed/",
                            keyword_filter=["intern", "associate", "hiring", "vacancy",
                                            "recruit", "job", "opportunit", "legal"])
    if listings:
        return listings

    # Fallback: scrape /jobs/ and /internships/ listing pages directly
    combined = []
    for fallback_url in [
        "https://www.lawctopus.com/jobs/",
        "https://www.lawctopus.com/internships/",
    ]:
        resp = safe_get(fallback_url)
        if not resp:
            continue
        soup = BeautifulSoup(resp.text, "html.parser")
        articles = soup.select("article, div.post")[:MAX_LISTINGS_PER_SOURCE]
        for art in articles:
            try:
                link_el = art.select_one("h2 a, h3 a, a.entry-title-link")
                if not link_el:
                    continue
                title = link_el.get_text(strip=True)
                href = link_el.get("href")
                if title and href:
                    combined.append({
                        "source": "Lawctopus", "title": title,
                        "company": "See listing", "location": "See listing", "url": href,
                    })
            except Exception as e:
                print(f"  [warn] parse error on Lawctopus item: {e}", file=sys.stderr)
                continue

    if not combined:
        report_issue("Lawctopus", "both RSS and HTML fallback returned 0 — needs a look")
    print(f"  -> {len(combined)} listings (HTML fallback)")
    return combined


def fetch_lawfoyer():
    """LawFoyer opportunities via WordPress REST API.

    LawFoyer's category pages are JavaScript-rendered — BeautifulSoup sees
    only the page shell, no post cards. Their WP REST API (confirmed working)
    returns clean JSON without needing a browser.

    Category 455 = 'internship' (the only job-type category confirmed in
    their WP taxonomy). We also do a keyword search for 'job' to catch
    posts tagged under other categories.
    """
    print("Fetching LawFoyer...")
    base = "https://lawfoyer.in/wp-json/wp/v2"
    fields = "_fields=title,link,date"
    job_keywords = ["intern", "job", "vacancy", "hiring", "recruit", "opportunit",
                    "associate", "counsel", "advocate"]
    listings = []
    seen_urls: set = set()

    endpoints = [
        f"{base}/posts?categories=455&per_page=20&{fields}",          # internship category
        f"{base}/posts?search=job+vacancy+legal&per_page=20&{fields}", # keyword search
        f"{base}/posts?search=internship+law+firm&per_page=20&{fields}",
    ]

    for url in endpoints:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            posts = resp.json()
            if not isinstance(posts, list):
                continue
            for post in posts:
                title = post.get("title", {}).get("rendered", "").strip()
                link  = post.get("link", "").strip()
                if not (title and link) or link in seen_urls:
                    continue
                # Filter to job-relevant posts
                if not any(k in title.lower() for k in job_keywords):
                    continue
                seen_urls.add(link)
                listings.append({
                    "source": "LawFoyer",
                    "title": title,
                    "company": "See listing",
                    "location": "See listing",
                    "url": link,
                })
        except Exception as e:
            print(f"  [warn] LawFoyer API error ({url}): {e}", file=sys.stderr)
            continue

    if not listings:
        report_issue("LawFoyer", "WP REST API returned 0 job-relevant posts")
    print(f"  -> {len(listings)} listings")
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
    """LiveLaw job-relevant posts via Google News RSS.

    LiveLaw uses a JS-rendered custom frontend — standard HTML scraping sees
    only an empty shell, and their WP REST API is not exposed. Google News
    RSS is a stable, zero-dependency alternative: Google indexes LiveLaw
    continuously and exposes its own RSS of matching articles. No scraping
    needed — we just parse the RSS Google serves.
    """
    print("Fetching LiveLaw (via Google News RSS)...")
    import urllib.parse
    query = urllib.parse.quote('site:livelaw.in (intern OR vacancy OR "job opening" OR hiring OR associate OR recruit)')
    feed_url = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
    job_keywords = ["intern", "associate", "hiring", "vacancy", "recruit", "job", "opportunit"]
    return fetch_wp_rss("LiveLaw", feed_url, keyword_filter=job_keywords)


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
    if not OPENROUTER_API_KEY:
        print("  [warn] OPENROUTER_API_KEY not set — skipping scoring, returning all listings unscored")
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
                    OPENROUTER_URL,
                    headers={
                        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                        "Content-Type": "application/json",
                        # Optional — OpenRouter uses these for its public
                        # rankings/attribution. Harmless to leave in, safe to
                        # delete if you'd rather not send them.
                        "HTTP-Referer": "https://github.com/abhyudaysingh00-cmd/Legal-job-monitor",
                        "X-Title": "Legal Job Monitor",
                    },
                    json={
                        "model": OPENROUTER_MODEL,
                        "max_tokens": 2000,
                        # OpenRouter speaks the OpenAI chat-completions shape,
                        # not the Anthropic Messages shape — the system
                        # prompt is a message in the array, not a top-level
                        # "system" field.
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": json.dumps(batch_input, ensure_ascii=False)},
                        ],
                    },
                    timeout=60,
                )
                resp.raise_for_status()
                data = resp.json()
                # OpenAI-style response: plain string at choices[0].message.content
                # (Anthropic's Messages API instead returns a list of content
                # blocks under "content", which is what the old code parsed.)
                text = data["choices"][0]["message"]["content"]
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


def _score_badge(score):
    """Return an inline-styled score badge for use in HTML email."""
    if score is None:
        return '<span style="display:inline-block;padding:2px 8px;border-radius:12px;font-size:12px;font-weight:700;background:#f59e0b;color:#fff;">? / 10</span>'
    if score >= 7:
        colour = "#16a34a"  # green
    elif score >= 5:
        colour = "#d97706"  # amber
    else:
        colour = "#6b7280"  # grey
    return (
        f'<span style="display:inline-block;padding:2px 8px;border-radius:12px;'
        f'font-size:12px;font-weight:700;background:{colour};color:#fff;">'
        f'{score} / 10</span>'
    )


def build_email_html(scored_listings, today=None):
    """Build a self-contained, inline-CSS HTML email from the scored listings.

    Designed to render correctly in Gmail and Outlook (no external CSS,
    no <style> blocks that Gmail strips, all styling inline).
    """
    if today is None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    unscored   = [l for l in scored_listings if l.get("fit_score") is None]
    scored     = [l for l in scored_listings if l.get("fit_score") is not None]
    relevant   = sorted([l for l in scored if l["fit_score"] >= 5],
                        key=lambda l: l["fit_score"], reverse=True)
    low_fit    = [l for l in scored if l["fit_score"] < 5]

    # ── Shared inline styles ──────────────────────────────────────────────
    card_style = (
        "margin:0 0 16px 0;padding:16px 20px;border-radius:8px;"
        "border-left:4px solid #4f46e5;background:#f8f7ff;"
        "font-family:'Segoe UI',Arial,sans-serif;"
    )
    card_style_warn = card_style.replace("#4f46e5", "#f59e0b").replace("#f8f7ff", "#fffbeb")
    card_style_low  = card_style.replace("#4f46e5", "#9ca3af").replace("#f8f7ff", "#f9fafb")

    label_style = (
        "font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;"
        "color:#6b7280;margin:0 0 2px 0;"
    )
    value_style = "font-size:14px;color:#374151;margin:0 0 8px 0;"
    link_style  = (
        "display:inline-block;margin-top:8px;padding:7px 16px;border-radius:6px;"
        "background:#4f46e5;color:#fff !important;text-decoration:none;"
        "font-size:13px;font-weight:600;"
    )

    def card(listing, style=card_style):
        title   = listing.get("title", "Untitled")
        url     = listing.get("url", "#")
        company = listing.get("company", "Unknown")
        source  = listing.get("source", "Unknown")
        reason  = listing.get("fit_reason", "")
        score   = listing.get("fit_score")
        badge   = _score_badge(score)
        return f"""
<div style="{style}">
  <p style="font-size:16px;font-weight:700;color:#1e1b4b;margin:0 0 8px 0;">{title}</p>
  <p style="{label_style}">Company</p>
  <p style="{value_style}">{company}</p>
  <p style="{label_style}">Source</p>
  <p style="{value_style}">{source}</p>
  <p style="{label_style}">Fit score &nbsp;{badge}</p>
  <p style="{value_style}">{reason}</p>
  <a href="{url}" style="{link_style}">View listing →</a>
</div>"""

    # ── Build HTML sections ───────────────────────────────────────────────
    issues_html = ""
    if RUN_ISSUES:
        rows = "".join(
            f'<li style="margin-bottom:4px;"><strong>{s}:</strong> {m}</li>'
            for s, m in RUN_ISSUES
        )
        issues_html = f"""
<div style="margin:0 0 24px 0;padding:14px 18px;border-radius:8px;
            background:#fffbeb;border:1px solid #fcd34d;
            font-family:'Segoe UI',Arial,sans-serif;">
  <p style="font-size:14px;font-weight:700;color:#92400e;margin:0 0 8px 0;">
    ⚠ {len(RUN_ISSUES)} issue(s) this run
  </p>
  <ul style="margin:0;padding-left:18px;font-size:13px;color:#78350f;">{rows}</ul>
</div>"""

    unscored_html = ""
    if unscored:
        cards = "".join(card(l, card_style_warn) for l in unscored)
        unscored_html = f"""
<h2 style="font-family:'Segoe UI',Arial,sans-serif;font-size:16px;
           color:#92400e;margin:24px 0 12px 0;">
  ⚠ {len(unscored)} listing(s) — needs manual review (scoring failed)
</h2>
{cards}"""

    if not relevant:
        relevant_html = """
<p style="font-family:'Segoe UI',Arial,sans-serif;font-size:14px;color:#6b7280;
          text-align:center;padding:32px 0;">
  No new listings scored above the relevance threshold today.
</p>"""
    else:
        cards = "".join(card(l) for l in relevant)
        relevant_html = f"""
<h2 style="font-family:'Segoe UI',Arial,sans-serif;font-size:16px;
           color:#1e1b4b;margin:0 0 16px 0;">
  ✅ {len(relevant)} relevant new listing(s)
</h2>
{cards}"""

    low_fit_html = ""
    if low_fit:
        rows = "".join(
            f'<li style="margin-bottom:6px;">'
            f'<a href="{l.get("url","#")}" style="color:#4f46e5;font-weight:600;">'
            f'{l.get("title","Untitled")}</a>'
            f' — {l.get("company","Unknown")} &nbsp;{_score_badge(l["fit_score"])}'
            f'</li>'
            for l in low_fit
        )
        low_fit_html = f"""
<details style="margin-top:24px;">
  <summary style="font-family:'Segoe UI',Arial,sans-serif;font-size:14px;
                  font-weight:600;color:#6b7280;cursor:pointer;">
    {len(low_fit)} lower-relevance listings (click to expand)
  </summary>
  <ul style="margin:12px 0 0 0;padding-left:18px;
             font-family:'Segoe UI',Arial,sans-serif;font-size:13px;">
    {rows}
  </ul>
</details>"""

    # ── Assemble full email ───────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f3f4f6;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;padding:32px 0;">
  <tr><td align="center">
    <table width="620" cellpadding="0" cellspacing="0"
           style="max-width:620px;width:100%;border-radius:12px;
                  overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08);">

      <!-- Header -->
      <tr>
        <td style="background:linear-gradient(135deg,#4f46e5 0%,#7c3aed 100%);
                   padding:32px 32px 24px 32px;">
          <p style="margin:0;font-family:'Segoe UI',Arial,sans-serif;
                    font-size:22px;font-weight:700;color:#fff;">
            ⚖️ Legal Job Digest
          </p>
          <p style="margin:6px 0 0 0;font-family:'Segoe UI',Arial,sans-serif;
                    font-size:14px;color:#c7d2fe;">
            {today} &nbsp;·&nbsp; {len(relevant)} relevant &nbsp;·&nbsp;
            {len(low_fit)} lower-relevance &nbsp;·&nbsp; {len(unscored)} need review
          </p>
        </td>
      </tr>

      <!-- Body -->
      <tr>
        <td style="background:#fff;padding:28px 32px 32px 32px;">
          {issues_html}
          {unscored_html}
          {relevant_html}
          {low_fit_html}

          <hr style="border:none;border-top:1px solid #e5e7eb;margin:32px 0 20px 0;">
          <p style="font-family:'Segoe UI',Arial,sans-serif;font-size:12px;
                    color:#9ca3af;text-align:center;margin:0;">
            Sent by Legal Job Monitor &nbsp;·&nbsp;
            Scores generated by {OPENROUTER_MODEL} via OpenRouter
          </p>
        </td>
      </tr>

    </table>
  </td></tr>
</table>
</body>
</html>"""
    return html


def send_email_digest(scored_listings, subject, today=None):
    """Send the digest as a styled HTML email via Gmail SMTP.

    Requires three env vars / GitHub secrets:
      GMAIL_SENDER       — the Gmail address used to send (e.g. you@gmail.com)
      GMAIL_APP_PASSWORD — a 16-char Google App Password (NOT your login password).
                           Generate one at: Google Account → Security →
                           2-Step Verification → App passwords.
      DIGEST_TO_EMAIL    — recipient address (can be the same Gmail or any inbox).

    Args:
        scored_listings: list of listing dicts (already scored).
        subject: email subject line string.
        today: optional date string for the email header (defaults to today UTC).
    """
    if not (GMAIL_SENDER and GMAIL_APP_PASSWORD and DIGEST_TO_EMAIL):
        print(
            "  [info] Gmail not configured "
            "(GMAIL_SENDER / GMAIL_APP_PASSWORD / DIGEST_TO_EMAIL missing) — skipping email"
        )
        return False

    html_body = build_email_html(scored_listings, today=today)

    # Plain-text fallback for clients that can't render HTML
    unscored = [l for l in scored_listings if l.get("fit_score") is None]
    relevant = sorted(
        [l for l in scored_listings if l.get("fit_score") is not None and l["fit_score"] >= 5],
        key=lambda l: l["fit_score"], reverse=True,
    )
    text_lines = [subject, "=" * len(subject), ""]
    if unscored:
        text_lines.append(f"⚠ {len(unscored)} listing(s) need manual review (scoring failed)\n")
    if relevant:
        text_lines.append(f"✅ {len(relevant)} relevant new listing(s)\n")
        for l in relevant:
            text_lines.append(
                f"[{l['fit_score']}/10] {l.get('title','Untitled')} — {l.get('company','Unknown')}"
            )
            text_lines.append(f"  {l.get('url','')}\n")
    else:
        text_lines.append("No new listings scored above the relevance threshold today.")
    plain_body = "\n".join(text_lines)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_SENDER
    msg["To"]      = DIGEST_TO_EMAIL
    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body,  "html",  "utf-8"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(GMAIL_SENDER, GMAIL_APP_PASSWORD)
            smtp.sendmail(GMAIL_SENDER, DIGEST_TO_EMAIL, msg.as_string())
        print(f"  [info] Email sent successfully → {DIGEST_TO_EMAIL}")
        return True
    except smtplib.SMTPException as e:
        print(f"  [warn] Gmail SMTP send failed: {e}", file=sys.stderr)
        report_issue("Gmail SMTP", f"send failed: {e}")
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

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    relevant_count = len([l for l in scored if l.get("fit_score") is not None and l["fit_score"] >= 5])
    unscored_count = len([l for l in scored if l.get("fit_score") is None])
    subject_bits = f"{relevant_count} relevant"
    if unscored_count:
        subject_bits += f", {unscored_count} need review"
    if RUN_ISSUES:
        subject_bits += f" — ⚠ {len(RUN_ISSUES)} issue(s)"
    subject = f"Legal Job Digest — {subject_bits} — {datetime.now(timezone.utc).strftime('%b %d')}"
    send_email_digest(scored, subject, today=today_str)

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
