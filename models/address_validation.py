import os
import json
import time
import logging
import requests
from datetime import datetime, timezone
from openai import OpenAI
import httpx
import urllib3
import psycopg2.extras

from models.base_models import AddressZoneMaster, StepStatus
from models.llm_analysis import get_db_connection

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────
GOOGLE_API_KEY       = os.getenv("GOOGLE_API_KEY")
RADIUS_SMALL         = 100
RADIUS_LARGE         = 300
MIN_PLACES_THRESHOLD = 10
LAT_LNG_DELTA        = 0.0009   # ~100m

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

http_client = httpx.Client(
    verify=False,
    timeout=60.0,
    limits=httpx.Limits(max_keepalive_connections=1, max_connections=2)
)

client = OpenAI(http_client=http_client)


# ─────────────────────────────────────────
# STEP 1: GEOCODE
# ─────────────────────────────────────────
def geocode_address(address: str) -> dict:
    url    = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"address": address, "key": GOOGLE_API_KEY}
    return requests.get(url, params=params, verify=False).json()


# ─────────────────────────────────────────
# STEP 2: NEARBY PLACES
# ─────────────────────────────────────────
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
    places = fetch_nearby_places(lat, lng, radius=RADIUS_SMALL)
    if len(places) >= MIN_PLACES_THRESHOLD:
        print(f"      ✔ {len(places)} places @ {RADIUS_SMALL}m — sufficient")
        return places, RADIUS_SMALL
    print(f"      → Only {len(places)} places @ {RADIUS_SMALL}m — expanding to {RADIUS_LARGE}m")
    places = fetch_nearby_places(lat, lng, radius=RADIUS_LARGE)
    print(f"      ✔ {len(places)} places @ {RADIUS_LARGE}m")
    return places, RADIUS_LARGE


# ─────────────────────────────────────────
# ZONE PRE-SCORER  (rule-based, India-aware)
# ─────────────────────────────────────────
DOMINANT_THRESHOLD = 0.60

PLACE_TYPE_WEIGHTS: dict[str, tuple[float, float, float]] = {
    # ── Ubiquitous in Indian residential areas — LOW commercial weight ──
    "grocery_or_supermarket": (0.0, 0.2, 0.0),   # kirana / provision shop
    "convenience_store":      (0.0, 0.2, 0.0),
    "pharmacy":               (0.0, 0.2, 0.0),
    "health":                 (0.0, 0.2, 0.0),
    "doctor":                 (0.0, 0.2, 0.0),
    "hospital":               (0.0, 0.3, 0.0),
    "physiotherapist":        (0.0, 0.15, 0.0),
    "dentist":                (0.0, 0.15, 0.0),
    "atm":                    (0.0, 0.1,  0.0),
    "bank":                   (0.0, 0.3,  0.0),
    "place_of_worship":       (0.2, 0.0,  0.0),
    "hindu_temple":           (0.3, 0.0,  0.0),
    "mosque":                 (0.3, 0.0,  0.0),
    "church":                 (0.3, 0.0,  0.0),
    "school":                 (0.3, 0.1,  0.0),
    "primary_school":         (0.4, 0.0,  0.0),
    "secondary_school":       (0.4, 0.0,  0.0),
    "park":                   (0.5, 0.0,  0.0),
    "food":                   (0.0, 0.2,  0.0),
    "cafe":                   (0.0, 0.25, 0.0),
    "restaurant":             (0.0, 0.25, 0.0),
    "meal_takeaway":          (0.0, 0.2,  0.0),
    "beauty_salon":           (0.0, 0.2,  0.0),
    "hair_care":              (0.0, 0.2,  0.0),
    "laundry":                (0.0, 0.15, 0.0),
    "lodging":                (0.8, 0.2,  0.0),
    "point_of_interest":      (0.0, 0.0,  0.0),
    "establishment":          (0.0, 0.0,  0.0),

    # ── Distinctly commercial ──────────────────────────────────────────
    "shopping_mall":          (0.0, 0.9, 0.0),
    "clothing_store":         (0.0, 0.7, 0.0),
    "electronics_store":      (0.0, 0.7, 0.0),
    "furniture_store":        (0.0, 0.7, 0.0),
    "hardware_store":         (0.0, 0.6, 0.0),
    "home_goods_store":       (0.0, 0.6, 0.0),
    "jewelry_store":          (0.0, 0.7, 0.0),
    "shoe_store":             (0.0, 0.6, 0.0),
    "store":                  (0.0, 0.5, 0.0),
    "supermarket":            (0.0, 0.6, 0.0),
    "department_store":       (0.0, 0.7, 0.0),
    "car_dealer":             (0.0, 0.7, 0.0),
    "car_repair":             (0.0, 0.5, 0.0),
    "gas_station":            (0.0, 0.5, 0.0),
    "parking":                (0.0, 0.3, 0.0),
    "office":                 (0.0, 0.7, 0.0),
    "real_estate_agency":     (0.0, 0.6, 0.0),
    "finance":                (0.0, 0.6, 0.0),
    "insurance_agency":       (0.0, 0.5, 0.0),
    "travel_agency":          (0.0, 0.5, 0.0),
    "accounting":             (0.0, 0.5, 0.0),
    "lawyer":                 (0.0, 0.5, 0.0),
    "gym":                    (0.0, 0.4, 0.0),
    "movie_theater":          (0.0, 0.8, 0.0),
    "night_club":             (0.0, 0.8, 0.0),
    "bar":                    (0.0, 0.6, 0.0),
    "hotel":                  (0.0, 0.8, 0.0),
    "stadium":                (0.0, 0.7, 0.0),
    "university":             (0.1, 0.5, 0.0),
    "transit_station":        (0.0, 0.3, 0.0),
    "bus_station":            (0.0, 0.3, 0.0),
    "train_station":          (0.0, 0.4, 0.0),
    "airport":                (0.0, 0.9, 0.0),
    "spa":                    (0.0, 0.5, 0.0),

    # ── Industrial ─────────────────────────────────────────────────────
    "storage":                (0.0, 0.2, 0.8),
    "moving_company":         (0.0, 0.2, 0.6),
    "factory":                (0.0, 0.0, 1.0),
    "warehouse":              (0.0, 0.0, 0.9),
    "electrician":            (0.0, 0.1, 0.4),
    "plumber":                (0.0, 0.1, 0.4),
    "general_contractor":     (0.0, 0.1, 0.5),
    "roofing_contractor":     (0.0, 0.1, 0.5),
}


