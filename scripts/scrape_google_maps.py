"""
Google Maps scraper — subprocess.

Flask mode (CLIENT_SLUG="default"):
  python scripts/scrape_google_maps.py default query=X op_id=Y coords_sw=... coords_ne=...
  Reads API key from DB settings, saves to businesses table.

VPS mode (CLIENT_SLUG != "default"):
  python3 scripts/scrape_google_maps.py <client_slug> api_key=X query=Y coords_sw=... coords_ne=...
  Saves results to /home/deploy/clients/<slug>/output/results_<query>.csv
"""

import sys
import os
import csv
import json
import time
from pathlib import Path

CLIENT_SLUG = sys.argv[1] if len(sys.argv) > 1 else "default"
params_kv   = dict(arg.split("=", 1) for arg in sys.argv[2:] if "=" in arg)
VPS_MODE    = CLIENT_SLUG != "default"
OUTPUT_DIR  = Path(f"/home/deploy/clients/{CLIENT_SLUG}/output")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

if not VPS_MODE:
    from config import DATABASE
    from app.crypto import decrypt
    import sqlite3

    def get_db():
        conn = sqlite3.connect(DATABASE, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.row_factory = sqlite3.Row
        return conn

    def get_api_key_from_db():
        db = get_db()
        row = db.execute("SELECT value FROM settings WHERE key = 'api_key'").fetchone()
        db.close()
        return decrypt(row["value"]) if row and row["value"] else None

    def log_operation(status, details, op_id=None):
        db = get_db()
        if op_id is None:
            cursor = db.execute(
                "INSERT INTO operations_log (operation_type, status, details) VALUES ('google_maps_scrape', ?, ?)",
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

    def update_op_details(op_id, details):
        db = get_db()
        db.execute("UPDATE operations_log SET details = ? WHERE id = ?", (details, op_id))
        db.commit()
        db.close()

    def save_business(db, biz, query, workspace_id=1):
        place_id = biz.get("place_id", "")
        if place_id:
            existing = db.execute("SELECT id FROM businesses WHERE place_id = ?", (place_id,)).fetchone()
        else:
            existing = db.execute(
                "SELECT id FROM businesses WHERE name = ? AND address = ?",
                (biz["name"], biz["address"]),
            ).fetchone()
        if existing:
            return False
        db.execute(
            "INSERT INTO businesses (name, address, city, country, phone, website, category, category_google, source_query, place_id, workspace_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (biz["name"], biz["address"], biz.get("city", ""), biz.get("country", ""),
             biz.get("phone", ""), biz.get("website", ""), query, biz.get("category_google", ""),
             query, place_id, workspace_id),
        )
        return True


def search_places(api_key, query, coords_sw=None, coords_ne=None):
    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": (
            "places.id,places.displayName,places.formattedAddress,"
            "places.addressComponents,places.types,"
            "places.nationalPhoneNumber,places.internationalPhoneNumber,"
            "places.websiteUri,places.googleMapsUri,"
            "places.location,places.rating,places.userRatingCount,"
            "places.businessStatus,places.regularOpeningHours,"
            "nextPageToken"
        ),
    }
    body = {"textQuery": query, "pageSize": 20}

    if coords_sw and coords_ne:
        sw = [float(x.strip()) for x in coords_sw.split(",")]
        ne = [float(x.strip()) for x in coords_ne.split(",")]
        body["locationRestriction"] = {
            "rectangle": {
                "low":  {"latitude": sw[0], "longitude": sw[1]},
                "high": {"latitude": ne[0], "longitude": ne[1]},
            }
        }

    results = []
    pages_fetched = 0
    while pages_fetched < 3:
        resp = requests.post(url, json=body, headers=headers, timeout=15)
        data = resp.json()
        pages_fetched += 1
        if "error" in data:
            err = data["error"]
            raise Exception(f"API error {err.get('code')}: {err.get('message', '')}")
        for place in data.get("places", []):
            city = country = ""
            admin3 = sublocality = postal_town = ""
            for comp in place.get("addressComponents", []):
                types = comp.get("types", [])
                if "locality" in types:
                    city = comp.get("longText", "")
                elif "postal_town" in types:
                    postal_town = comp.get("longText", "")
                elif "administrative_area_level_3" in types:
                    admin3 = comp.get("longText", "")
                elif "sublocality" in types or "sublocality_level_1" in types:
                    sublocality = comp.get("longText", "")
                elif "country" in types:
                    country = comp.get("longText", "")
            if not city:
                city = postal_town or admin3 or sublocality
            location = place.get("location", {})
            opening_hours = place.get("regularOpeningHours", {})
            weekday_descriptions = opening_hours.get("weekdayDescriptions", [])

            results.append({
                "name":            place.get("displayName", {}).get("text", ""),
                "address":         place.get("formattedAddress", ""),
                "city":            city,
                "country":         country,
                "category_google": ", ".join(place.get("types", [])),
                "place_id":        place.get("id", ""),
                "phone":           place.get("nationalPhoneNumber", ""),
                "phone_international": place.get("internationalPhoneNumber", ""),
                "website":         place.get("websiteUri", ""),
                "google_maps_url": place.get("googleMapsUri", ""),
                "latitude":        str(location.get("latitude", "")) if location.get("latitude") else "",
                "longitude":       str(location.get("longitude", "")) if location.get("longitude") else "",
                "rating":          str(place.get("rating", "")) if place.get("rating") else "",
                "review_count":    str(place.get("userRatingCount", "")) if place.get("userRatingCount") else "",
                "business_status": place.get("businessStatus", ""),
                "opening_hours":   "|".join(weekday_descriptions) if weekday_descriptions else "",
            })
        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break
        body["pageToken"] = next_page_token
        time.sleep(2)
    return results


