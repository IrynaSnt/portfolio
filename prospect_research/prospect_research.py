"""
Agentic Prospect Research Tool — Educational Travel Company
============================================================
Stage 1: Score and filter schools → top 20 high-priority prospects
Stage 2: Enrich each school with web intelligence (travel programs,
         competitor signals, staff contacts)
"""

import csv
import time
import re
import os
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse, quote_plus
from datetime import datetime

# ─────────────────────────────────────────────
#  EDITABLE CONFIGURATION
# ─────────────────────────────────────────────

TOP_N = 20          # how many schools to enrich
REQUEST_DELAY = 2   # seconds between HTTP requests

# Keywords that signal an active travel program
TRAVEL_KEYWORDS = [
    "educational tour",
    "student trip",
    "travel club",
    "international travel",
    "study abroad",
    "model un trip",
    "class trip",
    "spring trip",
    "cultural exchange",
    "overseas program",
    "language immersion",
    "global education",
]

# Competitor brand names to flag
COMPETITOR_KEYWORDS = [
    "EF Tours",
    "WorldStrides",
    "Rick Steves",
    "Explorica",
    "ACIS",
    "Grand European Travel",
    "Brightspark",
    "Rustic Pathways",
    "ProWorld",
    "National Geographic Student Expeditions",
    "People to People",
    "Putney Student Travel",
    "Where There Be Dragons",
    "ISE",
    "CIEE",
]

SEARCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

INPUT_CSV = "schools_dataset.csv"
OUTPUT_CSV = "enriched_prospects.csv"

# ─────────────────────────────────────────────
#  STAGE 1: SCORING
# ─────────────────────────────────────────────

SCORE_WEIGHTS = {
    "private_school": 20,
    "ib_program": 15,
    "high_ap_count": 10,       # ap_courses_count >= 20
    "language_program": 10,
    "travel_club": 15,
    "high_income": 12,         # median_household_income >= 100000
    "honors_program": 8,
    "arts_program": 5,
    "large_enrollment": 5,     # enrollment >= 1500 (more students = more budget)
}


def score_school(row: dict) -> int:
    score = 0
    if row.get("school_type", "").strip().lower() in ("private", "charter"):
        score += SCORE_WEIGHTS["private_school"]
    if row.get("international_baccalaureate", "").strip().lower() == "true":
        score += SCORE_WEIGHTS["ib_program"]
    try:
        if int(row.get("ap_courses_count", 0)) >= 20:
            score += SCORE_WEIGHTS["high_ap_count"]
    except ValueError:
        pass
    if row.get("has_language_program", "").strip().lower() == "true":
        score += SCORE_WEIGHTS["language_program"]
    if row.get("travel_club_mentioned", "").strip().lower() == "true":
        score += SCORE_WEIGHTS["travel_club"]
    try:
        if int(row.get("median_household_income", 0)) >= 100_000:
            score += SCORE_WEIGHTS["high_income"]
    except ValueError:
        pass
    if row.get("has_honors_program", "").strip().lower() == "true":
        score += SCORE_WEIGHTS["honors_program"]
    if row.get("has_arts_program", "").strip().lower() == "true":
        score += SCORE_WEIGHTS["arts_program"]
    try:
        if int(row.get("enrollment", 0)) >= 1500:
            score += SCORE_WEIGHTS["large_enrollment"]
    except ValueError:
        pass
    return score


def load_and_score(path: str) -> list[dict]:
    schools = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row["score"] = score_school(row)
            schools.append(row)
    schools.sort(key=lambda x: x["score"], reverse=True)
    return schools


# ─────────────────────────────────────────────
#  STAGE 2: WEB ENRICHMENT HELPERS
# ─────────────────────────────────────────────

