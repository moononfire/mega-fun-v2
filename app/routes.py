import subprocess
import sys
import os
import re
import time
import json
import threading
import io
import csv
import urllib.parse as urlparse

from flask import Blueprint, render_template, request, jsonify, redirect, url_for, session, make_response
from app.db import get_db
from app.crypto import encrypt, decrypt
from config import APP_PASSWORD

# Brute force protection: track failed login attempts per IP
_login_attempts = {}  # ip -> {"count": int, "locked_until": float}
_MAX_ATTEMPTS = 5
_LOCKOUT_SECONDS = 300  # 5 minut


def _extract_source_query(details):
    """Extract source_query from operations_log details field.
    Handles both formats:
      - initial: 'Query: restauracja'
      - final:   'Znaleziono 60, zapisano 60 nowych (query: restauracja)'
    """
    if not details:
        return None
    m = re.search(r'\(query:\s*(.+?)\)\s*$', details)
    if m:
        return m.group(1).strip()
    if details.startswith("Query: "):
        return details[7:].strip()
    return None

main_bp = Blueprint("main", __name__)


def get_workspace_id():
    """Return the current workspace ID from the session (default: 1)."""
    return session.get("workspace_id", 1)


# Track running scrape processes {op_id: Popen}
_scrape_processes = {}


# 1x1 transparent GIF (binary)
_PIXEL_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00"
    b"!\xf9\x04\x00\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01"
    b"\x00\x00\x02\x02D\x01\x00;"
)


@main_bp.route("/track/<token>")
def track_open(token):
    """Tracking pixel — public, no auth required."""
    db = get_db()
    row = db.execute(
        "SELECT id, opened_at FROM campaign_emails WHERE open_token = ?", (token,)
    ).fetchone()
    if row:
        if row["opened_at"] is None:
            db.execute(
                "UPDATE campaign_emails SET opened_at = CURRENT_TIMESTAMP, open_count = 1 WHERE id = ?",
                (row["id"],),
            )
        else:
            db.execute(
                "UPDATE campaign_emails SET open_count = open_count + 1 WHERE id = ?",
                (row["id"],),
            )
        db.commit()
    db.close()
    resp = make_response(_PIXEL_GIF)
    resp.headers["Content-Type"] = "image/gif"
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


@main_bp.before_app_request
def require_login():
    allowed = ("main.login", "main.track_open", "static")
    if request.endpoint in allowed:
        return
    if not session.get("authenticated"):
        return redirect(url_for("main.login"))


@main_bp.route("/login", methods=["GET", "POST"])
def login():
    error = None
    ip = request.remote_addr

    if request.method == "POST":
        now = time.time()
        attempt = _login_attempts.get(ip, {"count": 0, "locked_until": 0})

        if attempt["locked_until"] > now:
            remaining = int(attempt["locked_until"] - now)
            return render_template("login.html", error=f"Zbyt wiele prób. Spróbuj za {remaining} sekund.")

        password = request.form.get("password", "")
        if password == APP_PASSWORD:
            _login_attempts.pop(ip, None)
            session["authenticated"] = True
            return redirect(url_for("main.index"))

        attempt["count"] += 1
        if attempt["count"] >= _MAX_ATTEMPTS:
            attempt["locked_until"] = now + _LOCKOUT_SECONDS
            attempt["count"] = 0
            _login_attempts[ip] = attempt
            return render_template("login.html", error=f"Zbyt wiele nieudanych prób. Konto zablokowane na {_LOCKOUT_SECONDS // 60} minut.")

        _login_attempts[ip] = attempt
        remaining_attempts = _MAX_ATTEMPTS - attempt["count"]
        error = f"Nieprawidłowe hasło. Pozostało prób: {remaining_attempts}."

    return render_template("login.html", error=error)


@main_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("main.login"))


