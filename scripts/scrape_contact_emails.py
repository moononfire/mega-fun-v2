"""
scrape_contact_emails.py — scrapes emails for snowman-v2 contacts.
Usage: python3 scrape_contact_emails.py <client_slug> contacts=<base64_json>

contacts param: base64-encoded JSON array of [{id: str, website: str}]
Output: output/emails.json — dict {contactId: primaryEmail}
"""

import sys
import os
import re
import json
import time
import random
import base64
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse, quote, urlunparse

CLIENT_SLUG = sys.argv[1] if len(sys.argv) > 1 else "default"
BASE_DIR    = Path(f"/home/deploy/clients/{CLIENT_SLUG}")
OUTPUT_DIR  = BASE_DIR / "output"
params_kv   = dict(arg.split("=", 1) for arg in sys.argv[2:] if "=" in arg)

import requests
from bs4 import BeautifulSoup

NUM_WORKERS = 20
DEFAULT_MAX_PAGES = 5

EMAIL_RE = re.compile(
    r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}',
    re.IGNORECASE,
)

IGNORE_EXTENSIONS = {
    'png', 'jpg', 'jpeg', 'gif', 'svg', 'webp', 'bmp', 'ico',
    'pdf', 'zip', 'rar', 'exe', 'mp3', 'mp4', 'avi', 'mov',
    'woff', 'woff2', 'ttf', 'eot', 'css', 'js',
}

SKIP_URL_EXTENSIONS = {
    '.pdf', '.zip', '.rar', '.exe', '.mp3', '.mp4', '.avi', '.mov',
    '.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.bmp', '.ico',
    '.woff', '.woff2', '.ttf', '.eot', '.css', '.js', '.xml', '.json',
    '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
}

IGNORE_EMAILS = {
    'example@example.com', 'test@test.com', 'email@example.com',
    'name@domain.com', 'user@example.com', 'your@email.com',
    'noreply@', 'no-reply@',
}

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

_OWNER_PREFIXES = {
    'dyrektor', 'prezes', 'szef', 'wlasciciel', 'owner', 'ceo', 'zarzad',
    'kierownik', 'director', 'boss', 'zarzadzajacy', 'partner',
}
_OFFICE_PREFIXES = {
    'biuro', 'office', 'sekretariat', 'recepcja', 'kancelaria', 'firma',
}
_CONTACT_PREFIXES = {
    'kontakt', 'contact', 'mail', 'napisz', 'email', 'poczta', 'zapytanie',
}
_LOW_PREFIXES = {
    'info', 'hello', 'hej', 'reklama', 'marketing', 'newsletter', 'oferty',
    'spam', 'noreply', 'no-reply', 'donotreply', 'rodo', 'iod', 'help',
    'support', 'pomoc', 'serwis', 'service', 'sklep', 'shop', 'zamowienia',
    'orders', 'faktury', 'invoice', 'invoices', 'reklamacje',
}

_NAME_PATTERN = re.compile(r'^[a-z]{2,}(?:\.[a-z]{2,})+$')
_ABBR_PATTERN = re.compile(r'^[a-z]{1,3}\.[a-z]{3,}$')


def score_email(email: str) -> int:
    local = email.split('@')[0].lower()
    if _NAME_PATTERN.match(local):
        return 100
    if _ABBR_PATTERN.match(local):
        return 85
    if any(local == p or local.startswith(p) for p in _OWNER_PREFIXES):
        return 90
    if any(local == p or local.startswith(p) for p in _OFFICE_PREFIXES):
        return 70
    if any(local == p or local.startswith(p) for p in _CONTACT_PREFIXES):
        return 50
    if any(local == p or local.startswith(p) for p in _LOW_PREFIXES):
        return 20
    return 40


def is_valid_email(email):
    email_lower = email.lower().strip()
    tld = email_lower.rsplit('.', 1)[-1]
    if tld in IGNORE_EXTENSIONS:
        return False
    for ignore in IGNORE_EMAILS:
        if email_lower == ignore or email_lower.startswith(ignore):
            return False
    if len(email_lower) < 5 or len(email_lower) > 254:
        return False
    return True


def extract_emails_from_html(html):
    emails = set()
    soup = BeautifulSoup(html, 'lxml')
    for link in soup.find_all('a', href=True):
        href = link['href']
        if href.startswith('mailto:'):
            email = href.replace('mailto:', '').split('?')[0].strip()
            if EMAIL_RE.match(email) and is_valid_email(email):
                emails.add(email.lower())
    text = soup.get_text(separator=' ')
    for match in EMAIL_RE.findall(text):
        if is_valid_email(match):
            emails.add(match.lower())
    for match in EMAIL_RE.findall(html):
        if is_valid_email(match):
            emails.add(match.lower())
    return emails, soup


def get_internal_links(soup, base_url):
    parsed_base = urlparse(base_url)
    base_domain = parsed_base.netloc.lower()
    links = []
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if href.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
            continue
        abs_url = urljoin(base_url, href)
        parsed = urlparse(abs_url)
        if parsed.netloc.lower() != base_domain:
            continue
        if parsed.scheme not in ('http', 'https'):
            continue
        path_lower = parsed.path.lower()
        if any(path_lower.endswith(ext) for ext in SKIP_URL_EXTENSIONS):
            continue
        clean_url = parsed._replace(fragment='').geturl()
        links.append(clean_url)
    return links


