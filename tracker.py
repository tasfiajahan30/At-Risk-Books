"""
ISBN Market Drop & Digital Scarcity Tracker
--------------------------------------------
Searches Open Library for candidate pre-2022 titles within a set of
subjects, using each work's own `ebook_count` to judge whether ANY
digital scan exists anywhere for that work. Titles with zero digital
copies AND a low edition count (a rarity signal) are upserted into a
Supabase table (`at_risk_books`) so archivists can prioritize them.

This replaces the older "hand-typed ISBN list" approach: instead of you
supplying ISBNs to check, the script actively searches for candidates.

Environment variables required:
    SUPABASE_URL   - your Supabase project URL
    SUPABASE_KEY   - a Supabase SERVICE ROLE key (server-side only;
                     never expose this key in the frontend)

Dependencies:
    pip install requests supabase
"""

import os
import sys
import time
import logging
from typing import Optional, Dict, Any, List

import requests
from supabase import create_client, Client

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

SEARCH_ENDPOINT = "https://openlibrary.org/search.json"
REQUEST_TIMEOUT_SECONDS = 15
RATE_LIMIT_DELAY_SECONDS = 1.0
USER_AGENT = "ISBNScarcityTracker/1.0 (contact: archivist@example.org)"

# Subjects to search for candidates in. Edit this list to steer the kind
# of books you're trying to preserve (local history, small-press poetry,
# regional literature, out-of-print academic work, etc.)
CANDIDATE_SUBJECTS: List[str] = [
    "local history",
    "regional literature",
    "small press",
    "out of print",
]

# Only consider books first published before this year.
MAX_PUBLISH_YEAR = 2022

# Rarity heuristic: skip works with more than this many editions, since
# heavily re-printed books are unlikely to be truly at risk.
MAX_EDITION_COUNT = 3

# How many search results to pull per subject, per page.
RESULTS_PER_SUBJECT = 50

# Safety cap: max number of rows written to Supabase in a single run.
MAX_FLAGGED_PER_RUN = 25

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("isbn_tracker")


# --------------------------------------------------------------------------
# Supabase client setup
# --------------------------------------------------------------------------

def get_supabase_client() -> Client:
    """Build a Supabase client from environment variables, or exit cleanly."""
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_KEY")

    if not supabase_url or not supabase_key:
        logger.error(
            "Missing SUPABASE_URL or SUPABASE_KEY environment variables. "
            "Set them locally or as GitHub Repository Secrets."
        )
        sys.exit(1)

    try:
        return create_client(supabase_url, supabase_key)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to initialize Supabase client: %s", exc)
        sys.exit(1)


# --------------------------------------------------------------------------
# Open Library discovery search
# --------------------------------------------------------------------------

def search_subject(subject: str) -> List[Dict[str, Any]]:
    """
    Search Open Library for candidate works within a subject.
    Returns the raw list of "doc" records from the search API.
    """
    params = {
        "q": f'subject:"{subject}"',
        "fields": "title,author_name,isbn,first_publish_year,"
                  "edition_count,ebook_count_i",
        "limit": RESULTS_PER_SUBJECT,
        "sort": "old",  # bias toward older, more likely out-of-print works
    }
    headers = {"User-Agent": USER_AGENT}

    try:
        response = requests.get(
            SEARCH_ENDPOINT,
            params=params,
            headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.exceptions.Timeout:
        logger.warning("Timeout while searching subject '%s'", subject)
        return []
    except requests.exceptions.RequestException as exc:
        logger.warning("Network/API error searching '%s': %s", subject, exc)
        return []
    except ValueError as exc:
        logger.warning("Invalid JSON for subject '%s': %s", subject, exc)
        return []

    return payload.get("docs", [])


def parse_candidate(doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Turn a raw Open Library search "doc" into our record shape, or return
    None if the doc doesn't qualify (no ISBN, too recent, too many
    editions, or a digital copy already exists somewhere).
    """
    isbns = doc.get("isbn") or []
    if not isbns:
        return None
    isbn = isbns[0]

    publish_year = doc.get("first_publish_year")
    if publish_year and publish_year > MAX_PUBLISH_YEAR:
        return None

    edition_count = doc.get("edition_count", 0) or 0
    if edition_count > MAX_EDITION_COUNT:
        return None  # too widely reprinted to be "rare"

    ebook_count = doc.get("ebook_count_i", 0) or 0
    has_digital_copy = ebook_count > 0

    if has_digital_copy:
        return None  # already preserved digitally somewhere; not at risk

    title = doc.get("title", "Unknown Title")
    authors = doc.get("author_name") or []
    author = ", ".join(authors) if authors else "Unknown"

    return {
        "isbn": isbn,
        "title": title,
        "author": author,
        "publish_year": publish_year,
        "has_digital_copy": False,
        "library_holdings": edition_count,
        "risk_status": "HIGH RISK",
    }


# --------------------------------------------------------------------------
# Supabase upsert
# --------------------------------------------------------------------------

def upsert_at_risk_book(client: Client, record: Dict[str, Any]) -> None:
    """Upsert a single record into the at_risk_books table, on isbn conflict."""
    try:
        client.table("at_risk_books").upsert(
            record, on_conflict="isbn"
        ).execute()
        logger.info(
            "Upserted '%s' (ISBN %s) - digital copy: %s",
            record["title"],
            record["isbn"],
            record["has_digital_copy"],
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Supabase upsert failed for ISBN %s: %s", record["isbn"], exc
        )


# --------------------------------------------------------------------------
# Main run loop
# --------------------------------------------------------------------------

def run_scan(subjects: List[str]) -> None:
    client = get_supabase_client()

    flagged_count = 0
    checked_count = 0
    seen_isbns = set()

    for subject in subjects:
        if flagged_count >= MAX_FLAGGED_PER_RUN:
            break

        logger.info("Searching subject '%s'...", subject)
        docs = search_subject(subject)
        logger.info("  -> %d candidate works returned", len(docs))

        for doc in docs:
            checked_count += 1
            try:
                record = parse_candidate(doc)
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to parse a candidate doc: %s", exc)
                continue

            if record is None:
                continue  # didn't qualify (too common, has a scan, etc.)

            if record["isbn"] in seen_isbns:
                continue
            seen_isbns.add(record["isbn"])

            upsert_at_risk_book(client, record)
            flagged_count += 1

            if flagged_count >= MAX_FLAGGED_PER_RUN:
                logger.info("Reached MAX_FLAGGED_PER_RUN cap; stopping.")
                break

        # Respect Open Library's rate limits between subject searches.
        time.sleep(RATE_LIMIT_DELAY_SECONDS)

    logger.info(
        "Scan complete. Checked %d candidates, flagged %d as at-risk.",
        checked_count,
        flagged_count,
    )


if __name__ == "__main__":
    run_scan(CANDIDATE_SUBJECTS)
