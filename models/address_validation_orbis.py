import os
import json
import time
import logging
import requests
from datetime import datetime, timezone
from openai import AzureOpenAI
from models.base_models import AddressZoneMaster, StepStatus, GeocodeSource, AddressZoneMasterOrbis
from models.llm_analysis import get_db_connection_orbis
import psycopg2.extras

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────
GOOGLE_API_KEY        = os.getenv("GOOGLE_API_KEY")
AZURE_ENDPOINT        = os.getenv("OPENAI__AZURE_ENDPOINT")
API_KEY               = os.getenv("OPENAI__API_KEY")
MODEL_DEPLOYMENT_NAME = os.getenv("OPENAI__MODEL_DEPLOYMENT_NAME", "gpt-5.1")
RADIUS_SMALL         = 100
RADIUS_LARGE         = 300
MIN_PLACES_THRESHOLD = 10
LAT_LNG_DELTA        = 0.0009   # ~100m

import httpx

# --------------------------------------------------
# Custom HTTPX client (corporate / proxy safe)
# --------------------------------------------------
http_client = httpx.Client(
    verify=False,              # ⚠️ Disable SSL verification (corporate proxy)
    timeout=60.0,
    limits=httpx.Limits(
        max_keepalive_connections=1,
        max_connections=2
    )
)

# Azure OpenAI — was a plain OpenAI() client reading OPENAI_API_KEY, with a
# separate, unused AzureOpenAI "client1" sitting dead alongside it. This is
# now the one and only client, and it's already Azure.
client = AzureOpenAI(
    azure_endpoint=AZURE_ENDPOINT,
    api_key=API_KEY,
    api_version="2024-07-01-preview",
    http_client=http_client,
)


import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ─────────────────────────────────────────
# STEP 1: GEOCODE
# ─────────────────────────────────────────
def geocode_address(address: str) -> dict:
    url    = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"address": address, "key": GOOGLE_API_KEY}
    return requests.get(url, params=params, verify=False).json()


# ─────────────────────────────────────────
# STEP 2: NEARBY PLACES
# ────────────────────────────────────��────
def fetch_nearby_places(lat: float, lng: float, radius: int = RADIUS_SMALL) -> list:
    url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
    params = {
        "location": f"{lat},{lng}",
        "radius": radius,
        "key": GOOGLE_API_KEY,
    }

    all_places = []
    for _ in range(3):
        response = requests.get(url, params=params, verify=False).json()
        status = response.get("status")
        if status not in ("OK", "ZERO_RESULTS"):
            break

        all_places.extend(response.get("results", []))
        token = response.get("next_page_token")
        if not token:
            break

        time.sleep(2)
        params = {"pagetoken": token, "key": GOOGLE_API_KEY}

    return all_places

def fetch_places_adaptive(lat: float, lng: float) -> tuple[list, int]:
    """
    Fetch places at 200m first.
    If fewer than MIN_PLACES_THRESHOLD found, expand to 500m.
    Max 2 API calls total.
    """
    places = fetch_nearby_places(lat, lng, radius=RADIUS_SMALL)
    if len(places) >= MIN_PLACES_THRESHOLD:
        print(f"      ✔ {len(places)} places @ {RADIUS_SMALL}m — sufficient")
        return places, RADIUS_SMALL

    print(f"      → Only {len(places)} places @ {RADIUS_SMALL}m — expanding to {RADIUS_LARGE}m")
    places = fetch_nearby_places(lat, lng, radius=RADIUS_LARGE)
    print(f"      ✔ {len(places)} places @ {RADIUS_LARGE}m")
    return places, RADIUS_LARGE


