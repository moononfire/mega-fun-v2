"""
Email scraper — subprocess.
Usage: python scripts/scrape_emails.py [--op-id N] [--source-query "query"] [--business-ids "1,2,3"] [--max-pages 10]

Visits business websites and extracts email addresses.
For each business: crawls the homepage + internal links (up to --max-pages limit).
Saves results to emails table and logs to operations_log.
"""

import sys
import os
import re
import json
import time
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse, quote, urlunparse

# VPS Worker: client context
CLIENT_SLUG = sys.argv[1] if len(sys.argv) > 1 else "default"
BASE_DIR    = Path(f"/home/deploy/clients/{CLIENT_SLUG}")
INPUT_DIR   = BASE_DIR / "input"
OUTPUT_DIR  = BASE_DIR / "output"
params_kv   = dict(arg.split("=", 1) for arg in sys.argv[2:] if "=" in arg)

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from bs4 import BeautifulSoup
import sqlite3
from config import DATABASE

# Default max pages to crawl per business website
DEFAULT_MAX_PAGES = 5

# Number of concurrent worker threads for scraping
NUM_WORKERS = 25

# Email regex — matches common email patterns
EMAIL_RE = re.compile(
    r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}',
    re.IGNORECASE,
)

# Extensions that are NOT real emails (image files etc.)
IGNORE_EXTENSIONS = {
    'png', 'jpg', 'jpeg', 'gif', 'svg', 'webp', 'bmp', 'ico',
    'pdf', 'zip', 'rar', 'exe', 'mp3', 'mp4', 'avi', 'mov',
    'woff', 'woff2', 'ttf', 'eot', 'css', 'js',
}

# File extensions in URLs to skip (not HTML pages)
SKIP_URL_EXTENSIONS = {
    '.pdf', '.zip', '.rar', '.exe', '.mp3', '.mp4', '.avi', '.mov',
    '.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.bmp', '.ico',
    '.woff', '.woff2', '.ttf', '.eot', '.css', '.js', '.xml', '.json',
    '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
}

# Common dummy/system emails to skip
IGNORE_EMAILS = {
    'example@example.com', 'test@test.com', 'email@example.com',
    'name@domain.com', 'user@example.com', 'your@email.com',
    'noreply@', 'no-reply@',
}

# Priority subpages to crawl first (contact pages usually have emails)
PRIORITY_PATHS = [
    '/kontakt', '/contact', '/kontakt.html', '/contact.html',
    '/about', '/o-nas', '/about-us', '/impressum',
    '/kontakty', '/dane-kontaktowe', '/napisz-do-nas',
]

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9,pl;q=0.8',
}

# Prefixes that suggest a decision-maker inbox
_OWNER_PREFIXES = {
    'dyrektor', 'prezes', 'szef', 'wlasciciel', 'owner', 'ceo', 'zarzad',
    'kierownik', 'director', 'boss', 'zarzadzajacy', 'partner',
}
# Prefixes that suggest the main office inbox
_OFFICE_PREFIXES = {
    'biuro', 'office', 'sekretariat', 'recepcja', 'kancelaria', 'firma',
}
# Generic "contact us" inboxes
_CONTACT_PREFIXES = {
    'kontakt', 'contact', 'mail', 'napisz', 'email', 'poczta', 'zapytanie',
}
# Low-value / broadcast inboxes
_LOW_PREFIXES = {
    'info', 'hello', 'hej', 'reklama', 'marketing', 'newsletter', 'oferty',
    'spam', 'noreply', 'no-reply', 'donotreply', 'rodo', 'iod', 'help',
    'support', 'pomoc', 'serwis', 'service', 'sklep', 'shop', 'zamowienia',
    'orders', 'faktury', 'invoice', 'invoices', 'reklamacje',
}

_NAME_PATTERN = re.compile(r'^[a-z]{2,}(?:\.[a-z]{2,})+$')   # jan.kowalski, a.b.c
_ABBR_PATTERN = re.compile(r'^[a-z]{1,3}\.[a-z]{3,}$')        # j.kowalski


def score_email(email: str) -> int:
    """Return a priority score for choosing a primary email. Higher = better."""
    local = email.split('@')[0].lower()

    # Named email — highest priority (reaches a real person)
    if _NAME_PATTERN.match(local):
        return 100
    if _ABBR_PATTERN.match(local):
        return 85

    # Decision-maker keywords
    if any(local == p or local.startswith(p) for p in _OWNER_PREFIXES):
        return 90

    # Office / main inbox
    if any(local == p or local.startswith(p) for p in _OFFICE_PREFIXES):
        return 70

    # Generic contact
    if any(local == p or local.startswith(p) for p in _CONTACT_PREFIXES):
        return 50

    # Low-value broadcast
    if any(local == p or local.startswith(p) for p in _LOW_PREFIXES):
        return 20

    # Fallback — unknown but possibly real
    return 40


