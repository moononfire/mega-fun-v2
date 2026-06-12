"""
Google Maps scraper — subprocess.
Usage: python scripts/scrape_google_maps.py "<query>" [--coords-sw "lat,lng" --coords-ne "lat,lng"]

Uses Google Places API (New) with API key from DB settings.
Saves results to businesses table and logs to operations_log.
"""

import sys
import os
import json
import time
from pathlib import Path

# VPS Worker: client context
CLIENT_SLUG = sys.argv[1] if len(sys.argv) > 1 else "default"
BASE_DIR    = Path(f"/home/deploy/clients/{CLIENT_SLUG}")
INPUT_DIR   = BASE_DIR / "input"
OUTPUT_DIR  = BASE_DIR / "output"
params_kv   = dict(arg.split("=", 1) for arg in sys.argv[2:] if "=" in arg)

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from config import DATABASE
from app.crypto import decrypt
import sqlite3


def get_db():
    conn = sqlite3.connect(DATABASE, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    return conn


def get_api_key():
    db = get_db()
    row = db.execute("SELECT value FROM settings WHERE key = 'api_key'").fetchone()
    db.close()
    if row and row["value"]:
        return decrypt(row["value"])
    return None


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


def search_places(api_key, query, coords_sw=None, coords_ne=None):
    """Fetch places from Google Places API (New) — Text Search.

    Fetches all available pages (max 3 = 60 results).

    Args:
        coords_sw: optional "lat,lng" — south-west corner of bounding box
        coords_ne: optional "lat,lng" — north-east corner of bounding box
    """
    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "places.id,places.displayName,places.formattedAddress,places.addressComponents,places.types,"
                            "places.nationalPhoneNumber,places.websiteUri,nextPageToken",
    }

    body = {"textQuery": query, "pageSize": 20}

    if coords_sw and coords_ne:
        sw = [float(x.strip()) for x in coords_sw.split(",")]
        ne = [float(x.strip()) for x in coords_ne.split(",")]
        body["locationRestriction"] = {
            "rectangle": {
                "low": {"latitude": sw[0], "longitude": sw[1]},
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
            # Extract city and country from addressComponents
            city = ""
            country = ""
            for comp in place.get("addressComponents", []):
                types = comp.get("types", [])
                if "locality" in types:
                    city = comp.get("longText", "")
                elif "country" in types:
                    country = comp.get("longText", "")
            results.append({
                "name": place.get("displayName", {}).get("text", ""),
                "address": place.get("formattedAddress", ""),
                "city": city,
                "country": country,
                "category_google": ", ".join(place.get("types", [])),
                "place_id": place.get("id", ""),
                "phone": place.get("nationalPhoneNumber", ""),
                "website": place.get("websiteUri", ""),
            })

        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break

        body["pageToken"] = next_page_token
        time.sleep(2)

    return results


def save_business(db, biz, query, workspace_id=1):
    """Insert business, skip if place_id already exists."""
    place_id = biz.get("place_id", "")
    if place_id:
        existing = db.execute(
            "SELECT id FROM businesses WHERE place_id = ?",
            (place_id,),
        ).fetchone()
    else:
        existing = db.execute(
            "SELECT id FROM businesses WHERE name = ? AND address = ?",
            (biz["name"], biz["address"]),
        ).fetchone()
    if existing:
        return False
    db.execute(
        "INSERT INTO businesses (name, address, city, country, phone, website, category, category_google, source_query, place_id, workspace_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (biz["name"], biz["address"], biz.get("city", ""), biz.get("country", ""), biz.get("phone", ""), biz.get("website", ""), query, biz.get("category_google", ""), query, place_id, workspace_id),
    )
    return True


MAX_SUBDIVISION_DEPTH = 3  # max recursion depth (max 64 sub-areas)


def update_op_details(op_id, details):
    """Update operation details text without changing status/finished_at."""
    db = get_db()
    db.execute("UPDATE operations_log SET details = ? WHERE id = ?", (details, op_id))
    db.commit()
    db.close()


def scrape_recursive(api_key, query, sw, ne, db, op_id, depth=0, stats=None, workspace_id=1):
    """Scrape an area; if 60 results returned, subdivide into 4 quadrants and recurse."""
    if stats is None:
        stats = {"found": 0, "saved": 0, "areas": 0, "subdivisions": 0, "depth_saturated": 0}

    coords_sw = f"{sw[0]},{sw[1]}"
    coords_ne = f"{ne[0]},{ne[1]}"

    places = search_places(api_key, query, coords_sw, coords_ne)

    if len(places) == 60 and depth < MAX_SUBDIVISION_DEPTH:
        # Area saturated — divide into 4 quadrants
        stats["subdivisions"] += 1
        mid_lat = (sw[0] + ne[0]) / 2
        mid_lng = (sw[1] + ne[1]) / 2

        quadrants = [
            ([sw[0], sw[1]],       [mid_lat, mid_lng]),   # SW
            ([sw[0], mid_lng],     [mid_lat, ne[1]]),      # SE
            ([mid_lat, sw[1]],     [ne[0], mid_lng]),      # NW
            ([mid_lat, mid_lng],   [ne[0], ne[1]]),        # NE
        ]

        update_op_details(op_id, f"Podzielono na {4 ** (depth + 1)} obszarow (depth={depth+1}), zapisano {stats['saved']} nowych (query: {query})")
        print(json.dumps({"status": "subdividing", "depth": depth, "query": query}), flush=True)

        for q_sw, q_ne in quadrants:
            scrape_recursive(api_key, query, q_sw, q_ne, db, op_id, depth + 1, stats, workspace_id)
    else:
        # Area is small enough (or hit depth limit) — save results
        if len(places) == 60 and depth >= MAX_SUBDIVISION_DEPTH:
            stats["depth_saturated"] += 1
        for place in places:
            if save_business(db, place, query, workspace_id):
                stats["saved"] += 1
        stats["found"] += len(places)
        stats["areas"] += 1

        db.execute(
            "INSERT INTO scrape_areas (source_query, sw_lat, sw_lng, ne_lat, ne_lng, results_count, workspace_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (query, sw[0], sw[1], ne[0], ne[1], len(places), workspace_id),
        )
        db.commit()

        print(json.dumps({"status": "area_done", "depth": depth, "found": len(places)}), flush=True)

    return stats


def main():
    # Params passed by VPS runner as key=value pairs (sys.argv[2:] after client_slug)
    query = params_kv.get("query", "").strip()
    if not query:
        print(json.dumps({"error": "Brakuje parametru 'query'"}))
        sys.exit(1)
    coords_sw = params_kv.get("coords_sw") or params_kv.get("coords-sw") or None
    coords_ne = params_kv.get("coords_ne") or params_kv.get("coords-ne") or None
    op_id_raw = params_kv.get("op_id") or params_kv.get("op-id")
    op_id = int(op_id_raw) if op_id_raw else None

    api_key = get_api_key()
    if not api_key:
        msg = "Brak klucza API. Ustaw go w Ustawieniach."
        if op_id:
            log_operation("error", msg, op_id)
        print(json.dumps({"error": msg}))
        sys.exit(1)

    workspace_id = int(os.environ.get("WORKSPACE_ID", "1"))
    op_id = op_id or log_operation("running", f"Query: {query}")

    try:
        print(json.dumps({"status": "searching", "query": query}), flush=True)

        if coords_sw and coords_ne:
            # Recursive subdivision mode
            sw = [float(x.strip()) for x in coords_sw.split(",")]
            ne = [float(x.strip()) for x in coords_ne.split(",")]
            db = get_db()
            stats = scrape_recursive(api_key, query, sw, ne, db, op_id, workspace_id=workspace_id)
            db.close()

            warning = (
                f" ⚠️ Obszar za duzy — {stats['depth_saturated']} podobszar(ow) nadal nasyconych."
                f" Zaznacz mniejszy prostokat aby uzyskac pelne wyniki."
                if stats["depth_saturated"] else ""
            )
            summary = (
                f"Znaleziono {stats['found']}, zapisano {stats['saved']} nowych"
                f"{', podzielono na ' + str(stats['areas']) + ' podobszarow' if stats['subdivisions'] else ''}"
                f" (query: {query}){warning}"
            )
        else:
            # No bounding box — flat scrape (no subdivision possible)
            places = search_places(api_key, query, None, None)
            db = get_db()
            saved = 0
            for i, place in enumerate(places):
                if save_business(db, place, query, workspace_id):
                    saved += 1
                print(json.dumps({"status": "progress", "current": i + 1, "total": len(places)}), flush=True)
            db.commit()
            db.close()
            summary = f"Znaleziono {len(places)}, zapisano {saved} nowych (query: {query})"

        log_operation("done", summary, op_id)
        print(json.dumps({"status": "done", "summary": summary}), flush=True)

    except Exception as e:
        log_operation("error", str(e), op_id)
        print(json.dumps({"error": str(e)}), flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