# ─────────────────────────────────────────
# STEP 3: LLM
# ─────────────────────────────────────────
def classify_zone(address: str, lat: float, lng: float, places: list) -> dict:
    if places:
        simplified_places = [
            {"name": p.get("name"), "types": p.get("types", [])}
            for p in places
        ]
        context = f"""
    Coordinates   : lat={lat}, lng={lng}
    Nearby Places : {json.dumps(simplified_places, indent=2)}
        """
        print(f"      → Mode: lat/lng + places")
    else:
        context = f"""
    Coordinates   : lat={lat}, lng={lng}
    Nearby Places : Not available
        """
        print(f"      → Mode: lat/lng only")

    prompt = f"""
    You are an Indian urban zoning expert.

    Classify the zone as:
    - "Residential"  → primarily housing/living area
    - "Commercial"   → primarily shops/offices/businesses
    - "Mixed"        → both residential and commercial
    - "Industrial"   → factories/warehouses
    - "Unknown"      → cannot determine

    Address : {address}
    {context}

    Respond ONLY in this JSON format:
    {{
        "zone": "Residential | Commercial | Mixed | Industrial | Unknown",
        "confidence": <0-100>,
        "reason": "one line explanation"
    }}
    """
    response = client.chat.completions.create(
        model=MODEL_DEPLOYMENT_NAME,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    return json.loads(response.choices[0].message.content)


# ─────────────────────────────────────────
# DB HELPERS
# ─────────────────────────────────────────
def db_insert(record: AddressZoneMasterOrbis) -> str | None:
    """Insert record. Returns geo_id on success. Skips silently on duplicate."""
    conn = get_db_connection_orbis()
    cur  = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO public.address_zone_master
                (geo_id, name, address, bvd_id,
                 lat, lng,
                 geocode_status, places_status, llm_status,
                 created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (geo_id) DO NOTHING
            RETURNING geo_id
        """, (
            record.geo_id,
            record.name, record.address,
            record.bvd_id,
            record.lat, record.lng,
            record.geocode_status, record.places_status, record.llm_status,
            datetime.now(timezone.utc), datetime.now(timezone.utc),
        ))
        row = cur.fetchone()
        conn.commit()
        # ON CONFLICT DO NOTHING → returns nothing if geo_id already exists
        return row[0] if row else record.geo_id
    except Exception as e:
        conn.rollback()
        logger.error(f"db_insert: {e}")
        return None
    finally:
        cur.close()
        conn.close()


def db_update(geo_id: str, fields: dict):
    conn = get_db_connection_orbis()
    cur = conn.cursor()
    try:
        fields["updated_at"] = datetime.now(timezone.utc)

        set_clause = ", ".join([f"{k} = %s" for k in fields.keys()])
        values = list(fields.values()) + [geo_id]

        cur.execute(
            f"""
            UPDATE public.address_zone_master
            SET {set_clause}
            WHERE geo_id = %s
            """,
            values,
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"db_update failed: {e}", exc_info=True)
    finally:
        cur.close()
        conn.close()


def distance_sq(lat1, lng1, lat2, lng2):
    return (lat1 - lat2) ** 2 + (lng1 - lng2) ** 2

def normalize_places(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return []
    return []

def db_find_cached(bvd_id: str, lat: float, lng: float) -> dict | None:
    conn = get_db_connection_orbis()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        cur.execute("""
            SELECT
                geo_id,
                lat       AS lat,
                lng       AS lng,
                places,
                zone,
                confidence,
                reason,
                geocode_status,
                places_status,
                llm_status,
                updated_at
            FROM public.address_zone_master
            WHERE bvd_id = %s
              AND lat IS NOT NULL
              AND lng IS NOT NULL
              AND geocode_status = 'passed'
            ORDER BY updated_at DESC
        """, (bvd_id,))

        rows = cur.fetchall()
        if not rows:
            print(f"      → No cache found for bvd_id={bvd_id}")
            return None

        best_match = None
        best_dist  = None

        print(f"      → {len(rows)} cached rows found")

        for row in rows:
            db_lat = row["lat"]
            db_lng = row["lng"]

            if not is_valid_lat_lng(db_lat, db_lng):
                print(f"      ⚠ Skipping invalid lat/lng for geo_id={row['geo_id']}")
                continue

            lat_diff = abs(db_lat - lat)
            lng_diff = abs(db_lng - lng)

            print(
                f"      → geo_id={row['geo_id']} "
                f"Δlat={round(lat_diff,6)} Δlng={round(lng_diff,6)}"
            )

            if lat_diff <= LAT_LNG_DELTA and lng_diff <= LAT_LNG_DELTA:
                dist = distance_sq(db_lat, db_lng, lat, lng)

                if best_dist is None or dist < best_dist:
                    best_dist  = dist
                    best_match = row

        if not best_match:
            print(
                f"      → No cache within ~100m for bvd_id={bvd_id} "
                f"at ({lat}, {lng})"
            )
            return None

        print(
            f"      ✔ Cache match — geo_id={best_match['geo_id']} "
            f"geocode={best_match['geocode_status']} "
            f"places={best_match['places_status']} "
            f"llm={best_match['llm_status']}"
        )

        return {
            "geo_id": best_match["geo_id"],
            "lat": best_match["lat"],
            "lng": best_match["lng"],
            "places": normalize_places(best_match["places"]),
            "zone": best_match["zone"],
            "confidence": best_match["confidence"],
            "reason": best_match["reason"],
            "geocode_status": best_match["geocode_status"],
            "places_status": best_match["places_status"],
            "llm_status": best_match["llm_status"],
        }

    except Exception as e:
        logger.error(f"db_find_cached: {e}", exc_info=True)
        return None

    finally:
        cur.close()
        conn.close()
# ─────────────────────────────────────────
# CACHE HANDLER
# ─────────────────────────────────────────
def is_valid_lat_lng(lat, lng) -> bool:
    try:
        lat = float(lat)
        lng = float(lng)
        return -90 <= lat <= 90 and -180 <= lng <= 180
    except (TypeError, ValueError):
        return False

async def handle_cached(cached: dict, address: str) -> dict:
    geo_id = cached["geo_id"]
    lat    = cached["lat"]
    lng    = cached["lng"]
    places = cached["places"]

    # ── CASE 1: llm passed → return cached directly
    if cached["llm_status"] == StepStatus.passed.value:
        print(f"      ✔ Cache hit — llm passed — returning cached result")
        return {
            "status": 200,
            "data": {
                "address":            address,
                "coordinates":        {"lat": lat, "lng": lng},
                "total_places_found": len(places),
                "zone_result": {
                    "zone":       cached["zone"],
                    "confidence": cached["confidence"],
                    "reason":     cached["reason"],
                },
                "source": "cache",
            },
        }

    # ── CASE 2: places passed, llm failed → re-run LLM only
    if cached["places_status"] == StepStatus.passed.value:
        print(f"      → Cache: places passed — re-running LLM only (DB lat/lng + DB places)")
        try:
            zone_result = classify_zone(address, lat, lng, places)
            db_update(geo_id, {
                "zone":       zone_result.get("zone"),
                "confidence": zone_result.get("confidence"),
                "reason":     zone_result.get("reason"),
                "llm_status": StepStatus.passed.value,
            })
            print(f"      ✔ Zone: {zone_result['zone']} | Confidence: {zone_result['confidence']}%")
            return {
                "status": 200,
                "data": {
                    "address":            address,
                    "coordinates":        {"lat": lat, "lng": lng},
                    "total_places_found": len(places),
                    "zone_result":        zone_result,
                    "source":             "cache_rerun_llm",
                },
            }
        except Exception as e:
            logger.error(f"Cache LLM re-run error: {e}")
            db_update(geo_id, {"llm_status": StepStatus.failed.value})
            return {"status": 500, "message": "LLM re-run failed"}

    # ── CASE 3: places failed/not_called → re-run places + LLM
    print(f"      → Cache: places failed/not_called — re-running places + LLM (DB lat/lng)")
    try:
        places, radius_used = fetch_places_adaptive(lat, lng)
        print(f"      ✔ {len(places)} places @ {radius_used}m")
        simplified_places = [
            {"name": p.get("name"), "types": p.get("types", [])}
            for p in places
        ]
        db_update(geo_id, {
            "places":        json.dumps(simplified_places),
            "places_status": StepStatus.passed.value,
        })
    except Exception as e:
        logger.error(f"Cache places re-run error: {e}")
        print(f"      ✘ Places re-run failed — proceeding with lat/lng only")
        db_update(geo_id, {"places_status": StepStatus.failed.value})
        places = []

    try:
        zone_result = classify_zone(address, lat, lng, places)
        db_update(geo_id, {
            "zone":       zone_result.get("zone"),
            "confidence": zone_result.get("confidence"),
            "reason":     zone_result.get("reason"),
            "llm_status": StepStatus.passed.value,
        })
        print(f"      ✔ Zone: {zone_result['zone']} | Confidence: {zone_result['confidence']}%")
        return {
            "status": 200,
            "data": {
                "address":            address,
                "coordinates":        {"lat": lat, "lng": lng},
                "total_places_found": len(places),
                "zone_result":        zone_result,
                "source":             "cache_rerun_places_llm",
            },
        }
    except Exception as e:
        logger.error(f"Cache LLM re-run error: {e}")
        db_update(geo_id, {"llm_status": StepStatus.failed.value})
        return {"status": 500, "message": "LLM re-run failed"}


# ─────────────────────────────────────────
# MAIN FUNCTION
# ─────────────────────────────────────────
async def get_zone(
    name: str, address: str,
    bvd_id:str,
) -> dict:

    # ──────────────────────────────────────
    # STEP 1: GEOCODE
    # ──────────────────────────────────────
    print(f"[1/3] Geocoding: {address}")
    try:
        geocode_data = geocode_address(address)
        if geocode_data.get("status") != "OK":
            raise ValueError(f"Google status: {geocode_data.get('status')}")

        location = geocode_data["results"][0]["geometry"]["location"]
        lat, lng = location["lat"], location["lng"]
        geo_id   = f"{bvd_id}_{round(lat, 6)}_{round(lng, 6)}"
        print(f"      ✔ ({lat}, {lng}) → geo_id={geo_id}")

    except Exception as e:
        logger.error(f"Geocoding error: {e}")
        print(f"      ✘ {e} — exiting")
        record = AddressZoneMasterOrbis(
            geo_id=f"failed_{bvd_id}_{datetime.now(timezone.utc).timestamp()}",
            name=name, address=address,
            lat=None, lng=None,
            bvd_id=bvd_id,
            geocode_status=StepStatus.failed,
            places_status=StepStatus.failed,
            llm_status=StepStatus.failed,
        )
        db_insert(record)
        return {"status": 404, "message": "Geocoding failed. Exiting."}

    # ──────────────────────────────────────
    # CACHE CHECK
    # ──────────────────────────────────────
    print(f"      → Checking cache for bvd_id={bvd_id} near ({lat}, {lng})")
    cached = db_find_cached(bvd_id, lat, lng)

    if cached:
        return await handle_cached(cached, address)

    # ──────────────────────────────────────
    # FRESH RUN — insert initial row
    # ──────────────────────────────────────
    print(f"      → No cache — fresh run")
    record = AddressZoneMasterOrbis(
        geo_id=geo_id,
        name=name, address=address,
        lat=lat, lng=lng,
        bvd_id=bvd_id,
        geocode_status=StepStatus.passed,
        places_status=StepStatus.not_called,
        llm_status=StepStatus.not_called,
    )
    geo_id = db_insert(record)

    if not geo_id:
        logger.error("db_insert returned None — cannot proceed")
        return {"status": 500, "message": "DB insert failed"}

    # ──────────────────────────────────────
    # STEP 2: NEARBY PLACES — adaptive radius
    # ──────────────────────────────────────
    places      = []
    radius_used = RADIUS_SMALL
    print(f"[2/3] Fetching nearby places (adaptive radius)...")
    try:
        places, radius_used = fetch_places_adaptive(lat, lng)
        print(f"      ✔ {len(places)} places found @ {radius_used}m")
        simplified_places = [
            {"name": p.get("name"), "types": p.get("types", [])}
            for p in places
        ]
        db_update(geo_id, {
            "places":        json.dumps(simplified_places),
            "places_status": StepStatus.passed.value,
        })
    except Exception as e:
        logger.error(f"Places error: {e}")
        print(f"      ✘ {e} — proceeding with lat/lng only")
        db_update(geo_id, {"places_status": StepStatus.failed.value})

    # ──────────────────────────────────────
    # STEP 3: LLM
    # ──────────────────────────────────────
    print(f"[3/3] Classifying with LLM...")
    try:
        zone_result = classify_zone(address, lat, lng, places)
        print(f"      ✔ Zone: {zone_result['zone']} | Confidence: {zone_result['confidence']}%")
        db_update(geo_id, {
            "zone":       zone_result.get("zone"),
            "confidence": zone_result.get("confidence"),
            "reason":     zone_result.get("reason"),
            "llm_status": StepStatus.passed.value,
        })
    except Exception as e:
        logger.error(f"LLM error: {e}")
        print(f"      ✘ {e}")
        db_update(geo_id, {"llm_status": StepStatus.failed.value})
        return {"status": 500, "message": "LLM classification failed"}

    return {
        "status": 200,
        "data": {
            "name":               name,
            "address":            address,
            "coordinates":        {"lat": lat, "lng": lng},
            "radius_used":        radius_used,
            "total_places_found": len(places),
            "zone_result":        zone_result,
            "source":             "fresh",
        },
    }


# ─────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────
async def address_validation_orbis(request):
    request = request.dict()
    result  = await get_zone(
        name=request["name"],
        address=request["address"],
        bvd_id=request["bvd_id"],
    )
    print("\n── FINAL RESULT ──")
    print(json.dumps(result, indent=2))
    return result