def get_db():
    conn = sqlite3.connect(DATABASE, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    return conn


def log_operation(status, details, op_id=None):
    db = get_db()
    if op_id is None:
        cursor = db.execute(
            "INSERT INTO operations_log (operation_type, status, details) VALUES ('email_scrape', ?, ?)",
            (status, details),
        )
        op_id = cursor.lastrowid
    else:
        db.execute(
            "UPDATE operations_log SET status = ?, details = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, details, op_id),
        )
    db.commit()
    db.close()
    return op_id


def is_valid_email(email):
    """Filter out fake/system/file emails."""
    email_lower = email.lower().strip()

    # Check extension — the TLD part
    tld = email_lower.rsplit('.', 1)[-1]
    if tld in IGNORE_EXTENSIONS:
        return False

    # Check against known dummy emails
    for ignore in IGNORE_EMAILS:
        if email_lower == ignore or email_lower.startswith(ignore):
            return False

    # Must have reasonable length
    if len(email_lower) < 5 or len(email_lower) > 254:
        return False

    return True


def extract_emails_from_html(html):
    """Extract emails from HTML string using multiple methods."""
    emails = set()
    soup = BeautifulSoup(html, 'lxml')

    # Method 1: mailto: links
    for link in soup.find_all('a', href=True):
        href = link['href']
        if href.startswith('mailto:'):
            email = href.replace('mailto:', '').split('?')[0].strip()
            if EMAIL_RE.match(email) and is_valid_email(email):
                emails.add(email.lower())

    # Method 2: Regex on visible text
    text = soup.get_text(separator=' ')
    for match in EMAIL_RE.findall(text):
        if is_valid_email(match):
            emails.add(match.lower())

    # Method 3: Regex on raw HTML (attributes, comments, etc.)
    for match in EMAIL_RE.findall(html):
        if is_valid_email(match):
            emails.add(match.lower())

    return emails, soup


def get_internal_links(soup, base_url):
    """Extract internal links from parsed HTML. Returns list of absolute URLs."""
    parsed_base = urlparse(base_url)
    base_domain = parsed_base.netloc.lower()
    links = []

    for a in soup.find_all('a', href=True):
        href = a['href'].strip()

        # Skip anchors, javascript, mailto, tel
        if href.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
            continue

        # Build absolute URL
        abs_url = urljoin(base_url, href)
        parsed = urlparse(abs_url)

        # Must be same domain
        if parsed.netloc.lower() != base_domain:
            continue

        # Must be http/https
        if parsed.scheme not in ('http', 'https'):
            continue

        # Skip file downloads
        path_lower = parsed.path.lower()
        if any(path_lower.endswith(ext) for ext in SKIP_URL_EXTENSIONS):
            continue

        # Normalize: strip fragment, keep path+query
        clean_url = parsed._replace(fragment='').geturl()
        links.append(clean_url)

    return links


def sanitize_url(url):
    """Sanitize a URL for Windows: strip whitespace, IDNA-encode non-ASCII hostname,
    percent-encode non-ASCII path/query. Returns None if the URL is unrecoverable."""
    url = url.strip()
    if not url:
        return None
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or ''
        # IDNA-encode hostname containing non-ASCII (e.g. Polish IDN domains)
        if any(ord(c) > 127 for c in hostname):
            try:
                encoded_host = hostname.encode('idna').decode('ascii')
            except (UnicodeError, UnicodeDecodeError):
                return None  # hostname can't be encoded — skip
            port = f':{parsed.port}' if parsed.port else ''
            parsed = parsed._replace(netloc=encoded_host + port)
        # Percent-encode non-ASCII characters in path and query
        path = quote(parsed.path, safe='/:@!$&\'()*+,;=')
        query = quote(parsed.query, safe='=&+%')
        parsed = parsed._replace(path=path, query=query)
        return urlunparse(parsed)
    except Exception:
        return url  # Return original if sanitization itself errors


def crawl_website(url, max_pages):
    """Crawl a website starting from url, visiting up to max_pages internal pages.

    Returns (all_emails: dict[email->source_url], error: str|None, pages_visited: int).
    Priority: contact-like pages are visited first.
    """
    all_emails = {}  # email -> url where it was first found

    # Ensure URL has scheme
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url

    # Sanitize the starting URL (non-ASCII hostname/path → [Errno 22] on Windows)
    url = sanitize_url(url) or url

    visited = set()
    to_visit_priority = []  # contact-like pages first
    to_visit_normal = [url]  # then other internal links

    try:
        while len(visited) < max_pages and (to_visit_priority or to_visit_normal):
            # Pick next URL: priority pages first
            if to_visit_priority:
                current_url = to_visit_priority.pop(0)
            else:
                current_url = to_visit_normal.pop(0)

            # Normalize for dedup
            normalized = urlparse(current_url)._replace(fragment='').geturl()
            if normalized in visited:
                continue
            visited.add(normalized)

            try:
                safe_url = sanitize_url(current_url)
                if safe_url is None:
                    continue
                resp = requests.get(safe_url, headers=HEADERS, timeout=2, allow_redirects=True)
                # Rate-limited or blocked — skip entire domain
                if resp.status_code == 429:
                    break
                if resp.status_code != 200:
                    continue

                # Only parse HTML responses
                content_type = resp.headers.get('Content-Type', '')
                if 'text/html' not in content_type:
                    continue

                page_emails, soup = extract_emails_from_html(resp.text)
                for em in page_emails:
                    if em not in all_emails:
                        all_emails[em] = resp.url  # record page where email was first found

                # Extract internal links for further crawling
                if len(visited) < max_pages:
                    links = get_internal_links(soup, resp.url)
                    for link in links:
                        link_norm = urlparse(link)._replace(fragment='').geturl()
                        if link_norm in visited:
                            continue
                        # Check if it's a priority (contact-like) path
                        link_path = urlparse(link).path.lower().rstrip('/')
                        is_priority = any(
                            link_path == p.rstrip('/') or link_path.endswith(p.rstrip('/'))
                            for p in PRIORITY_PATHS
                        )
                        if is_priority and link_norm not in [urlparse(u)._replace(fragment='').geturl() for u in to_visit_priority]:
                            to_visit_priority.append(link)
                        elif link_norm not in [urlparse(u)._replace(fragment='').geturl() for u in to_visit_normal]:
                            to_visit_normal.append(link)

                # Small delay between requests to same site
                time.sleep(random.uniform(0.3, 0.5))

            except Exception:
                continue

    except Exception as e:
        return all_emails, str(e), len(visited)

    return all_emails, None, len(visited)



def _scrape_one(biz, max_pages, counters, lock):
    """Scrape a single business website. Safe to call concurrently from multiple threads."""
    biz_id = biz['id']
    website = biz['website']
    try:
        emails, error, pages_visited = crawl_website(website, max_pages)
        with lock:
            counters['pages_visited'] += pages_visited
            if error:
                counters['errors'] += 1
            counters['found'] += len(emails)

        db = get_db()
        if emails:
            for email, email_source in emails.items():
                cursor = db.execute(
                    "INSERT OR IGNORE INTO emails (email, business_id, source) VALUES (?, ?, ?)",
                    (email, biz_id, email_source),
                )
                if cursor.rowcount > 0:
                    with lock:
                        counters['saved'] += 1
            existing_primary = db.execute(
                "SELECT id FROM emails WHERE business_id = ? AND is_primary = 1 LIMIT 1",
                (biz_id,)
            ).fetchone()
            if not existing_primary:
                all_biz_emails = db.execute(
                    "SELECT id, email FROM emails WHERE business_id = ?",
                    (biz_id,)
                ).fetchall()
                if all_biz_emails:
                    best = max(all_biz_emails, key=lambda r: score_email(r['email']))
                    db.execute("UPDATE emails SET is_primary = 1 WHERE id = ?", (best['id'],))
        db.execute(
            "UPDATE businesses SET email_scraped_at = CURRENT_TIMESTAMP, email_scraped_website = ?, email_scrape_pending = 0 WHERE id = ?",
            (website, biz_id),
        )
        db.commit()
        db.close()
    except Exception as e:
        with lock:
            counters['errors'] += 1
        print(json.dumps({
            "status": "error",
            "business_id": biz_id,
            "website": website,
            "error": str(e),
        }), flush=True)
    finally:
        with lock:
            counters['completed'] += 1


def main():
    # Params from VPS runner (key=value pairs in sys.argv[2:]) with env var fallback
    op_id_raw = params_kv.get("op_id") or params_kv.get("op-id") or os.environ.get("SCRAPE_OP_ID")
    max_pages_raw = params_kv.get("max_pages") or params_kv.get("max-pages") or os.environ.get("SCRAPE_MAX_PAGES")
    source_query = params_kv.get("source_query") or params_kv.get("source-query") or os.environ.get("SCRAPE_SOURCE_QUERY") or None
    business_ids = params_kv.get("business_ids") or params_kv.get("business-ids") or os.environ.get("SCRAPE_BUSINESS_IDS") or None
    country = params_kv.get("country") or os.environ.get("SCRAPE_COUNTRY") or None
    city = params_kv.get("city") or os.environ.get("SCRAPE_CITY") or None
    max_pages = int(max_pages_raw) if max_pages_raw else DEFAULT_MAX_PAGES

    workspace_id = int(os.environ.get("WORKSPACE_ID", "1"))
    op_id = (int(op_id_raw) if op_id_raw else None) or log_operation("running", "Rozpoczynanie scrapowania emaili...")

    try:
        db = get_db()

        # Build query for businesses with websites (skip those with emails or already scraped)
        conditions = [
            "workspace_id = ?",
            "website IS NOT NULL",
            "website != ''",
            "id NOT IN (SELECT DISTINCT business_id FROM emails WHERE business_id IS NOT NULL)",
            "(email_scraped_at IS NULL OR COALESCE(email_scraped_website, '') != COALESCE(website, ''))",
        ]
        params = [workspace_id]

        if business_ids:
            ids = [int(x.strip()) for x in business_ids.split(',') if x.strip()]
            placeholders = ','.join('?' * len(ids))
            conditions.append(f"id IN ({placeholders})")
            params.extend(ids)

        if source_query:
            conditions.append("source_query = ?")
            params.append(source_query)

        if country:
            conditions.append("country = ?")
            params.append(country)

        if city:
            conditions.append("city = ?")
            params.append(city)

        where = "WHERE " + " AND ".join(conditions)
        businesses = db.execute(
            f"SELECT id, name, website FROM businesses {where}", params
        ).fetchall()

        total = len(businesses)
        if total == 0:
            db.close()
            log_operation("done", "Brak biznesow z stronami www do przeskanowania", op_id)
            print(json.dumps({"status": "done", "found": 0, "saved": 0, "errors": 0}), flush=True)
            return

        # Mark all fetched businesses as pending before crawling starts
        biz_ids = [biz['id'] for biz in businesses]
        placeholders = ','.join('?' * len(biz_ids))
        db.execute(f"UPDATE businesses SET email_scrape_pending = 1 WHERE id IN ({placeholders})", biz_ids)
        db.commit()
        db.close()

        log_operation("running", f"Skanowanie {total} stron www (max {max_pages} podstron/biznes, {NUM_WORKERS} watkow)...", op_id)
        print(json.dumps({"status": "running", "total": total}), flush=True)

        counters = {'found': 0, 'saved': 0, 'errors': 0, 'pages_visited': 0, 'completed': 0}
        lock = threading.Lock()

        with ThreadPoolExecutor(max_workers=NUM_WORKERS) as executor:
            futures = {executor.submit(_scrape_one, biz, max_pages, counters, lock): biz for biz in businesses}
            for future in as_completed(futures):
                future.result()  # re-raise any unexpected exceptions
                with lock:
                    completed = counters['completed']
                    found = counters['found']
                    saved = counters['saved']
                    pages = counters['pages_visited']

                if completed % 3 == 0 or completed == total:
                    progress = (
                        f"Postep: {completed}/{total} biznesow, "
                        f"{pages} podstron, "
                        f"znaleziono {found} emaili, zapisano {saved}"
                    )
                    log_operation("running", progress, op_id)

                print(json.dumps({
                    "status": "progress",
                    "current": completed,
                    "total": total,
                    "found": found,
                    "saved": saved,
                    "pages_visited": pages,
                }), flush=True)

        total_found = counters['found']
        total_saved = counters['saved']
        total_errors = counters['errors']
        total_pages_visited = counters['pages_visited']

        summary = (
            f"Przeskanowano {total} biznesow ({total_pages_visited} podstron), "
            f"znaleziono {total_found} emaili, zapisano {total_saved} nowych, bledow: {total_errors}"
        )
        log_operation("done", summary, op_id)
        print(json.dumps({
            "status": "done",
            "scanned": total,
            "pages_visited": total_pages_visited,
            "found": total_found,
            "saved": total_saved,
            "errors": total_errors,
        }), flush=True)

    except Exception as e:
        log_operation("error", str(e), op_id)
        print(json.dumps({"error": str(e)}), flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