@main_bp.route("/workspaces/switch", methods=["POST"])
def switch_workspace():
    workspace_id = request.form.get("workspace_id", type=int)
    if not workspace_id:
        return jsonify({"error": "Brak workspace_id"}), 400
    db = get_db()
    row = db.execute("SELECT id FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
    db.close()
    if not row:
        return jsonify({"error": "Workspace nie istnieje"}), 404
    session["workspace_id"] = workspace_id
    return redirect(request.referrer or url_for("main.tab_dashboard"))


@main_bp.route("/api/workspaces", methods=["GET"])
def api_workspaces():
    db = get_db()
    workspaces = db.execute("SELECT id, name, created_at FROM workspaces ORDER BY id").fetchall()
    db.close()
    return jsonify({"workspaces": [dict(w) for w in workspaces], "current": get_workspace_id()})


@main_bp.route("/api/workspaces", methods=["POST"])
def api_create_workspace():
    name = request.form.get("name", "").strip()
    if not name:
        return jsonify({"error": "Podaj nazwę workspace"}), 400
    db = get_db()
    try:
        cursor = db.execute("INSERT INTO workspaces (name) VALUES (?)", (name,))
        workspace_id = cursor.lastrowid
        db.commit()
    except Exception:
        db.close()
        return jsonify({"error": "Nazwa workspace już istnieje"}), 400
    db.close()
    return jsonify({"ok": True, "id": workspace_id, "name": name})


@main_bp.route("/api/workspaces/<int:workspace_id>/delete", methods=["POST"])
def api_delete_workspace(workspace_id):
    if workspace_id == 1:
        return jsonify({"error": "Nie można usunąć domyślnego workspace"}), 400
    db = get_db()
    db.execute("DELETE FROM workspaces WHERE id = ?", (workspace_id,))
    db.commit()
    db.close()
    if get_workspace_id() == workspace_id:
        session["workspace_id"] = 1
    return jsonify({"ok": True})


@main_bp.route("/")
def index():
    return redirect(url_for("main.tab_dashboard"))


@main_bp.route("/dashboard")
def tab_dashboard():
    return render_template("tabs/dashboard.html", active_tab="dashboard")


@main_bp.route("/api/dashboard-stats")
def api_dashboard_stats():
    """Return dashboard statistics: today, this month, total."""
    db = get_db()
    ws = get_workspace_id()

    stats = {}

    # Google API calls (estimated from scrape_areas: each area = ceil(results_count/20) pages, min 1)
    stats["api_calls_total"] = db.execute(
        "SELECT COALESCE(SUM(MAX(1, (results_count + 19) / 20)), 0) FROM scrape_areas WHERE workspace_id = ?", (ws,)
    ).fetchone()[0]
    stats["api_calls_month"] = db.execute(
        "SELECT COALESCE(SUM(MAX(1, (results_count + 19) / 20)), 0) FROM scrape_areas WHERE workspace_id = ? AND strftime('%Y-%m', created_at, 'localtime') = strftime('%Y-%m', 'now', 'localtime')", (ws,)
    ).fetchone()[0]
    stats["api_calls_today"] = db.execute(
        "SELECT COALESCE(SUM(MAX(1, (results_count + 19) / 20)), 0) FROM scrape_areas WHERE workspace_id = ? AND date(created_at, 'localtime') = date('now', 'localtime')", (ws,)
    ).fetchone()[0]

    # Businesses
    stats["businesses_total"] = db.execute(
        "SELECT COUNT(*) FROM businesses WHERE workspace_id = ?", (ws,)
    ).fetchone()[0]
    stats["businesses_month"] = db.execute(
        "SELECT COUNT(*) FROM businesses WHERE workspace_id = ? AND strftime('%Y-%m', created_at, 'localtime') = strftime('%Y-%m', 'now', 'localtime')", (ws,)
    ).fetchone()[0]
    stats["businesses_today"] = db.execute(
        "SELECT COUNT(*) FROM businesses WHERE workspace_id = ? AND date(created_at, 'localtime') = date('now', 'localtime')", (ws,)
    ).fetchone()[0]

    # Websites (businesses with non-empty website)
    stats["websites_total"] = db.execute(
        "SELECT COUNT(*) FROM businesses WHERE workspace_id = ? AND website IS NOT NULL AND website != ''", (ws,)
    ).fetchone()[0]
    stats["websites_month"] = db.execute(
        "SELECT COUNT(*) FROM businesses WHERE workspace_id = ? AND website IS NOT NULL AND website != '' AND strftime('%Y-%m', created_at, 'localtime') = strftime('%Y-%m', 'now', 'localtime')", (ws,)
    ).fetchone()[0]
    stats["websites_today"] = db.execute(
        "SELECT COUNT(*) FROM businesses WHERE workspace_id = ? AND website IS NOT NULL AND website != '' AND date(created_at, 'localtime') = date('now', 'localtime')", (ws,)
    ).fetchone()[0]

    # Emails scraped
    stats["emails_total"] = db.execute(
        "SELECT COUNT(*) FROM emails e JOIN businesses b ON e.business_id = b.id WHERE b.workspace_id = ?", (ws,)
    ).fetchone()[0]
    stats["emails_month"] = db.execute(
        "SELECT COUNT(*) FROM emails e JOIN businesses b ON e.business_id = b.id WHERE b.workspace_id = ? AND strftime('%Y-%m', e.created_at, 'localtime') = strftime('%Y-%m', 'now', 'localtime')", (ws,)
    ).fetchone()[0]
    stats["emails_today"] = db.execute(
        "SELECT COUNT(*) FROM emails e JOIN businesses b ON e.business_id = b.id WHERE b.workspace_id = ? AND date(e.created_at, 'localtime') = date('now', 'localtime')", (ws,)
    ).fetchone()[0]

    # Campaigns
    stats["campaigns_total"] = db.execute(
        "SELECT COUNT(*) FROM campaigns WHERE workspace_id = ?", (ws,)
    ).fetchone()[0]
    stats["campaigns_month"] = db.execute(
        "SELECT COUNT(*) FROM campaigns WHERE workspace_id = ? AND strftime('%Y-%m', created_at, 'localtime') = strftime('%Y-%m', 'now', 'localtime')", (ws,)
    ).fetchone()[0]
    stats["campaigns_today"] = db.execute(
        "SELECT COUNT(*) FROM campaigns WHERE workspace_id = ? AND date(created_at, 'localtime') = date('now', 'localtime')", (ws,)
    ).fetchone()[0]

    # Emails sent (campaign_emails with status='sent')
    stats["sent_total"] = db.execute(
        "SELECT COUNT(*) FROM campaign_emails ce JOIN campaigns c ON ce.campaign_id = c.id WHERE c.workspace_id = ? AND ce.status = 'sent'", (ws,)
    ).fetchone()[0]
    stats["sent_month"] = db.execute(
        "SELECT COUNT(*) FROM campaign_emails ce JOIN campaigns c ON ce.campaign_id = c.id WHERE c.workspace_id = ? AND ce.status = 'sent' AND strftime('%Y-%m', ce.sent_at, 'localtime') = strftime('%Y-%m', 'now', 'localtime')", (ws,)
    ).fetchone()[0]
    stats["sent_today"] = db.execute(
        "SELECT COUNT(*) FROM campaign_emails ce JOIN campaigns c ON ce.campaign_id = c.id WHERE c.workspace_id = ? AND ce.status = 'sent' AND date(ce.sent_at, 'localtime') = date('now', 'localtime')", (ws,)
    ).fetchone()[0]

    # Primary emails
    stats["primary_emails_total"] = db.execute(
        "SELECT COUNT(*) FROM emails e JOIN businesses b ON e.business_id = b.id WHERE b.workspace_id = ? AND e.is_primary = 1", (ws,)
    ).fetchone()[0]
    stats["primary_emails_month"] = db.execute(
        "SELECT COUNT(*) FROM emails e JOIN businesses b ON e.business_id = b.id WHERE b.workspace_id = ? AND e.is_primary = 1 AND strftime('%Y-%m', e.created_at, 'localtime') = strftime('%Y-%m', 'now', 'localtime')", (ws,)
    ).fetchone()[0]
    stats["primary_emails_today"] = db.execute(
        "SELECT COUNT(*) FROM emails e JOIN businesses b ON e.business_id = b.id WHERE b.workspace_id = ? AND e.is_primary = 1 AND date(e.created_at, 'localtime') = date('now', 'localtime')", (ws,)
    ).fetchone()[0]

    db.close()
    return jsonify(stats)


@main_bp.route("/google-maps")
def tab_google_maps():
    return render_template("tabs/google_maps.html", active_tab="google_maps")


# ──────────────────────────────────────────────────────────────────────────────
# Maps URL Scraper — single-place scrape from a Google Maps link → CSV/HubSpot
# ──────────────────────────────────────────────────────────────────────────────

def _expand_url(url):
    """Follow redirects and return the final URL (handles maps.app.goo.gl short links)."""
    import requests as req
    if 'maps.app.goo.gl' in url or 'goo.gl' in url:
        try:
            r = req.get(url, allow_redirects=True, timeout=10,
                        headers={"User-Agent": "Mozilla/5.0"})
            return r.url
        except Exception:
            pass
    return url


def _parse_maps_url(url):
    """Return (place_name, lat, lng) extracted from a Google Maps place URL."""
    # Name from path  /maps/place/<NAME>/
    m = re.search(r'/maps/place/([^/@]+)', url)
    place_name = urlparse.unquote_plus(m.group(1)) if m else ""

    # Precise coords from data parameter — take the LAST pair of !3d / !4d
    # (URLs with multiple places embed several pairs; the target place is last)
    lat_all = re.findall(r'!3d(-?\d+\.?\d*)', url)
    lng_all = re.findall(r'!4d(-?\d+\.?\d*)', url)
    if lat_all and lng_all:
        return place_name, float(lat_all[-1]), float(lng_all[-1])

    # Fallback: @lat,lng in path
    coords_m = re.search(r'@(-?\d+\.?\d*),(-?\d+\.?\d*)', url)
    if coords_m:
        return place_name, float(coords_m.group(1)), float(coords_m.group(2))

    return place_name, None, None


_PLACE_FIELD_MASK = (
    "places.id,places.displayName,places.formattedAddress,"
    "places.addressComponents,places.types,places.primaryTypeDisplayName,"
    "places.nationalPhoneNumber,places.internationalPhoneNumber,"
    "places.websiteUri,places.rating,places.userRatingCount,"
    "places.businessStatus,places.googleMapsUri,places.editorialSummary"
)


def _place_api_headers(api_key):
    return {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": _PLACE_FIELD_MASK,
    }


def _format_place(place):
    city = postal_code = country = street = ""
    for comp in place.get("addressComponents", []):
        types = comp.get("types", [])
        ltext = comp.get("longText", "")
        if "locality" in types:
            city = ltext
        elif "postal_code" in types:
            postal_code = ltext
        elif "country" in types:
            country = ltext
        elif "route" in types:
            street = ltext
    return {
        "name": place.get("displayName", {}).get("text", ""),
        "address": place.get("formattedAddress", ""),
        "street": street,
        "city": city,
        "postal_code": postal_code,
        "country": country,
        "phone": place.get("nationalPhoneNumber", ""),
        "phone_international": place.get("internationalPhoneNumber", ""),
        "website": place.get("websiteUri", ""),
        "category": place.get("primaryTypeDisplayName", {}).get("text", ""),
        "category_google": ", ".join(place.get("types", [])[:5]),
        "rating": str(place.get("rating", "")),
        "rating_count": str(place.get("userRatingCount", "")),
        "business_status": place.get("businessStatus", ""),
        "maps_url": place.get("googleMapsUri", ""),
        "place_id": place.get("id", ""),
        "description": place.get("editorialSummary", {}).get("text", ""),
    }


def _fetch_place_details(api_key, place_name, lat, lng):
    """Fetch place details using Google Places API (New).

    Strategy order:
    1. Text Search + tight location restriction (name + coords) — most precise
    2. Text Search + location bias 500 m (name + approximate area)
    3. Text Search without location (name only, global fallback)
    """
    import requests as req

    hdrs = _place_api_headers(api_key)
    text_url = "https://places.googleapis.com/v1/places:searchText"
    last_error = None

    # ── 1. Text Search + locationRestriction (hard boundary, 200 m) ─────────
    if lat is not None and lng is not None:
        body = {
            "textQuery": place_name,
            "pageSize": 1,
            "locationRestriction": {
                "circle": {
                    "center": {"latitude": lat, "longitude": lng},
                    "radius": 200.0,
                }
            },
        }
        r = req.post(text_url, json=body, headers=hdrs, timeout=15)
        d = r.json()
        if "error" in d:
            last_error = f"{d['error'].get('code')} {d['error'].get('message','')}"
        elif d.get("places"):
            return _format_place(d["places"][0])

    # ── 2. Text Search + locationBias (soft, 500 m) ─────────────────────────
    if lat is not None and lng is not None:
        body = {
            "textQuery": place_name,
            "pageSize": 1,
            "locationBias": {
                "circle": {
                    "center": {"latitude": lat, "longitude": lng},
                    "radius": 500.0,
                }
            },
        }
        r = req.post(text_url, json=body, headers=hdrs, timeout=15)
        d = r.json()
        if "error" in d:
            last_error = f"{d['error'].get('code')} {d['error'].get('message','')}"
        elif d.get("places"):
            return _format_place(d["places"][0])

    # ── 3. Text Search — no location ────────────────────────────────────────
    body = {"textQuery": place_name, "pageSize": 1}
    r = req.post(text_url, json=body, headers=hdrs, timeout=15)
    d = r.json()
    if "error" in d:
        err = d["error"]
        raise Exception(f"API error {err.get('code')}: {err.get('message', '')}")
    if d.get("places"):
        return _format_place(d["places"][0])

    coords_hint = f" ({lat}, {lng})" if lat else ""
    raise Exception(
        f"Nie znaleziono miejsca: '{place_name}'{coords_hint}"
        + (f" — ostatni błąd API: {last_error}" if last_error else "")
    )


@main_bp.route("/maps-url-scraper")
def tab_maps_url_scraper():
    return render_template("tabs/maps_url_scraper.html", active_tab="maps_url_scraper")


@main_bp.route("/api/maps-url-scrape", methods=["POST"])
def api_maps_url_scrape():
    """Scrape place details from one or more Google Maps URLs."""
    urls_raw = request.form.get("urls", "").strip()
    if not urls_raw:
        return jsonify({"error": "Brak URL"}), 400

    db = get_db()
    row = db.execute("SELECT value FROM settings WHERE key = 'api_key'").fetchone()
    db.close()
    if not row or not row["value"]:
        return jsonify({"error": "Brak klucza API Google. Ustaw go w Ustawieniach."}), 400

    api_key = decrypt(row["value"])
    urls = [u.strip() for u in urls_raw.splitlines() if u.strip()]

    results = []
    for url in urls:
        try:
            url = _expand_url(url)
            place_name, lat, lng = _parse_maps_url(url)
            if not place_name:
                raise ValueError("Nie można wyodrębnić nazwy miejsca z URL")
            data = _fetch_place_details(api_key, place_name, lat, lng)
            data["source_url"] = url
            results.append(data)
        except Exception as e:
            results.append({"error": str(e), "source_url": url})

    return jsonify({"results": results})


@main_bp.route("/api/maps-url-scrape/csv", methods=["POST"])
def api_maps_url_scrape_csv():
    """Return scraped places as a HubSpot-compatible CSV file."""
    data = request.get_json(force=True)
    results = data.get("results", [])

    fieldnames = [
        "Company name", "Website URL", "Phone number",
        "City", "Country/Region", "Zip Code", "Address",
        "Industry", "Description",
        "Google Maps URL", "Google Rating", "Google Rating Count",
        "Google Place ID",
    ]

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()

    for r in results:
        if "error" in r:
            continue
        writer.writerow({
            "Company name": r.get("name", ""),
            "Website URL": r.get("website", ""),
            "Phone number": r.get("phone_international") or r.get("phone", ""),
            "City": r.get("city", ""),
            "Country/Region": r.get("country", ""),
            "Zip Code": r.get("postal_code", ""),
            "Address": r.get("address", ""),
            "Industry": r.get("category", ""),
            "Description": r.get("description", ""),
            "Google Maps URL": r.get("maps_url", ""),
            "Google Rating": r.get("rating", ""),
            "Google Rating Count": r.get("rating_count", ""),
            "Google Place ID": r.get("place_id", ""),
        })

    csv_bytes = output.getvalue().encode("utf-8-sig")  # BOM for Excel
    response = make_response(csv_bytes)
    response.headers["Content-Type"] = "text/csv; charset=utf-8-sig"
    response.headers["Content-Disposition"] = 'attachment; filename="hubspot_import.csv"'
    return response


@main_bp.route("/api/businesses/export")
def api_businesses_export():
    """Export filtered businesses as HubSpot-compatible CSV."""
    db = get_db()
    ws = get_workspace_id()

    search = request.args.get("search", "").strip()
    category = request.args.get("category", "").strip()
    country = request.args.get("country", "").strip()
    city = request.args.get("city", "").strip()
    has_website = request.args.get("has_website", "").strip()
    ids_param = request.args.get("ids", "").strip()

    conditions = ["workspace_id = ?", "COALESCE(not_interesting, 0) = 0"]
    params = [ws]

    if ids_param:
        id_list = [int(x) for x in ids_param.split(",") if x.strip().isdigit()]
        if id_list:
            conditions.append(f"id IN ({','.join('?' * len(id_list))})")
            params.extend(id_list)
    else:
        if has_website == "1":
            conditions.append("(website IS NOT NULL AND website != '')")
        elif has_website == "0":
            conditions.append("(website IS NULL OR website = '')")
        if search:
            conditions.append("(name LIKE ? OR address LIKE ? OR phone LIKE ? OR website LIKE ?)")
            like = f"%{search}%"
            params.extend([like, like, like, like])
        if category:
            conditions.append("category = ?")
            params.append(category)
        if country:
            conditions.append("country = ?")
            params.append(country)
        if city:
            conditions.append("city = ?")
            params.append(city)

    where = "WHERE " + " AND ".join(conditions)
    rows = db.execute(
        f"SELECT * FROM businesses {where} ORDER BY created_at DESC",
        params,
    ).fetchall()
    db.close()

    fieldnames = [
        "Company name", "Website URL", "Phone number",
        "City", "Country/Region", "Address",
        "Industry", "Google Place ID",
    ]

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({
            "Company name": r["name"] or "",
            "Website URL": r["website"] or "",
            "Phone number": r["phone"] or "",
            "City": r["city"] or "",
            "Country/Region": r["country"] or "",
            "Address": r["address"] or "",
            "Industry": r["category_google"] or r["category"] or "",
            "Google Place ID": r["place_id"] or "",
        })

    csv_bytes = output.getvalue().encode("utf-8-sig")
    response = make_response(csv_bytes)
    response.headers["Content-Type"] = "text/csv; charset=utf-8-sig"
    response.headers["Content-Disposition"] = 'attachment; filename="hubspot_companies.csv"'
    return response


@main_bp.route("/api/businesses")
def api_businesses():
    """AJAX endpoint: list businesses with pagination, filtering, search."""
    db = get_db()
    ws = get_workspace_id()

    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 50, type=int)
    search = request.args.get("search", "").strip()
    source_query = request.args.get("source_query", "").strip()
    category = request.args.get("category", "").strip()
    country = request.args.get("country", "").strip()
    city = request.args.get("city", "").strip()
    has_website = request.args.get("has_website", "").strip()
    no_email = request.args.get("no_email", "").strip()
    not_scraped = request.args.get("not_scraped", "").strip()

    ALLOWED_SORT_COLS = {"id", "name", "address", "city", "country", "phone", "category", "category_google", "source_query", "created_at"}
    sort_col = request.args.get("sort_col", "created_at").strip()
    sort_dir = request.args.get("sort_dir", "desc").strip().lower()
    if sort_col not in ALLOWED_SORT_COLS:
        sort_col = "created_at"
    if sort_dir not in ("asc", "desc"):
        sort_dir = "desc"
    order_by = f"{sort_col} {sort_dir.upper()}"

    per_page = min(per_page, 1000)
    offset = (page - 1) * per_page

    show_not_interesting = request.args.get("show_not_interesting", "0").strip()

    conditions = ["workspace_id = ?"]
    params = [ws]
    if show_not_interesting != "1":
        conditions.append("COALESCE(not_interesting, 0) = 0")
    if has_website == "1":
        conditions.append("(website IS NOT NULL AND website != '')")
    elif has_website == "0":
        conditions.append("(website IS NULL OR website = '')")
    if no_email == "1":
        conditions.append("id NOT IN (SELECT DISTINCT business_id FROM emails WHERE business_id IS NOT NULL)")
    if not_scraped == "1":
        conditions.append("(email_scraped_at IS NULL OR COALESCE(email_scraped_website, '') != COALESCE(website, ''))")
        conditions.append("COALESCE(email_scrape_pending, 0) = 0")
    if search:
        conditions.append("(name LIKE ? OR address LIKE ? OR phone LIKE ? OR website LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like, like, like])
    if source_query:
        conditions.append("source_query = ?")
        params.append(source_query)
    if category:
        conditions.append("category = ?")
        params.append(category)
    if country:
        conditions.append("country = ?")
        params.append(country)
    if city:
        conditions.append("city = ?")
        params.append(city)

    where = ""
    if conditions:
        where = "WHERE " + " AND ".join(conditions)

    total = db.execute(f"SELECT COUNT(*) FROM businesses {where}", params).fetchone()[0]

    rows = db.execute(
        f"SELECT * FROM businesses {where} ORDER BY {order_by} LIMIT ? OFFSET ?",
        params + [per_page, offset],
    ).fetchall()

    businesses = [dict(r) for r in rows]

    # Get distinct source queries for filter dropdown
    queries = [r[0] for r in db.execute(
        "SELECT DISTINCT source_query FROM businesses WHERE workspace_id = ? AND source_query IS NOT NULL ORDER BY source_query", (ws,)
    ).fetchall()]

    # Get distinct categories for filter dropdown
    categories = [r[0] for r in db.execute(
        "SELECT DISTINCT category FROM businesses WHERE workspace_id = ? AND category IS NOT NULL AND category != '' ORDER BY category", (ws,)
    ).fetchall()]

    # Get distinct countries and cities for filter dropdowns
    countries = [r[0] for r in db.execute(
        "SELECT DISTINCT country FROM businesses WHERE workspace_id = ? AND country IS NOT NULL AND country != '' ORDER BY country", (ws,)
    ).fetchall()]

    cities = [r[0] for r in db.execute(
        "SELECT DISTINCT city FROM businesses WHERE workspace_id = ? AND city IS NOT NULL AND city != '' ORDER BY city", (ws,)
    ).fetchall()]

    db.close()

    return jsonify({
        "businesses": businesses,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page if per_page else 1,
        "source_queries": queries,
        "categories": categories,
        "countries": countries,
        "cities": cities,
    })


@main_bp.route("/api/businesses/bulk-action", methods=["POST"])
def api_businesses_bulk_action():
    """Bulk action on selected businesses."""
    db = get_db()
    ws = get_workspace_id()
    data = request.get_json(force=True)
    ids = data.get("ids", [])
    action = data.get("action", "")

    if not ids or not isinstance(ids, list):
        return jsonify({"error": "Brak ID"}), 400

    # Validate: only IDs belonging to this workspace
    placeholders = ",".join("?" * len(ids))
    valid = db.execute(
        f"SELECT id FROM businesses WHERE id IN ({placeholders}) AND workspace_id = ?",
        ids + [ws],
    ).fetchall()
    valid_ids = [r["id"] for r in valid]

    if not valid_ids:
        db.close()
        return jsonify({"error": "Brak pasujących rekordów"}), 404

    if action == "not_interesting":
        db.execute(
            f"UPDATE businesses SET not_interesting = 1 WHERE id IN ({','.join('?' * len(valid_ids))})",
            valid_ids,
        )
        db.commit()
        db.close()
        return jsonify({"ok": True, "updated": len(valid_ids)})

    db.close()
    return jsonify({"error": f"Nieznana akcja: {action}"}), 400


def _run_batch(queries, coords_sw, coords_ne, batch_op_id, workspace_id=1):
    """Run multiple scrape queries sequentially in a background thread."""
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "scrape_google_maps.py")
    total = len(queries)
    errors = []

    for i, query in enumerate(queries):
        # Update batch progress indicator
        db = get_db()
        db.execute(
            "UPDATE operations_log SET details = ? WHERE id = ?",
            (f"Zapytanie {i + 1}/{total}: {query}...", batch_op_id),
        )
        db.commit()
        db.close()

        # Create individual op entry for this query
        db = get_db()
        cursor = db.execute(
            "INSERT INTO operations_log (operation_type, status, details) VALUES ('google_maps_scrape', 'running', ?)",
            (f"Query: {query}",),
        )
        child_op_id = cursor.lastrowid
        db.commit()
        db.close()

        cmd = [sys.executable, script, "default", f"query={query}", f"op_id={child_op_id}"]
        if coords_sw and coords_ne:
            cmd += [f"coords_sw={coords_sw}", f"coords_ne={coords_ne}"]

        env = os.environ.copy()
        env["WORKSPACE_ID"] = str(workspace_id)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        _scrape_processes[child_op_id] = proc
        proc.wait()  # block until this query finishes before starting next
        _scrape_processes.pop(child_op_id, None)

        db = get_db()
        child_row = db.execute("SELECT status FROM operations_log WHERE id = ?", (child_op_id,)).fetchone()
        db.close()
        if child_row and child_row["status"] == "error":
            errors.append(query)

    # Mark batch op as finished
    db = get_db()
    if errors:
        summary = f"Zakończono {total} zapytań, błędy w: {', '.join(errors)}"
    else:
        summary = f"Zakończono {total} zapytań: {', '.join(queries)}"
    db.execute(
        "UPDATE operations_log SET status = 'done', details = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?",
        (summary, batch_op_id),
    )
    db.commit()
    db.close()