def score_places(places: list) -> dict:
    res = com = ind = 0.0
    for place in places:
        for ptype in place.get("types", []):
            weights = PLACE_TYPE_WEIGHTS.get(ptype)
            if weights:
                res += weights[0]
                com += weights[1]
                ind += weights[2]

    total = res + com + ind
    if total == 0:
        return {
            "residential": 0, "commercial": 0, "industrial": 0,
            "dominant_zone": None, "dominant_confidence": 0,
        }

    r_pct, c_pct, i_pct = res / total, com / total, ind / total

    dominant_zone, dominant_confidence = None, 0
    if r_pct >= DOMINANT_THRESHOLD:
        dominant_zone, dominant_confidence = "Residential", round(r_pct * 100)
    elif c_pct >= DOMINANT_THRESHOLD:
        dominant_zone, dominant_confidence = "Commercial", round(c_pct * 100)
    elif i_pct >= DOMINANT_THRESHOLD:
        dominant_zone, dominant_confidence = "Industrial", round(i_pct * 100)

    return {
        "residential":         round(r_pct * 100),
        "commercial":          round(c_pct * 100),
        "industrial":          round(i_pct * 100),
        "dominant_zone":       dominant_zone,
        "dominant_confidence": dominant_confidence,
    }


# ─────────────────────────────────────────
# STEP 3: LLM  (with India-aware pre-score)
# ─────────────────────────────────────────
def classify_zone(address: str, lat: float, lng: float, places: list) -> dict:
    scores = score_places(places)
    print(
        f"      → Pre-score  residential={scores['residential']}%  "
        f"commercial={scores['commercial']}%  industrial={scores['industrial']}%"
    )

    if scores["dominant_zone"]:
        zone = scores["dominant_zone"]
        conf = scores["dominant_confidence"]
        reason = (
            f"Rule-based pre-score: {zone.lower()} places account for {conf}% of weighted "
            f"evidence. Incidental amenities (shops, clinics, ATMs) in an otherwise "
            f"{zone.lower()} area were discounted per Indian urban norms."
        )
        print(f"      ✔ Short-circuit → {zone} ({conf}%) — LLM skipped")
        return {"zone": zone, "confidence": conf, "reason": reason}

    if places:
        simplified_places = [
            {"name": p.get("name"), "types": p.get("types", [])}
            for p in places
        ]
        context = f"""
Coordinates   : lat={lat}, lng={lng}
Nearby Places : {json.dumps(simplified_places, indent=2)}

Pre-computed zone score (weighted, India-adjusted):
  Residential = {scores['residential']}%
  Commercial  = {scores['commercial']}%
  Industrial  = {scores['industrial']}%
        """
    else:
        context = f"Coordinates: lat={lat}, lng={lng}\nNearby Places: Not available"

    prompt = f"""
You are an Indian urban zoning expert classifying addresses for Indian cities and towns.

IMPORTANT RULES FOR INDIAN CONTEXT:
1. Almost every Indian residential area has some provision/kirana shops, small clinics,
   medical shops, small eateries, temples, and ATMs nearby. These are NOT indicators of
   a commercial or mixed zone — they are a normal part of Indian residential neighbourhoods.
2. Classify as "Mixed" ONLY when there is a genuine, substantial commercial or industrial
   presence (market street, commercial complex, offices, large retail stores) that exists
   alongside housing in roughly equal measure.
3. Use the pre-computed zone scores as your primary guide. Override only with strong reason.
4. When in doubt between "Residential" and "Mixed", choose "Residential".

ZONE DEFINITIONS:
- "Residential"  → primarily housing / living area (incidental shops/clinics are fine here)
- "Commercial"   → primarily shops / offices / businesses / markets
- "Mixed"        → substantial residential AND commercial in near-equal proportion
- "Industrial"   → factories / warehouses / manufacturing units
- "Unknown"      → genuinely cannot determine

Address : {address}
{context}

Respond ONLY in this exact JSON format:
{{
    "zone": "Residential | Commercial | Mixed | Industrial | Unknown",
    "confidence": <integer 0-100>,
    "reason": "one line explanation referencing the dominant evidence"
}}
    """
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    return json.loads(response.choices[0].message.content)