MAX_SUBDIVISION_DEPTH = 3


def scrape_recursive(api_key, query, sw, ne, depth=0, stats=None,
                     db=None, op_id=None, workspace_id=1, all_places=None):
    """Recursively scrape area, subdividing when saturated (60 results)."""
    if stats is None:
        stats = {"found": 0, "saved": 0, "areas": 0, "subdivisions": 0, "depth_saturated": 0}
    if all_places is None:
        all_places = []

    coords_sw = f"{sw[0]},{sw[1]}"
    coords_ne = f"{ne[0]},{ne[1]}"
    places = search_places(api_key, query, coords_sw, coords_ne)

    if len(places) == 60 and depth < MAX_SUBDIVISION_DEPTH:
        stats["subdivisions"] += 1
        mid_lat = (sw[0] + ne[0]) / 2
        mid_lng = (sw[1] + ne[1]) / 2
        quadrants = [
            ([sw[0], sw[1]],   [mid_lat, mid_lng]),
            ([sw[0], mid_lng], [mid_lat, ne[1]]),
            ([mid_lat, sw[1]], [ne[0], mid_lng]),
            ([mid_lat, mid_lng], [ne[0], ne[1]]),
        ]
        if not VPS_MODE and op_id:
            update_op_details(op_id, f"Podzielono na {4 ** (depth + 1)} obszarow (depth={depth+1}), zapisano {stats['saved']} nowych (query: {query})")
        print(json.dumps({"status": "subdividing", "depth": depth, "query": query}), flush=True)
        for q_sw, q_ne in quadrants:
            scrape_recursive(api_key, query, q_sw, q_ne, depth + 1, stats,
                             db=db, op_id=op_id, workspace_id=workspace_id, all_places=all_places)
    else:
        if len(places) == 60 and depth >= MAX_SUBDIVISION_DEPTH:
            stats["depth_saturated"] += 1
        stats["found"] += len(places)
        stats["areas"] += 1

        if VPS_MODE:
            all_places.extend(places)
        else:
            for place in places:
                if save_business(db, place, query, workspace_id):
                    stats["saved"] += 1
            db.execute(
                "INSERT INTO scrape_areas (source_query, sw_lat, sw_lng, ne_lat, ne_lng, results_count, workspace_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (query, sw[0], sw[1], ne[0], ne[1], len(places), workspace_id),
            )
            db.commit()

        print(json.dumps({"status": "area_done", "depth": depth, "found": len(places)}), flush=True)

    return stats, all_places