@main_bp.route("/google-maps/scrape", methods=["POST"])
def start_scrape():
    """Start Google Maps scrape as subprocess (supports comma-separated multi-query)."""
    query_raw = request.form.get("query", "").strip()
    queries = [q.strip() for q in query_raw.split(",") if q.strip()]
    coords_sw = request.form.get("coords_sw", "").strip()
    coords_ne = request.form.get("coords_ne", "").strip()

    if not queries:
        return jsonify({"error": "Podaj zapytanie"}), 400

    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "scrape_google_maps.py")
    workspace_id = get_workspace_id()

    if len(queries) == 1:
        # Single query — original behavior unchanged
        query = queries[0]
        db = get_db()
        cursor = db.execute(
            "INSERT INTO operations_log (operation_type, status, details) VALUES ('google_maps_scrape', 'running', ?)",
            (f"Query: {query}",),
        )
        op_id = cursor.lastrowid
        db.commit()
        db.close()

        cmd = [sys.executable, script, "default", f"query={query}", f"op_id={op_id}"]
        if coords_sw and coords_ne:
            cmd += [f"coords_sw={coords_sw}", f"coords_ne={coords_ne}"]
        env = os.environ.copy()
        env["WORKSPACE_ID"] = str(workspace_id)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        _scrape_processes[op_id] = proc

        return jsonify({"op_id": op_id, "status": "running"})

    else:
        # Multi-query batch — parent op tracks progress; each query runs sequentially
        db = get_db()
        cursor = db.execute(
            "INSERT INTO operations_log (operation_type, status, details) VALUES ('google_maps_batch', 'running', ?)",
            (f"0/{len(queries)}: {queries[0]}...",),
        )
        batch_op_id = cursor.lastrowid
        db.commit()
        db.close()

        threading.Thread(
            target=_run_batch,
            args=(queries, coords_sw, coords_ne, batch_op_id, workspace_id),
            daemon=True,
        ).start()

        return jsonify({"op_id": batch_op_id, "status": "running"})