# ─────────────────────────────────────────
# DB HELPERS
# ─────────────────────────────────────────
def db_insert(record: AddressZoneMaster) -> str | None:
    conn = get_db_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO public.address_zone_master
                (geo_id, name, address, identifier, identifier_type, entity_type,
                 lat, lng, geocode_status, places_status, llm_status,
                 created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (geo_id) DO NOTHING
            RETURNING geo_id
        """, (
            record.geo_id,
            record.name, record.address,
            record.identifier, record.identifier_type, record.entity_type,
            record.lat, record.lng,
            record.geocode_status, record.places_status, record.llm_status,
            datetime.now(timezone.utc), datetime.now(timezone.utc),
        ))
        row = cur.fetchone()
        conn.commit()
        return row[0] if row else record.geo_id
    except Exception as e:
        conn.rollback()
        logger.error(f"db_insert: {e}")
        return None
    finally:
        cur.close()
        conn.close()


def db_update(geo_id: str, fields: dict):
    conn = get_db_connection()
    cur  = conn.cursor()
    try:
        fields["updated_at"] = datetime.now(timezone.utc)
        set_clause = ", ".join([f"{k} = %s" for k in fields.keys()])
        values = list(fields.values()) + [geo_id]
        cur.execute(
            f"UPDATE public.address_zone_master SET {set_clause} WHERE geo_id = %s",
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


def is_valid_lat_lng(lat, lng) -> bool:
    try:
        return -90 <= float(lat) <= 90 and -180 <= float(lng) <= 180
    except (TypeError, ValueError):
        return False


def db_find_cached(identifier: str, lat: float, lng: float) -> dict | None:
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""
            SELECT geo_id, lat, lng, places, zone, confidence, reason,
                   geocode_status, places_status, llm_status, updated_at
            FROM public.address_zone_master
            WHERE identifier = %s
              AND lat IS NOT NULL AND lng IS NOT NULL
              AND geocode_status = 'passed'
            ORDER BY updated_at DESC
        """, (identifier,))

        rows = cur.fetchall()
        if not rows:
            print(f"      → No cache found for identifier={identifier}")
            return None

        best_match, best_dist = None, None
        print(f"      → {len(rows)} cached rows found")

        for row in rows:
            db_lat, db_lng = row["lat"], row["lng"]
            if not is_valid_lat_lng(db_lat, db_lng):
                continue
            lat_diff = abs(db_lat - lat)
            lng_diff = abs(db_lng - lng)
            print(f"      → geo_id={row['geo_id']} Δlat={round(lat_diff,6)} Δlng={round(lng_diff,6)}")
            if lat_diff <= LAT_LNG_DELTA and lng_diff <= LAT_LNG_DELTA:
                dist = distance_sq(db_lat, db_lng, lat, lng)
                if best_dist is None or dist < best_dist:
                    best_dist, best_match = dist, row

        if not best_match:
            print(f"      → No cache within ~100m for identifier={identifier} at ({lat}, {lng})")
            return None

        print(f"      ✔ Cache match — geo_id={best_match['geo_id']}")
        return {
            "geo_id":         best_match["geo_id"],
            "lat":            best_match["lat"],
            "lng":            best_match["lng"],
            "places":         normalize_places(best_match["places"]),
            "zone":           best_match["zone"],
            "confidence":     best_match["confidence"],
            "reason":         best_match["reason"],
            "geocode_status": best_match["geocode_status"],
            "places_status":  best_match["places_status"],
            "llm_status":     best_match["llm_status"],
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
async def handle_cached(cached: dict, address: str) -> dict:
    geo_id = cached["geo_id"]
    lat    = cached["lat"]
    lng    = cached["lng"]
    places = cached["places"]

    # CASE 1: llm passed → return cached directly
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

    # CASE 2: places passed, llm failed → re-run LLM only
    if cached["places_status"] == StepStatus.passed.value:
        print(f"      → Cache: places passed — re-running LLM only")
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

    # CASE 3: places failed/not_called → re-run places + LLM
    print(f"      → Cache: places failed/not_called — re-running places + LLM")
    try:
        places, radius_used = fetch_places_adaptive(lat, lng)
        simplified_places = [{"name": p.get("name"), "types": p.get("types", [])} for p in places]
        db_update(geo_id, {
            "places":        json.dumps(simplified_places),
            "places_status": StepStatus.passed.value,
        })
    except Exception as e:
        logger.error(f"Cache places re-run error: {e}")
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
    identifier: str, identifier_type: str, entity_type: str,
) -> dict:

    # STEP 1: GEOCODE
    print(f"[1/3] Geocoding: {address}")
    try:
        geocode_data = geocode_address(address)
        if geocode_data.get("status") != "OK":
            raise ValueError(f"Google status: {geocode_data.get('status')}")
        location = geocode_data["results"][0]["geometry"]["location"]
        lat, lng = location["lat"], location["lng"]
        geo_id   = f"{identifier}_{round(lat, 6)}_{round(lng, 6)}"
        print(f"      ✔ ({lat}, {lng}) → geo_id={geo_id}")
    except Exception as e:
        logger.error(f"Geocoding error: {e}")
        record = AddressZoneMaster(
            geo_id=f"failed_{identifier}_{datetime.now(timezone.utc).timestamp()}",
            name=name, address=address, lat=None, lng=None,
            identifier=identifier, identifier_type=identifier_type, entity_type=entity_type,
            geocode_status=StepStatus.failed,
            places_status=StepStatus.failed,
            llm_status=StepStatus.failed,
        )
        db_insert(record)
        return {"status": 404, "message": "Geocoding failed. Exiting."}

    # CACHE CHECK
    print(f"      → Checking cache for identifier={identifier} near ({lat}, {lng})")
    cached = db_find_cached(identifier, lat, lng)
    if cached:
        return await handle_cached(cached, address)

    # FRESH RUN
    print(f"      → No cache — fresh run")
    record = AddressZoneMaster(
        geo_id=geo_id, name=name, address=address, lat=lat, lng=lng,
        identifier=identifier, identifier_type=identifier_type, entity_type=entity_type,
        geocode_status=StepStatus.passed,
        places_status=StepStatus.not_called,
        llm_status=StepStatus.not_called,
    )
    geo_id = db_insert(record)
    if not geo_id:
        logger.error("db_insert returned None — cannot proceed")
        return {"status": 500, "message": "DB insert failed"}

    # STEP 2: NEARBY PLACES
    places      = []
    radius_used = RADIUS_SMALL
    print(f"[2/3] Fetching nearby places (adaptive radius)...")
    try:
        places, radius_used = fetch_places_adaptive(lat, lng)
        print(f"      ✔ {len(places)} places found @ {radius_used}m")
        simplified_places = [{"name": p.get("name"), "types": p.get("types", [])} for p in places]
        db_update(geo_id, {
            "places":        json.dumps(simplified_places),
            "places_status": StepStatus.passed.value,
        })
    except Exception as e:
        logger.error(f"Places error: {e}")
        db_update(geo_id, {"places_status": StepStatus.failed.value})

    # STEP 3: CLASSIFY
    print(f"[3/3] Classifying zone (rule-based pre-score + LLM fallback)...")
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
async def address_validation(request):
    request = request.dict()
    result  = await get_zone(
        name=request["name"],
        address=request["address"],
        identifier=request["identifier"],
        identifier_type=request["identifier_type"],
        entity_type=request["entity_type"],
    )
    print("\n── FINAL RESULT ──")
    print(json.dumps(result, indent=2))
    return result