def save_csv(places, query):
    """Write results to OUTPUT_DIR/results_<query>.csv and return filename."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    safe_query = "".join(c if c.isalnum() or c in "-_ " else "_" for c in query).strip().replace(" ", "_")
    filename = f"results_{safe_query}.csv"
    path = OUTPUT_DIR / filename
    fieldnames = ["name", "address", "city", "country", "phone", "phone_international", "website",
                  "category_google", "place_id", "google_maps_url", "latitude", "longitude",
                  "rating", "review_count", "business_status", "opening_hours"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(places)
    return filename


def main():
    query = params_kv.get("query", "").strip()
    if not query:
        print(json.dumps({"error": "Brakuje parametru 'query'"}))
        sys.exit(1)

    coords_sw   = params_kv.get("coords_sw") or params_kv.get("coords-sw") or None
    coords_ne   = params_kv.get("coords_ne") or params_kv.get("coords-ne") or None
    op_id_raw   = params_kv.get("op_id") or params_kv.get("op-id")
    op_id       = int(op_id_raw) if op_id_raw else None

    # API key: from params (VPS) or from DB (Flask)
    api_key = params_kv.get("api_key")
    if not api_key and not VPS_MODE:
        api_key = get_api_key_from_db()
    if not api_key:
        msg = "Brak klucza API."
        if not VPS_MODE and op_id:
            log_operation("error", msg, op_id)
        print(json.dumps({"error": msg}))
        sys.exit(1)

    if not VPS_MODE:
        workspace_id = int(os.environ.get("WORKSPACE_ID", "1"))
        op_id = op_id or log_operation("running", f"Query: {query}")

    try:
        print(json.dumps({"status": "searching", "query": query}), flush=True)

        if coords_sw and coords_ne:
            sw = [float(x.strip()) for x in coords_sw.split(",")]
            ne = [float(x.strip()) for x in coords_ne.split(",")]

            if VPS_MODE:
                stats, all_places = scrape_recursive(api_key, query, sw, ne)
                filename = save_csv(all_places, query)
                stats["saved"] = len(all_places)
            else:
                db = get_db()
                stats, _ = scrape_recursive(api_key, query, sw, ne,
                                            db=db, op_id=op_id, workspace_id=workspace_id)
                db.close()

            warning = (
                f" Obszar za duzy — {stats['depth_saturated']} podobszar(ow) nadal nasyconych."
                if stats["depth_saturated"] else ""
            )
            summary = (
                f"Znaleziono {stats['found']}, zapisano {stats['saved']} nowych"
                f"{', podzielono na ' + str(stats['areas']) + ' podobszarow' if stats['subdivisions'] else ''}"
                f" (query: {query}){warning}"
            )
        else:
            places = search_places(api_key, query, None, None)

            if VPS_MODE:
                filename = save_csv(places, query)
                summary = f"Znaleziono {len(places)}, zapisano do {filename} (query: {query})"
            else:
                db = get_db()
                workspace_id = int(os.environ.get("WORKSPACE_ID", "1"))
                saved = 0
                for i, place in enumerate(places):
                    if save_business(db, place, query, workspace_id):
                        saved += 1
                    print(json.dumps({"status": "progress", "current": i + 1, "total": len(places)}), flush=True)
                db.commit()
                db.close()
                summary = f"Znaleziono {len(places)}, zapisano {saved} nowych (query: {query})"

        if not VPS_MODE:
            log_operation("done", summary, op_id)
        print(json.dumps({"status": "done", "summary": summary}), flush=True)

    except Exception as e:
        if not VPS_MODE and op_id:
            log_operation("error", str(e), op_id)
        print(json.dumps({"error": str(e)}), flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