@main_bp.route("/google-maps/scrape/status/<int:op_id>")
def scrape_status(op_id):
    """Check scrape operation status."""
    db = get_db()
    row = db.execute("SELECT * FROM operations_log WHERE id = ?", (op_id,)).fetchone()
    db.close()

    if not row:
        return jsonify({"error": "Operacja nie znaleziona"}), 404

    result = {"op_id": op_id, "status": row["status"], "details": row["details"]}

    # Check if subprocess has finished
    proc = _scrape_processes.get(op_id)
    if proc and proc.poll() is not None:
        _scrape_processes.pop(op_id, None)

    return jsonify(result)


@main_bp.route("/api/scrape-areas")
def api_scrape_areas():
    """Return all scrape areas for map display."""
    db = get_db()
    ws = get_workspace_id()
    rows = db.execute(
        "SELECT * FROM scrape_areas WHERE workspace_id = ? ORDER BY created_at DESC", (ws,)
    ).fetchall()
    db.close()
    areas = [dict(r) for r in rows]
    return jsonify({"areas": areas})


@main_bp.route("/api/maps-key")
def api_maps_key():
    """Return decrypted Google Maps API key for Maps JS API."""
    db = get_db()
    row = db.execute("SELECT value FROM settings WHERE key = 'api_key'").fetchone()
    db.close()
    if row and row["value"]:
        return jsonify({"key": decrypt(row["value"])})
    return jsonify({"key": ""})


