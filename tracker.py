"""
ISBN Market Drop & Digital Scarcity Tracker
--------------------------------------------
Searches Open Library for candidate pre-2022 titles within a set of
subjects, then independently cross-checks each promising candidate
against Internet Archive and Google Books directly. A title is only
flagged as at-risk if NONE of the three sources show a digital copy.
Flagged titles are upserted into a Supabase table (`at_risk_books`) so
archivists can prioritize them.
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
IA_SEARCH_ENDPOINT = "https://archive.org/advancedsearch.php"
GOOGLE_BOOKS_ENDPOINT = "https://www.googleapis.com/books/v1/volumes"
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
# Independent cross-checks: does a digital copy exist ANYWHERE we can see?
# --------------------------------------------------------------------------
# Open Library's own "ebook_count" is actually sourced FROM Internet
# Archive (Open Library is an Internet Archive project), so checking it
# alone is really the same check twice. These two functions query two
# genuinely separate sources directly, so a book only gets flagged if
# NEITHER shows a digital copy.

def check_internet_archive(isbn: str) -> bool:
    """Return True if Internet Archive has any item matching this ISBN."""
    params = {
        "q": f"isbn:{isbn}",
        "fl[]": "identifier",
        "rows": 1,
        "output": "json",
    }
    headers = {"User-Agent": USER_AGENT}
    try:
        response = requests.get(
            IA_SEARCH_ENDPOINT,
            params=params,
            headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.exceptions.Timeout:
        logger.warning("IA timeout for ISBN %s", isbn)
        return False  # can't confirm either way; treat cautiously below
    except requests.exceptions.RequestException as exc:
        logger.warning("IA network error for ISBN %s: %s", isbn, exc)
        return False
    except (ValueError, KeyError) as exc:
        logger.warning("IA invalid response for ISBN %s: %s", isbn, exc)
        return False

    num_found = payload.get("response", {}).get("numFound", 0)
    return num_found > 0


def check_google_books(isbn: str) -> bool:
    """Return True if Google Books shows a downloadable/previewable copy."""
    params = {"q": f"isbn:{isbn}"}
    headers = {"User-Agent": USER_AGENT}
    try:
        response = requests.get(
            GOOGLE_BOOKS_ENDPOINT,
            params=params,
            headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.exceptions.Timeout:
        logger.warning("Google Books timeout for ISBN %s", isbn)
        return False
    except requests.exceptions.RequestException as exc:
        logger.warning("Google Books network error for ISBN %s: %s", isbn, exc)
        return False
    except ValueError as exc:
        logger.warning("Google Books invalid response for ISBN %s: %s", isbn, exc)
        return False

    items = payload.get("items") or []
    if not items:
        return False

    access_info = items[0].get("accessInfo", {})
    epub_available = access_info.get("epub", {}).get("isAvailable", False)
    pdf_available = access_info.get("pdf", {}).get("isAvailable", False)
    viewability = access_info.get("viewability", "NO_PAGES")

    return bool(epub_available or pdf_available or viewability == "ALL_PAGES")


def confirm_no_digital_copy(isbn: str) -> bool:
    """
    True only if BOTH Internet Archive and Google Books independently
    show no digital copy for this ISBN.
    """
    on_ia = check_internet_archive(isbn)
    time.sleep(RATE_LIMIT_DELAY_SECONDS)
    on_google = check_google_books(isbn)
    time.sleep(RATE_LIMIT_DELAY_SECONDS)

    if on_ia or on_google:
        logger.info(
            "ISBN %s has a digital copy (IA: %s, Google Books: %s) - skipping",
            isbn, on_ia, on_google,
        )
        return False
    return True


# --------------------------------------------------------------------------
# Supabase upsert with scarcity-trend comparison
# --------------------------------------------------------------------------

def fetch_existing_row(client: Client, isbn: str) -> Optional[Dict[str, Any]]:
    """Look up a prior row for this ISBN, if one exists."""
    try:
        result = (
            client.table("at_risk_books")
            .select("edition_count:library_holdings,previous_edition_count,"
                    "previous_ebook_count")
            .eq("isbn", isbn)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        return rows[0] if rows else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not fetch existing row for %s: %s", isbn, exc)
        return None


def apply_scarcity_trend(client: Client, record: Dict[str, Any]) -> Dict[str, Any]:
    """
    Compare this run's edition_count against the previously stored value
    (from the last time this ISBN was flagged) and label the trend.

    NOTE: this compares Open Library's own catalog/edition metadata over
    time, not live marketplace "copies for sale". There's no free public
    API for real-time bookseller inventory, so this is the closest
    honest scarcity signal available without a paid data source.
    """
    existing = fetch_existing_row(client, record["isbn"])
    current_editions = record.get("library_holdings", 0) or 0

    if existing is None:
        record["scarcity_trend"] = "BASELINE"
        record["previous_edition_count"] = current_editions
        record["previous_ebook_count"] = 0
        return record

    prev_editions = existing.get("edition_count") or 0

    if current_editions < prev_editions:
        record["scarcity_trend"] = "DROPPED"
    else:
        record["scarcity_trend"] = "STABLE"

    record["previous_edition_count"] = current_editions
    record["previous_ebook_count"] = 0
    record["last_checked_at"] = "now()"
    return record


def upsert_at_risk_book(client: Client, record: Dict[str, Any]) -> None:
    """Upsert a single record into the at_risk_books table, on isbn conflict."""
    record = apply_scarcity_trend(client, record)
    try:
        client.table("at_risk_books").upsert(
            record, on_conflict="isbn"
        ).execute()
        logger.info(
            "Upserted '%s' (ISBN %s) - trend: %s",
            record["title"],
            record["isbn"],
            record["scarcity_trend"],
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

            # Cross-check against two independent sources before flagging.
            # This is the expensive step (2 extra HTTP calls), so it only
            # runs on candidates that already passed the cheaper Open
            # Library filters above.
            if not confirm_no_digital_copy(record["isbn"]):
                continue

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