def google_search_urls(query: str, num_results: int = 3) -> list[str]:
    """
    Pull top-N result URLs from a Google search via the HTML page.
    Falls back to an empty list on any error.
    """
    encoded = quote_plus(query)
    search_url = f"https://www.google.com/search?q={encoded}&num={num_results + 5}"
    urls = []
    try:
        resp = requests.get(search_url, headers=SEARCH_HEADERS, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.select("a[href]"):
            href = a["href"]
            # Google wraps real URLs like /url?q=https://...
            if href.startswith("/url?q="):
                real = href[7:].split("&")[0]
                parsed = urlparse(real)
                if parsed.scheme in ("http", "https") and "google" not in parsed.netloc:
                    urls.append(real)
                    if len(urls) >= num_results:
                        break
    except Exception as e:
        print(f"    [search error] {query!r}: {e}")
    return urls


def fetch_page_text(url: str) -> str:
    """Return visible text from a URL, or '' on failure."""
    try:
        resp = requests.get(url, headers=SEARCH_HEADERS, timeout=10, allow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        return " ".join(soup.get_text(" ", strip=True).split())
    except Exception as e:
        print(f"    [fetch error] {url}: {e}")
        return ""


def find_keywords(text: str, keyword_list: list[str]) -> list[str]:
    text_lower = text.lower()
    return [kw for kw in keyword_list if kw.lower() in text_lower]


def find_contact_name(text: str) -> str:
    """
    Heuristic: look for patterns like 'John Smith, Activities Director'
    or 'contact: Jane Doe' near trip/travel coordinator titles.
    """
    title_pattern = re.compile(
        r"([A-Z][a-z]+(?:\s[A-Z][a-z]+){1,2})"
        r"[,\s]+(?:activities director|trip coordinator|travel coordinator"
        r"|director of student activities|international programs|chaperone coordinator"
        r"|club advisor|faculty advisor|group leader)",
        re.IGNORECASE,
    )
    matches = title_pattern.findall(text)
    if matches:
        return matches[0].strip()

    # Looser pattern: "contact [name]" or "coordinator [name]"
    loose = re.compile(
        r"(?:contact|coordinator|director|advisor)[:\s]+([A-Z][a-z]+(?:\s[A-Z][a-z]+){1,2})",
        re.IGNORECASE,
    )
    m = loose.search(text)
    if m:
        return m.group(1).strip()

    return ""


def enrich_school(school: dict) -> dict:
    name = school["school_name"]
    print(f"\n  Enriching: {name}")

    queries = [
        f"{name} student travel",
        f"{name} international trip",
    ]

    all_urls: list[str] = []
    for q in queries:
        urls = google_search_urls(q, num_results=3)
        all_urls.extend(urls)
        time.sleep(REQUEST_DELAY)
    # Deduplicate while preserving order
    seen = set()
    unique_urls = []
    for u in all_urls:
        if u not in seen:
            seen.add(u)
            unique_urls.append(u)
    unique_urls = unique_urls[:6]  # cap total pages visited

    # Visit pages and scan text
    travel_signals_found: list[str] = []
    competitors_found: list[str] = []
    contact_name = ""
    visited_urls: list[str] = []

    for url in unique_urls:
        print(f"    Visiting: {url}")
        text = fetch_page_text(url)
        time.sleep(REQUEST_DELAY)
        if not text:
            continue
        visited_urls.append(url)

        travel_signals_found.extend(find_keywords(text, TRAVEL_KEYWORDS))
        competitors_found.extend(find_keywords(text, COMPETITOR_KEYWORDS))
        if not contact_name:
            contact_name = find_contact_name(text)

    # Deduplicate signals
    travel_signals_found = sorted(set(travel_signals_found))
    competitors_found = sorted(set(competitors_found))

    # Contact search if still empty
    if not contact_name:
        contact_query = f"{name} activities director OR trip coordinator"
        print(f"    Contact search: {contact_query}")
        contact_text_combined = ""
        for url in google_search_urls(contact_query, num_results=2):
            text = fetch_page_text(url)
            time.sleep(REQUEST_DELAY)
            contact_text_combined += " " + text
            if not contact_name:
                contact_name = find_contact_name(contact_text_combined)

    school["source_urls"] = " | ".join(visited_urls)
    school["travel_signals"] = " | ".join(travel_signals_found)
    school["competitor_spotted"] = bool(competitors_found)
    school["competitors_found"] = " | ".join(competitors_found)
    school["contact_name"] = contact_name

    print(f"    Travel signals : {travel_signals_found or 'none'}")
    print(f"    Competitors    : {competitors_found or 'none'}")
    print(f"    Contact        : {contact_name or 'not found'}")

    return school


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  EDUCATIONAL TRAVEL PROSPECT RESEARCH TOOL")
    print(f"  Run date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    # ── Stage 1 ──────────────────────────────
    print(f"\n[Stage 1] Loading '{INPUT_CSV}' and scoring schools...")
    schools = load_and_score(INPUT_CSV)
    top_schools = schools[:TOP_N]

    print(f"\nTop {TOP_N} schools by prospect score:\n")
    print(f"{'Rank':<5} {'School':<45} {'Score':<6} {'Type':<10}")
    print("-" * 70)
    for i, s in enumerate(top_schools, 1):
        print(f"{i:<5} {s['school_name']:<45} {s['score']:<6} {s['school_type']:<10}")

    # ── Stage 2 ──────────────────────────────
    print(f"\n[Stage 2] Beginning agentic web enrichment for {TOP_N} schools...")
    print(f"(Delay between requests: {REQUEST_DELAY}s)\n")

    enriched = []
    for school in top_schools:
        enriched.append(enrich_school(school))

    # ── Write output CSV ──────────────────────
    output_fields = [
        "school_name", "state", "school_type", "enrollment",
        "score",
        "competitor_spotted", "competitors_found",
        "travel_signals",
        "contact_name",
        "source_urls",
        # keep original fields at end for reference
        "median_household_income", "international_baccalaureate",
        "ap_courses_count", "has_language_program", "travel_club_mentioned",
        "has_honors_program", "has_arts_program",
    ]

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=output_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(enriched)

    print(f"\n[Output] Wrote {len(enriched)} records → '{OUTPUT_CSV}'")

    # ── Summary ──────────────────────────────
    n_competitor = sum(1 for s in enriched if s.get("competitor_spotted"))
    n_travel     = sum(1 for s in enriched if s.get("travel_signals"))
    n_contact    = sum(1 for s in enriched if s.get("contact_name"))

    print("\n" + "=" * 60)
    print("  ENRICHMENT SUMMARY")
    print("=" * 60)
    print(f"  Schools enriched          : {len(enriched)}")
    print(f"  Competitor signal found   : {n_competitor}  ({n_competitor/len(enriched)*100:.0f}%)")
    print(f"  Travel program evidence   : {n_travel}  ({n_travel/len(enriched)*100:.0f}%)")
    print(f"  Contact name found        : {n_contact}  ({n_contact/len(enriched)*100:.0f}%)")
    print("=" * 60)

    # Per-school competitor breakdown
    if n_competitor:
        print("\n  Schools with competitor signals:")
        for s in enriched:
            if s.get("competitor_spotted"):
                print(f"    • {s['school_name']}: {s['competitors_found']}")


if __name__ == "__main__":
    main()