@main_bp.route("/businesses")
def tab_businesses():
    return render_template("tabs/businesses.html", active_tab="businesses")


@main_bp.route("/api/scrape-tasks")
def api_scrape_tasks():
    """Return all scrape operations from operations_log with business counts."""
    db = get_db()
    ws = get_workspace_id()
    rows = db.execute(
        "SELECT * FROM operations_log WHERE operation_type = 'google_maps_scrape' ORDER BY started_at DESC"
    ).fetchall()
    tasks = []
    for row in rows:
        t = dict(row)
        source_query = _extract_source_query(t.get("details"))
        t["source_query"] = source_query
        if source_query:
            count = db.execute(
                "SELECT COUNT(*) FROM businesses WHERE source_query = ? AND workspace_id = ?",
                (source_query, ws),
            ).fetchone()[0]
        else:
            count = 0
        t["business_count"] = count
        tasks.append(t)
    db.close()
    return jsonify({"tasks": tasks})


@main_bp.route("/api/scrape-tasks/<int:op_id>/emails")
def api_scrape_task_emails(op_id):
    """Return emails collected in a specific scrape task (by source_query)."""
    db = get_db()
    row = db.execute("SELECT * FROM operations_log WHERE id = ?", (op_id,)).fetchone()
    if not row:
        db.close()
        return jsonify({"error": "Nie znaleziono zadania"}), 404

    source_query = _extract_source_query(row["details"])

    if not source_query:
        db.close()
        return jsonify({"emails": [], "source_query": None})

    ws = get_workspace_id()
    emails = db.execute(
        """SELECT e.id, e.email, e.source, e.created_at,
                  b.name AS business_name, b.website
           FROM emails e
           JOIN businesses b ON e.business_id = b.id
           WHERE b.source_query = ? AND b.workspace_id = ?
           ORDER BY e.created_at DESC""",
        (source_query, ws),
    ).fetchall()
    db.close()
    return jsonify({"emails": [dict(e) for e in emails], "source_query": source_query})


@main_bp.route("/email-scraping")
def tab_email_scraping():
    return render_template("tabs/email_scraping.html", active_tab="email_scraping")


# Track running email scrape processes {op_id: Popen}
_email_scrape_processes = {}


def _launch_email_scrape_subprocess(op_id, source_query, business_ids, country, city, max_pages, workspace_id=1):
    """Build command and launch email scrape subprocess. Updates _email_scrape_processes."""
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "scrape_emails.py")
    cmd = [sys.executable, script]
    # Pass string params via env vars to avoid [Errno 22] Invalid argument on Windows
    # caused by non-ASCII characters (Polish diacritics) in CLI arguments.
    env = os.environ.copy()
    env["SCRAPE_OP_ID"] = str(op_id)
    env["SCRAPE_MAX_PAGES"] = str(max_pages)
    env["SCRAPE_SOURCE_QUERY"] = source_query or ""
    env["SCRAPE_BUSINESS_IDS"] = business_ids or ""
    env["SCRAPE_COUNTRY"] = country or ""
    env["SCRAPE_CITY"] = city or ""
    env["WORKSPACE_ID"] = str(workspace_id)
    # Use DEVNULL for stdout/stderr — status is tracked via operations_log (DB polling).
    # PIPE would fill the 4-8 KB Windows pipe buffer and cause the subprocess to block/fail.
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    _email_scrape_processes[op_id] = proc