def sanitize_url(url):
    url = url.strip()
    if not url:
        return None
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or ''
        if any(ord(c) > 127 for c in hostname):
            try:
                encoded_host = hostname.encode('idna').decode('ascii')
            except (UnicodeError, UnicodeDecodeError):
                return None
            port = f':{parsed.port}' if parsed.port else ''
            parsed = parsed._replace(netloc=encoded_host + port)
        path = quote(parsed.path, safe='/:@!$&\'()*+,;=')
        query = quote(parsed.query, safe='=&+%')
        parsed = parsed._replace(path=path, query=query)
        return urlunparse(parsed)
    except Exception:
        return url


def crawl_website(url, max_pages):
    all_emails = {}
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    url = sanitize_url(url) or url
    visited = set()
    to_visit_priority = []
    to_visit_normal = [url]
    try:
        while len(visited) < max_pages and (to_visit_priority or to_visit_normal):
            current_url = to_visit_priority.pop(0) if to_visit_priority else to_visit_normal.pop(0)
            normalized = urlparse(current_url)._replace(fragment='').geturl()
            if normalized in visited:
                continue
            visited.add(normalized)
            try:
                safe_url = sanitize_url(current_url)
                if safe_url is None:
                    continue
                resp = requests.get(safe_url, headers=HEADERS, timeout=2, allow_redirects=True)
                if resp.status_code == 429:
                    break
                if resp.status_code != 200:
                    continue
                content_type = resp.headers.get('Content-Type', '')
                if 'text/html' not in content_type:
                    continue
                page_emails, soup = extract_emails_from_html(resp.text)
                for em in page_emails:
                    if em not in all_emails:
                        all_emails[em] = resp.url
                if len(visited) < max_pages:
                    links = get_internal_links(soup, resp.url)
                    for link in links:
                        link_norm = urlparse(link)._replace(fragment='').geturl()
                        if link_norm in visited:
                            continue
                        link_path = urlparse(link).path.lower().rstrip('/')
                        is_priority = any(
                            link_path == p.rstrip('/') or link_path.endswith(p.rstrip('/'))
                            for p in PRIORITY_PATHS
                        )
                        if is_priority:
                            to_visit_priority.append(link)
                        else:
                            to_visit_normal.append(link)
                time.sleep(random.uniform(0.3, 0.5))
            except Exception:
                continue
    except Exception as e:
        return all_emails, str(e), len(visited)
    return all_emails, None, len(visited)


def is_ignored_email(email, patterns):
    """Return True if the email matches any ignored pattern."""
    email_lower = email.lower()
    for p in patterns:
        p = p.strip().lower()
        if not p:
            continue
        if p.startswith('@') and email_lower.endswith(p):
            return True
        if p.endswith('@') and email_lower.startswith(p):
            return True
        if email_lower == p:
            return True
    return False


def _scrape_one(contact, max_pages, ignored_patterns, results, lock):
    contact_id = contact['id']
    website = contact['website']
    try:
        emails_dict, _error, _pages = crawl_website(website, max_pages)
        if emails_dict:
            # Filter out ignored patterns
            filtered = {e: s for e, s in emails_dict.items() if not is_ignored_email(e, ignored_patterns)}
            if filtered:
                # Sort all emails by score descending (best first)
                sorted_emails = sorted(filtered.keys(), key=score_email, reverse=True)
                with lock:
                    results[contact_id] = sorted_emails
    except Exception:
        pass
    finally:
        with lock:
            results.setdefault('__done__', 0)
            results['__done__'] = results.get('__done__', 0) + 1


def main():
    contacts_b64 = params_kv.get("contacts", "")
    max_pages_raw = params_kv.get("max_pages", "")
    ignored_raw = params_kv.get("ignored_patterns", "")
    max_pages = int(max_pages_raw) if max_pages_raw else DEFAULT_MAX_PAGES
    ignored_patterns = [p.strip() for p in ignored_raw.split(",") if p.strip()] if ignored_raw else []

    if ignored_patterns:
        print(f"Ignorowane wzorce ({len(ignored_patterns)}): {', '.join(ignored_patterns)}", flush=True)

    if not contacts_b64:
        print(json.dumps({"error": "Missing contacts param"}), flush=True)
        sys.exit(1)

    try:
        contacts = json.loads(base64.b64decode(contacts_b64 + "==").decode())
    except Exception as e:
        print(json.dumps({"error": f"Failed to decode contacts: {e}"}), flush=True)
        sys.exit(1)

    total = len(contacts)
    if total == 0:
        print("Brak kontaktów do przeskanowania.", flush=True)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUTPUT_DIR / "emails.json").write_text(json.dumps({}))
        return

    print(f"Start: {total} kontaktów, max {max_pages} podstron każda, {NUM_WORKERS} wątków.", flush=True)

    results = {}
    lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=NUM_WORKERS) as executor:
        futures = {executor.submit(_scrape_one, c, max_pages, ignored_patterns, results, lock): c for c in contacts}
        for future in as_completed(futures):
            future.result()
            with lock:
                done = results.get('__done__', 0)
                found = len([k for k in results if k != '__done__'])
            if done % 5 == 0 or done == total:
                print(f"Postęp: {done}/{total} — znaleziono emaile dla {found} kontaktów.", flush=True)

    output = {k: v for k, v in results.items() if k != '__done__'}

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "emails.json").write_text(json.dumps(output))

    total_emails = sum(len(v) for v in output.values())
    if output:
        print(f"Gotowe: znaleziono {total_emails} emaili ({len(output)} kontaktów) z {total} przeskanowanych.", flush=True)
        for contact_id, emails in output.items():
            print(f"  {contact_id[:8]}... → {', '.join(emails)}", flush=True)
    else:
        print(f"Gotowe: nie znaleziono żadnych emaili ({total} kontaktów przeskanowanych).", flush=True)


if __name__ == "__main__":
    main()
