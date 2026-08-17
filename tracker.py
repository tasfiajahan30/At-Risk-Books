"""
ISBN Market Drop & Digital Scarcity Tracker
--------------------------------------------
Queries the Open Library Books API for a configurable list of pre-2022,
out-of-print ISBNs, determines whether a digital (scanned/fulltext) copy
exists, and upserts any title lacking one into a Supabase table
(`at_risk_books`) so archivists can prioritize preservation.

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

OPEN_LIBRARY_ENDPOINT = "https://openlibrary.org/api/books"
REQUEST_TIMEOUT_SECONDS = 10
RATE_LIMIT_DELAY_SECONDS = 1.0
USER_AGENT = "ISBNScarcityTracker/1.0 (contact: archivist@example.org)"

# Configurable list of sample pre-2022 ISBNs to monitor.
# Replace/extend this list with the titles you want to track.
MONITORED_ISBNS: List[str] = [
    "9780140449136",  # Homer, The Odyssey (Penguin Classics)
    "9780679732761",  # Cormac McCarthy, Blood Meridian
    "9780345391803",  # Douglas Adams, Hitchhiker's Guide to the Galaxy
    "9780060850524",  # Aldous Huxley, Brave New World
    "9780393315286",  # Chinua Achebe, Things Fall Apart
]

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
# Open Library lookups
# --------------------------------------------------------------------------

def fetch_book_data(isbn: str) -> Optional[Dict[str, Any]]:
    """
    Query the Open Library Books API for a single ISBN.
    Returns the parsed JSON payload for that ISBN, or None on failure.
    """
    params = {
        "bibkeys": f"ISBN:{isbn}",
        "format": "json",
        "jscmd": "data",
    }
    headers = {"User-Agent": USER_AGENT}

    try:
        response = requests.get(
            OPEN_LIBRARY_ENDPOINT,
            params=params,
            headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.exceptions.Timeout:
        logger.warning("Timeout while querying ISBN %s", isbn)
        return None
    except requests.exceptions.RequestException as exc:
        logger.warning("Network/API error for ISBN %s: %s", isbn, exc)
        return None
    except ValueError as exc:
        logger.warning("Invalid JSON response for ISBN %s: %s", isbn, exc)
        return None

    key = f"ISBN:{isbn}"
    book_entry = payload.get(key)

    if not book_entry:
        logger.info("No Open Library record found for ISBN %s", isbn)
        return None

    return book_entry


def parse_book_record(isbn: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract the fields we care about from an Open Library `data` entry,
    defaulting gracefully whenever a key is missing.
    """
    title = entry.get("title", "Unknown Title")

    authors_list = entry.get("authors", [])
    if authors_list:
        author = ", ".join(a.get("name", "Unknown") for a in authors_list)
    else:
        author = "Unknown"

    publish_date = entry.get("publish_date")
    publish_year = None
    if publish_date:
        # publish_date is free text (e.g. "March 1999"); pull the last
        # 4-digit run as a best-effort year.
        digits = "".join(ch if ch.isdigit() else " " for ch in publish_date)
        candidates = [tok for tok in digits.split() if len(tok) == 4]
        if candidates:
            try:
                publish_year = int(candidates[-1])
            except ValueError:
                publish_year = None

    # Digital scarcity check: look for an "ebooks" block with fulltext
    # access, or an explicit has_fulltext flag.
    ebooks = entry.get("ebooks", [])
    has_digital_copy = False
    for ebook in ebooks:
        if ebook.get("preview") in ("full", "borrow") or ebook.get(
            "read_url"
        ):
            has_digital_copy = True
            break
    if entry.get("has_fulltext"):
        has_digital_copy = True

    # Open Library doesn't give a "library holdings" count directly via
    # this endpoint; default to 0 unless a future data source is wired in.
    library_holdings = entry.get("number_of_pages", 0) and 0

    return {
        "isbn": isbn,
        "title": title,
        "author": author,
        "publish_year": publish_year,
        "has_digital_copy": has_digital_copy,
        "library_holdings": library_holdings,
        "risk_status": "HIGH RISK" if not has_digital_copy else "MONITORED",
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

def run_scan(isbns: List[str]) -> None:
    client = get_supabase_client()

    flagged_count = 0

    for isbn in isbns:
        logger.info("Checking ISBN %s...", isbn)

        entry = fetch_book_data(isbn)
        if entry is None:
            time.sleep(RATE_LIMIT_DELAY_SECONDS)
            continue

        try:
            record = parse_book_record(isbn, entry)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to parse record for ISBN %s: %s", isbn, exc)
            time.sleep(RATE_LIMIT_DELAY_SECONDS)
            continue

        # Only flag titles that currently lack a digital copy.
        if not record["has_digital_copy"]:
            upsert_at_risk_book(client, record)
            flagged_count += 1
        else:
            logger.info(
                "'%s' (ISBN %s) already has a digital copy - skipping.",
                record["title"],
                isbn,
            )

        # Respect Open Library's rate limits.
        time.sleep(RATE_LIMIT_DELAY_SECONDS)

    logger.info(
        "Scan complete. %d/%d titles flagged as at-risk.",
        flagged_count,
        len(isbns),
    )


if __name__ == "__main__":
    run_scan(MONITORED_ISBNS)