def _promote_next_email_scrape():
    """Promote the oldest queued email scrape to running and launch it."""
    db = get_db()
    row = db.execute(
        "SELECT * FROM operations_log WHERE operation_type = 'email_scrape' AND status = 'queued'"
        " ORDER BY started_at ASC LIMIT 1"
    ).fetchone()
    if not row:
        db.close()
        return

    op_id = row["id"]
    params = json.loads(row["params"] or "{}")
    source_query = params.get("source_query", "")
    business_ids = params.get("business_ids", "")
    country = params.get("country", "")
    city = params.get("city", "")
    workspace_id = params.get("workspace_id", 1)

    filters = []
    if source_query:
        filters.append(f"query: {source_query}")
    if country:
        filters.append(f"kraj: {country}")
    if city:
        filters.append(f"miasto: {city}")
    details_suffix = f" ({', '.join(filters)})" if filters else ""

    db.execute(
        "UPDATE operations_log SET status = 'running', details = ? WHERE id = ?",
        (f"Scraping emaili{details_suffix}", op_id),
    )
    db.commit()

    max_pages_row = db.execute("SELECT value FROM settings WHERE key = 'email_max_pages'").fetchone()
    max_pages = max_pages_row["value"] if max_pages_row else "10"
    db.close()

    _launch_email_scrape_subprocess(op_id, source_query, business_ids, country, city, max_pages, workspace_id)


@main_bp.route("/email-scraping/scrape", methods=["POST"])
def start_email_scrape():
    """Start email scraping as subprocess, or queue it if one is already running."""
    source_query = request.form.get("source_query", "").strip()
    business_ids = request.form.get("business_ids", "").strip()
    country = request.form.get("country", "").strip()
    city = request.form.get("city", "").strip()
    workspace_id = get_workspace_id()

    filters = []
    if source_query:
        filters.append(f"query: {source_query}")
    if country:
        filters.append(f"kraj: {country}")
    if city:
        filters.append(f"miasto: {city}")
    details_suffix = f" ({', '.join(filters)})" if filters else ""

    db = get_db()

    # Check if any email scrape is already running
    already_running = db.execute(
        "SELECT id FROM operations_log WHERE operation_type = 'email_scrape' AND status = 'running' LIMIT 1"
    ).fetchone()

    if already_running:
        # Queue this job — store params as JSON so we can launch it later
        params_json = json.dumps({
            "source_query": source_query,
            "business_ids": business_ids,
            "country": country,
            "city": city,
            "workspace_id": workspace_id,
        })
        cursor = db.execute(
            "INSERT INTO operations_log (operation_type, status, details, params) VALUES ('email_scrape', 'queued', ?, ?)",
            (f"W kolejce{details_suffix}", params_json),
        )
        op_id = cursor.lastrowid
        db.commit()
        db.close()
        return jsonify({"op_id": op_id, "status": "queued"})

    # No scrape running — start immediately
    cursor = db.execute(
        "INSERT INTO operations_log (operation_type, status, details) VALUES ('email_scrape', 'running', ?)",
        (f"Scraping emaili{details_suffix}",),
    )
    op_id = cursor.lastrowid
    db.commit()

    max_pages_row = db.execute("SELECT value FROM settings WHERE key = 'email_max_pages'").fetchone()
    max_pages = max_pages_row["value"] if max_pages_row else "10"
    db.close()

    _launch_email_scrape_subprocess(op_id, source_query, business_ids, country, city, max_pages, workspace_id)

    return jsonify({"op_id": op_id, "status": "running"})


@main_bp.route("/email-scraping/scrape/status/<int:op_id>")
def email_scrape_status(op_id):
    """Check email scrape operation status. Promotes next queued job when current finishes."""
    # Check ALL tracked processes for completion — not just this op_id.
    # This handles the case where the frontend is polling a queued job's status
    # while the previously running process finishes without its own status being polled.
    promoted = False
    for pid in list(_email_scrape_processes.keys()):
        proc = _email_scrape_processes.get(pid)
        if proc and proc.poll() is not None:
            _email_scrape_processes.pop(pid, None)
            if not promoted:
                _promote_next_email_scrape()
                promoted = True

    db = get_db()
    row = db.execute("SELECT * FROM operations_log WHERE id = ?", (op_id,)).fetchone()
    db.close()

    if not row:
        return jsonify({"error": "Operacja nie znaleziona"}), 404

    return jsonify({"op_id": op_id, "status": row["status"], "details": row["details"]})


@main_bp.route("/api/emails")
def api_emails():
    """AJAX endpoint: list emails with pagination, filtering, search."""
    db = get_db()
    ws = get_workspace_id()

    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 50, type=int)
    search = request.args.get("search", "").strip()
    source_query = request.args.get("source_query", "").strip()

    per_page = min(per_page, 200)
    offset = (page - 1) * per_page

    conditions = ["b.workspace_id = ?"]
    params = [ws]
    if search:
        conditions.append("(e.email LIKE ? OR b.name LIKE ? OR e.source LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like, like])
    if source_query:
        conditions.append("b.source_query = ?")
        params.append(source_query)

    where = "WHERE " + " AND ".join(conditions)

    total = db.execute(
        f"SELECT COUNT(*) FROM emails e LEFT JOIN businesses b ON e.business_id = b.id {where}", params
    ).fetchone()[0]

    rows = db.execute(
        f"""SELECT e.id, e.email, e.source, e.created_at, e.business_id,
                   e.is_primary, b.name AS business_name, b.source_query
            FROM emails e
            LEFT JOIN businesses b ON e.business_id = b.id
            {where}
            ORDER BY e.created_at DESC LIMIT ? OFFSET ?""",
        params + [per_page, offset],
    ).fetchall()

    emails = [dict(r) for r in rows]

    # Distinct source queries for filter
    queries = [r[0] for r in db.execute(
        "SELECT DISTINCT b.source_query FROM emails e JOIN businesses b ON e.business_id = b.id WHERE b.workspace_id = ? AND b.source_query IS NOT NULL ORDER BY b.source_query", (ws,)
    ).fetchall()]

    db.close()

    return jsonify({
        "emails": emails,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page if per_page else 1,
        "source_queries": queries,
    })


@main_bp.route("/api/emails/<int:email_id>/delete", methods=["POST"])
def delete_email(email_id):
    """Delete a single email."""
    db = get_db()
    db.execute("DELETE FROM emails WHERE id = ?", (email_id,))
    db.commit()
    db.close()
    return jsonify({"ok": True})


@main_bp.route("/api/emails/<int:email_id>/set-primary", methods=["POST"])
def set_primary_email(email_id):
    """Set this email as primary for its business, clearing any other primary."""
    db = get_db()
    row = db.execute("SELECT business_id, is_primary FROM emails WHERE id = ?", (email_id,)).fetchone()
    if not row:
        db.close()
        return jsonify({"error": "Nie znaleziono emaila"}), 404
    business_id = row["business_id"]
    # Toggle off if already primary
    if row["is_primary"]:
        db.execute("UPDATE emails SET is_primary = 0 WHERE id = ?", (email_id,))
    else:
        # Clear existing primary for this business, then set new one
        if business_id:
            db.execute("UPDATE emails SET is_primary = 0 WHERE business_id = ?", (business_id,))
        db.execute("UPDATE emails SET is_primary = 1 WHERE id = ?", (email_id,))
    db.commit()
    new_val = db.execute("SELECT is_primary FROM emails WHERE id = ?", (email_id,)).fetchone()["is_primary"]
    db.close()
    return jsonify({"ok": True, "is_primary": new_val})


@main_bp.route("/api/business-locations")
def api_business_locations():
    """Return distinct countries and cities from businesses table."""
    db = get_db()
    ws = get_workspace_id()
    country_filter = request.args.get("country", "").strip()

    countries = [r[0] for r in db.execute(
        "SELECT DISTINCT country FROM businesses WHERE workspace_id = ? AND country IS NOT NULL AND country != '' ORDER BY country", (ws,)
    ).fetchall()]

    city_q = "SELECT DISTINCT city FROM businesses WHERE workspace_id = ? AND city IS NOT NULL AND city != ''"
    city_params = [ws]
    if country_filter:
        city_q += " AND country = ?"
        city_params.append(country_filter)
    city_q += " ORDER BY city"
    cities = [r[0] for r in db.execute(city_q, city_params).fetchall()]

    db.close()
    return jsonify({"countries": countries, "cities": cities})


@main_bp.route("/api/email-scrape-tasks")
def api_email_scrape_tasks():
    """Return email scrape operations from operations_log."""
    # Auto-recover stuck queue: if no process is actually running but DB shows queued tasks,
    # promote the next one. Also clean up finished processes.
    for pid in list(_email_scrape_processes.keys()):
        proc = _email_scrape_processes.get(pid)
        if proc and proc.poll() is not None:
            _email_scrape_processes.pop(pid, None)

    if not _email_scrape_processes:
        db = get_db()
        still_running = db.execute(
            "SELECT id FROM operations_log WHERE operation_type = 'email_scrape' AND status = 'running' LIMIT 1"
        ).fetchone()
        db.close()
        if not still_running:
            _promote_next_email_scrape()

    db = get_db()
    rows = db.execute(
        "SELECT * FROM operations_log WHERE operation_type = 'email_scrape' ORDER BY started_at DESC"
    ).fetchall()
    db.close()
    return jsonify({"tasks": [dict(r) for r in rows]})


@main_bp.route("/api/email-scrape-tasks/<int:op_id>/cancel", methods=["POST"])
def cancel_email_scrape_task(op_id):
    """Cancel a running or queued email scrape task. If running, kills process and promotes next queued."""
    proc = _email_scrape_processes.pop(op_id, None)
    if proc:
        try:
            proc.terminate()
        except Exception:
            pass

    db = get_db()
    row = db.execute("SELECT status FROM operations_log WHERE id = ?", (op_id,)).fetchone()
    if not row:
        db.close()
        return jsonify({"error": "Nie znaleziono zadania"}), 404

    was_running = row["status"] == "running"
    db.execute(
        "UPDATE operations_log SET status = 'error', details = 'Anulowano', finished_at = datetime('now') WHERE id = ?",
        (op_id,),
    )
    db.commit()
    db.close()

    if was_running:
        _promote_next_email_scrape()

    return jsonify({"ok": True})


@main_bp.route("/campaigns")
def tab_campaigns():
    return render_template("tabs/campaigns.html", active_tab="campaigns")


@main_bp.route("/api/campaigns")
def api_campaigns():
    db = get_db()
    ws = get_workspace_id()
    campaigns = db.execute("SELECT * FROM campaigns WHERE workspace_id = ? ORDER BY created_at DESC", (ws,)).fetchall()
    campaigns_list = []
    for c in campaigns:
        d = dict(c)
        total = db.execute("SELECT COUNT(*) FROM campaign_emails WHERE campaign_id = ?", (c["id"],)).fetchone()[0]
        sent = db.execute("SELECT COUNT(*) FROM campaign_emails WHERE campaign_id = ? AND status = 'sent'", (c["id"],)).fetchone()[0]
        failed = db.execute("SELECT COUNT(*) FROM campaign_emails WHERE campaign_id = ? AND status = 'failed'", (c["id"],)).fetchone()[0]
        d["total"] = total
        d["sent"] = sent
        d["failed"] = failed
        campaigns_list.append(d)
    db.close()
    return jsonify({"campaigns": campaigns_list})


@main_bp.route("/api/campaign-estimate")
def api_campaign_estimate():
    """Return count of primary emails eligible for a campaign with given filters."""
    country = request.args.get("country", "").strip() or None
    city = request.args.get("city", "").strip() or None
    category = request.args.get("category", "").strip() or None
    ws = get_workspace_id()

    db = get_db()
    query = """SELECT COUNT(*) FROM emails e
               JOIN businesses b ON e.business_id = b.id
               WHERE b.workspace_id = ?
               AND e.is_primary = 1
               AND e.id NOT IN (
                   SELECT email_id FROM campaign_emails WHERE status IN ('pending', 'sending', 'sent', 'failed')
               )"""
    params = [ws]
    if country:
        query += " AND b.country = ?"
        params.append(country)
    if city:
        query += " AND b.city = ?"
        params.append(city)
    if category:
        query += " AND b.category = ?"
        params.append(category)
    count = db.execute(query, params).fetchone()[0]
    db.close()
    return jsonify({"count": count})


@main_bp.route("/campaigns/create", methods=["POST"])
def create_campaign():
    name = request.form.get("name", "").strip()
    subject = request.form.get("subject", "").strip()
    body = request.form.get("body", "").strip()
    target_city = request.form.get("target_city", "").strip() or None
    target_country = request.form.get("target_country", "").strip() or None
    target_category = request.form.get("target_category", "").strip() or None
    ws = get_workspace_id()

    if not name or not subject or not body:
        return jsonify({"error": "Wypełnij wszystkie pola"}), 400

    db = get_db()

    # If another campaign is already active (within this workspace), queue this one
    active = db.execute("SELECT id FROM campaigns WHERE workspace_id = ? AND status = 'active'", (ws,)).fetchone()
    status = "queued" if active else "active"

    cursor = db.execute(
        "INSERT INTO campaigns (name, subject, body_template, status, target_city, target_country, target_category, workspace_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (name, subject, body, status, target_city, target_country, target_category, ws),
    )
    campaign_id = cursor.lastrowid

    # Add only primary emails not yet successfully sent in any previous campaign
    # Scoped to current workspace
    query = """SELECT e.id FROM emails e
               JOIN businesses b ON e.business_id = b.id
               WHERE b.workspace_id = ?
               AND e.is_primary = 1
               AND e.id NOT IN (
                   SELECT email_id FROM campaign_emails WHERE status IN ('pending', 'sending', 'sent', 'failed')
               )"""
    params = [ws]
    if target_country:
        query += " AND b.country = ?"
        params.append(target_country)
    if target_city:
        query += " AND b.city = ?"
        params.append(target_city)
    if target_category:
        query += " AND b.category = ?"
        params.append(target_category)
    emails = db.execute(query, params).fetchall()
    for e in emails:
        db.execute(
            "INSERT INTO campaign_emails (campaign_id, email_id, status) VALUES (?, ?, 'pending')",
            (campaign_id, e["id"]),
        )

    db.commit()
    count = len(emails)
    db.close()

    return jsonify({"ok": True, "campaign_id": campaign_id, "emails_queued": count, "status": status})


@main_bp.route("/campaigns/<int:campaign_id>/stop", methods=["POST"])
def stop_campaign(campaign_id):
    db = get_db()
    db.execute("UPDATE campaigns SET status = 'stopped' WHERE id = ?", (campaign_id,))
    db.commit()
    db.close()
    return jsonify({"ok": True})


@main_bp.route("/campaigns/<int:campaign_id>/resume", methods=["POST"])
def resume_campaign(campaign_id):
    db = get_db()
    ws = get_workspace_id()
    # If another campaign is active (in same workspace), queue this one; otherwise activate
    active = db.execute(
        "SELECT id FROM campaigns WHERE workspace_id = ? AND status = 'active' AND id != ?", (ws, campaign_id)
    ).fetchone()
    new_status = "queued" if active else "active"
    db.execute("UPDATE campaigns SET status = ? WHERE id = ?", (new_status, campaign_id))
    db.commit()
    db.close()
    return jsonify({"ok": True, "status": new_status})


@main_bp.route("/campaigns/<int:campaign_id>/delete", methods=["POST"])
def delete_campaign(campaign_id):
    db = get_db()
    db.execute("DELETE FROM campaign_emails WHERE campaign_id = ?", (campaign_id,))
    db.execute("DELETE FROM campaigns WHERE id = ?", (campaign_id,))
    db.commit()
    db.close()
    return jsonify({"ok": True})



@main_bp.route("/sent")
def tab_sent():
    return render_template("tabs/sent.html", active_tab="sent")


@main_bp.route("/api/sent-emails")
def api_sent_emails():
    """AJAX endpoint: list all sent emails with campaign/mailbox/recipient info."""
    db = get_db()
    ws = get_workspace_id()

    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 50, type=int)
    search = request.args.get("search", "").strip()
    campaign_id = request.args.get("campaign_id", "").strip()
    mailbox_id = request.args.get("mailbox_id", "").strip()

    per_page = min(per_page, 200)
    offset = (page - 1) * per_page

    conditions = ["ce.status = 'sent'", "c.workspace_id = ?"]
    params = [ws]

    if search:
        conditions.append("(e.email LIKE ? OR b.name LIKE ? OR c.name LIKE ? OR c.subject LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like, like, like])
    if campaign_id:
        conditions.append("ce.campaign_id = ?")
        params.append(campaign_id)
    if mailbox_id:
        conditions.append("ce.mailbox_id = ?")
        params.append(mailbox_id)

    where = "WHERE " + " AND ".join(conditions)

    total = db.execute(
        f"""SELECT COUNT(*) FROM campaign_emails ce
            LEFT JOIN emails e ON ce.email_id = e.id
            LEFT JOIN businesses b ON e.business_id = b.id
            LEFT JOIN campaigns c ON ce.campaign_id = c.id
            LEFT JOIN mailboxes m ON ce.mailbox_id = m.id
            {where}""",
        params,
    ).fetchone()[0]

    rows = db.execute(
        f"""SELECT ce.id, ce.sent_at, ce.opened_at, ce.open_count,
                   e.email AS recipient_email,
                   b.name AS business_name,
                   c.id AS campaign_id, c.name AS campaign_name, c.subject, c.body_template,
                   m.email AS mailbox_email
            FROM campaign_emails ce
            LEFT JOIN emails e ON ce.email_id = e.id
            LEFT JOIN businesses b ON e.business_id = b.id
            LEFT JOIN campaigns c ON ce.campaign_id = c.id
            LEFT JOIN mailboxes m ON ce.mailbox_id = m.id
            {where}
            ORDER BY ce.sent_at DESC LIMIT ? OFFSET ?""",
        params + [per_page, offset],
    ).fetchall()

    campaigns_filter = db.execute(
        """SELECT DISTINCT c.id, c.name FROM campaign_emails ce
           JOIN campaigns c ON ce.campaign_id = c.id
           WHERE ce.status = 'sent' AND c.workspace_id = ? ORDER BY c.name""", (ws,)
    ).fetchall()

    mailboxes_filter = db.execute(
        """SELECT DISTINCT m.id, m.email FROM campaign_emails ce
           JOIN mailboxes m ON ce.mailbox_id = m.id
           JOIN campaigns c ON ce.campaign_id = c.id
           WHERE ce.status = 'sent' AND c.workspace_id = ? ORDER BY m.email""", (ws,)
    ).fetchall()

    db.close()

    return jsonify({
        "emails": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page if per_page else 1,
        "campaigns": [dict(c) for c in campaigns_filter],
        "mailboxes": [dict(m) for m in mailboxes_filter],
    })


@main_bp.route("/settings")
def tab_settings():
    db = get_db()
    ws = get_workspace_id()
    mailboxes = db.execute("SELECT * FROM mailboxes WHERE workspace_id = ? ORDER BY created_at DESC", (ws,)).fetchall()
    settings = {}
    for row in db.execute("SELECT key, value FROM settings").fetchall():
        settings[row["key"]] = row["value"]
    db.close()
    return render_template("tabs/settings.html", active_tab="settings", mailboxes=mailboxes, settings=settings)


@main_bp.route("/settings/api-key", methods=["POST"])
def save_api_key():
    key = request.form.get("api_key", "").strip()
    encrypted_key = encrypt(key)
    db = get_db()
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('api_key', ?)", (encrypted_key,))
    db.commit()
    db.close()
    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.content_type != "application/x-www-form-urlencoded":
        return jsonify({"ok": True})
    return redirect(url_for("main.tab_settings"))



@main_bp.route("/settings/email-scraping", methods=["POST"])
def save_email_scraping_settings():
    max_pages = request.form.get("email_max_pages", "10").strip()
    try:
        max_pages = str(max(1, min(100, int(max_pages))))
    except ValueError:
        max_pages = "10"
    db = get_db()
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('email_max_pages', ?)", (max_pages,))
    db.commit()
    db.close()
    return jsonify({"ok": True})


@main_bp.route("/settings/tracking-url", methods=["POST"])
def save_tracking_url():
    url = request.form.get("tracking_base_url", "").strip().rstrip("/")
    db = get_db()
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('tracking_base_url', ?)", (url,))
    db.commit()
    db.close()
    return jsonify({"ok": True})


@main_bp.route("/settings/daily-email-limit", methods=["POST"])
def save_daily_email_limit():
    limit = request.form.get("daily_email_limit", "0").strip()
    try:
        limit = str(max(0, int(limit)))
    except ValueError:
        limit = "0"
    db = get_db()
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('daily_email_limit', ?)", (limit,))
    db.commit()
    db.close()
    return jsonify({"ok": True})


@main_bp.route("/settings/mailbox", methods=["POST"])
def add_mailbox():
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "").strip()
    smtp_server = request.form.get("smtp_server", "smtp.purelymail.com").strip()
    smtp_port = int(request.form.get("smtp_port", 587))
    try:
        daily_limit = max(0, int(request.form.get("daily_limit", 0)))
    except ValueError:
        daily_limit = 0

    if email and password:
        db = get_db()
        ws = get_workspace_id()
        db.execute(
            "INSERT INTO mailboxes (email, password, smtp_server, smtp_port, daily_limit, workspace_id) VALUES (?, ?, ?, ?, ?, ?)",
            (email, encrypt(password), smtp_server, smtp_port, daily_limit, ws),
        )
        db.commit()
        db.close()
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest" or "multipart/form-data" in (request.content_type or "")
    if is_ajax:
        return jsonify({"ok": True})
    return redirect(url_for("main.tab_settings"))


@main_bp.route("/settings/mailbox/<int:mailbox_id>/limit", methods=["POST"])
def update_mailbox_limit(mailbox_id):
    try:
        daily_limit = max(0, int(request.form.get("daily_limit", 0)))
    except ValueError:
        daily_limit = 0
    db = get_db()
    db.execute("UPDATE mailboxes SET daily_limit = ? WHERE id = ?", (daily_limit, mailbox_id))
    db.commit()
    db.close()
    return jsonify({"ok": True})


@main_bp.route("/settings/mailbox/<int:mailbox_id>/delete", methods=["POST"])
def delete_mailbox(mailbox_id):
    db = get_db()
    db.execute("DELETE FROM mailboxes WHERE id = ?", (mailbox_id,))
    db.commit()
    db.close()
    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.content_type != "application/x-www-form-urlencoded":
        return jsonify({"ok": True})
    return redirect(url_for("main.tab_settings"))
