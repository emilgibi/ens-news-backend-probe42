import os
import json
import logging
import requests
from datetime import datetime, timezone

import psycopg2.extras
import urllib3
import httpx
from openai import OpenAI
from openai import AzureOpenAI

from models.base_models import AddressZoneMaster, StepStatus, GeocodeSource
from models.llm_analysis import get_db_connection

logger = logging.getLogger(__name__)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ─────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
AZURE_ENDPOINT = os.getenv("AZURE_ENDPOINT")
API_KEY = os.getenv("API_KEY")

LAT_LNG_DELTA = 0.0009  # ~100m

# Kept for compatibility (no longer used for nearby search)
RADIUS_SMALL = 200
RADIUS_LARGE = 500
MIN_PLACES_THRESHOLD = 10

# ─────────────────────────────────────────
# HARDENING SETTINGS (safe defaults)
# ─────────────────────────────────────────
GOOGLE_HTTP_TIMEOUT_SECS = float(os.getenv("GOOGLE_HTTP_TIMEOUT_SECS", "20"))
OPENAI_HTTP_TIMEOUT_SECS = float(os.getenv("OPENAI_HTTP_TIMEOUT_SECS", "60"))

# If LLM is unsure, force Unknown
LLM_MIN_CONFIDENCE = int(os.getenv("LLM_MIN_CONFIDENCE", "70"))

# AzureOpenAI client (kept since your repo already has it; not used below)
client1 = AzureOpenAI(
    azure_endpoint=AZURE_ENDPOINT,
    api_key=API_KEY,
    api_version="2024-07-01-preview",
)

# OpenAI client used for classification
http_client = httpx.Client(
    verify=False,  # corporate proxy
    timeout=OPENAI_HTTP_TIMEOUT_SECS,
    limits=httpx.Limits(max_keepalive_connections=1, max_connections=2),
)
client = OpenAI(http_client=http_client)

# ─────────────────────────────────────────
# VERY VISIBLE GUIDE (for quick navigation)
# ─────────────────────────────────────────
# ✅ BUILDING-TYPE CLASSIFIER (INDIA) — HIGH ACCURACY, CONSERVATIVE
#
# STEP 1: Google Geocode (mandatory)
# STEP 2: Try Google Place Details for SAME entity (mandatory try)
# STEP 3: High-precision short-circuit (only when obvious)
# STEP 4: LLM classification using ONLY building signals:
#         (geocode + place_details) if available, else (geocode only)
#         IMPORTANT: LLM never sees company name
# STEP 5: Confidence gate: if confidence < LLM_MIN_CONFIDENCE => Unknown
# STEP 6: Cache/DB persistence (table structure unchanged)
#
# ❌ NOT USED: Nearby Search / surrounding POIs / density zoning
# ─────────────────────────────────────────


# ─────────────────────────────────────────
# INTERNAL SAFE HELPERS
# ─────────────────────────────────────────
def _safe_int(value, default=0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


def _requests_get_json(url: str, *, params: dict) -> dict:
    """
    Centralized requests.get wrapper for Google calls.
    """
    try:
        r = requests.get(
            url,
            params=params,
            verify=False,
            timeout=GOOGLE_HTTP_TIMEOUT_SECS,
        )
        return r.json()
    except requests.exceptions.Timeout as e:
        raise TimeoutError(f"Timeout calling {url}") from e
    except Exception as e:
        raise RuntimeError(f"Failed calling {url}: {e}") from e


def _normalize_zone_result(zone: str | None, confidence, reason: str | None) -> dict:
    allowed = {"Residential", "Commercial", "Unknown"}
    z = zone if zone in allowed else "Unknown"
    c = _clamp(_safe_int(confidence, 0), 0, 100)
    r = (reason or "").strip() or "No reason provided"
    return {"zone": z, "confidence": c, "reason": r[:500]}


def _pick_components(components: list | None) -> dict:
    """
    Extract key address components for easy console inspection.
    """
    out = {}
    if not components:
        return out
    for c in components:
        t = c.get("types") or []
        if not t:
            continue
        key = t[0]
        out[key] = c.get("long_name")
    return out


def _print_debug_evidence(
    *,
    address: str,
    geocode_result: dict,
    place_details: dict | None,
    place_id: str | None,
    place_details_source: str,
):
    """
    Console logs for testing: types/components etc.
    Never fails the request.
    """
    try:
        print("\n================ ADDRESS VALIDATION DEBUG ================")
        print(f"[DEBUG] Input address: {address}")
        print(f"[DEBUG] place_id: {place_id}")
        print(f"[DEBUG] Place Details source: {place_details_source}")

        print("\n[DEBUG] GEOCODE.formatted_address:", geocode_result.get("formatted_address"))
        print("[DEBUG] GEOCODE.types:", geocode_result.get("types"))
        print("[DEBUG] GEOCODE.components(picked):", json.dumps(_pick_components(geocode_result.get("address_components")), indent=2, ensure_ascii=False))

        if place_details:
            print("\n[DEBUG] PLACE_DETAILS.formatted_address:", place_details.get("formatted_address"))
            # IMPORTANT: do not use place_details["name"] for classification; we log it optionally for debugging only.
            print("[DEBUG] PLACE_DETAILS.name (debug only):", place_details.get("name"))
            print("[DEBUG] PLACE_DETAILS.types:", place_details.get("types"))
            print("[DEBUG] PLACE_DETAILS.components(picked):", json.dumps(_pick_components(place_details.get("address_components")), indent=2, ensure_ascii=False))
            print("[DEBUG] PLACE_DETAILS.business_status:", place_details.get("business_status"))
        else:
            print("\n[DEBUG] PLACE_DETAILS: NOT AVAILABLE")

        print("==========================================================\n")
    except Exception as e:
        logger.error(f"debug print failed: {e}", exc_info=True)


# ─────────────────────────────────────────
# GOOGLE: GEOCODE
# ─────────────────────────────────────────
def geocode_address(address: str) -> dict:
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"address": address, "key": GOOGLE_API_KEY}
    return _requests_get_json(url, params=params)


# ─────────────────────────────────────────
# GOOGLE: PLACE DETAILS (same entity, NOT nearby)
# ─────────────────────────────────────────
def fetch_place_id_from_text(name: str, address: str) -> str | None:
    """
    Find place_id for the entity using Find Place From Text (NOT nearby).
    We use name+address to get best match, but we will NEVER feed company name into LLM.
    """
    url = "https://maps.googleapis.com/maps/api/place/findplacefromtext/json"
    text = f"{name} {address}".strip() if name else address.strip()

    params = {
        "input": text,
        "inputtype": "textquery",
        "fields": "place_id",
        "key": GOOGLE_API_KEY,
    }
    resp = _requests_get_json(url, params=params)

    if resp.get("status") != "OK":
        return None

    candidates = resp.get("candidates") or []
    if not candidates:
        return None

    return candidates[0].get("place_id")


def fetch_place_details(place_id: str) -> dict | None:
    """
    Place Details for the same entity.
    """
    url = "https://maps.googleapis.com/maps/api/place/details/json"
    params = {
        "place_id": place_id,
        "fields": "types,address_components,formatted_address,name,business_status",
        "key": GOOGLE_API_KEY,
    }
    resp = _requests_get_json(url, params=params)
    if resp.get("status") != "OK":
        return None
    return resp.get("result")


# ─────────────────────────────────────────
# INDIA-FOCUSED: HIGH-PRECISION SHORT-CIRCUIT
# ─────────────────────────────────────────
COMMERCIAL_PLACE_TYPES = {
    # These are strong signals for commercial/institutional use (high precision)
    "accounting", "atm", "bank", "bar", "beauty_salon", "book_store",
    "cafe", "car_dealer", "car_rental", "car_repair", "clinic", "dentist",
    "department_store", "doctor", "drugstore", "electronics_store",
    "fire_station", "gas_station", "gym", "hair_care", "hardware_store",
    "hospital", "insurance_agency", "jewelry_store", "laundry", "lawyer",
    "library", "lodging", "meal_delivery", "meal_takeaway", "movie_theater",
    "museum", "night_club", "parking", "pharmacy", "physiotherapist",
    "police", "post_office", "primary_school", "real_estate_agency",
    "restaurant", "school", "secondary_school", "shopping_mall", "spa",
    "storage", "store", "supermarket", "taxi_stand", "train_station",
    "transit_station", "travel_agency", "university", "veterinary_care",
    "office",
}

RESIDENTIAL_PLACE_TYPES = {
    # Not always present in India, but when present it helps
    "apartment", "housing_complex", "premise", "street_address", "subpremise",
}


def short_circuit_india_high_precision(place_details: dict | None) -> dict | None:
    """
    Conservative (high accuracy):
    - Commercial only when place types strongly indicate it.
    - Residential only when premise/street_address-like and NOT establishment.
    - Otherwise None.
    """
    if not place_details:
        return None

    types = set(place_details.get("types") or [])

    if types.intersection(COMMERCIAL_PLACE_TYPES):
        return _normalize_zone_result(
            "Commercial",
            92,
            f"Short-circuit: Place Details types indicate commercial/institutional use: {sorted(list(types))[:12]}",
        )

    # Residential short-circuit is weaker; keep conservative
    if types.intersection(RESIDENTIAL_PLACE_TYPES) and "establishment" not in types:
        return _normalize_zone_result(
            "Residential",
            72,
            f"Short-circuit: Place Details types look premise/residential-ish and not establishment: {sorted(list(types))[:12]}",
        )

    return None


# ─────────────────────────────────────────
# LLM: BUILDING-ONLY CLASSIFICATION (no company name)
# ─────────────────────────────────────────
def llm_classify_building_residential_commercial_unknown(
    *,
    address: str,
    lat: float,
    lng: float,
    geocode_result: dict,
    place_details: dict | None,
) -> dict:
    """
    LLM must classify based ONLY on building/address signals (types/components/formatted_address).
    Company name is intentionally excluded to avoid bias.
    """
    building_evidence = {
        "address": address,
        "coordinates": {"lat": lat, "lng": lng},
        "geocode": {
            "formatted_address": geocode_result.get("formatted_address"),
            "types": geocode_result.get("types"),
            "address_components": geocode_result.get("address_components"),
        },
        "place_details": None if not place_details else {
            "formatted_address": place_details.get("formatted_address"),
            "types": place_details.get("types"),
            "address_components": place_details.get("address_components"),
            "business_status": place_details.get("business_status"),
        },
        "india_context": (
            "In Indian addresses, words like Layout/Nagar/Sector/Phase are NOT decisive; "
            "those areas can contain both homes and commercial complexes. Do not guess from those."
        ),
    }

    prompt = f"""
You are validating an address in India for a building-type risk check.

Goal:
Classify the BUILDING/LOCATION USE at this address as exactly one of:
- "Residential"
- "Commercial"
- "Unknown"

CRITICAL RULES:
- Ignore company/entity name (it is not provided).
- Do NOT use nearby POIs or surrounding shops/buildings.
- Use ONLY the evidence JSON (geocode + optional place_details for this exact entity/address).
- In India, "Layout/Nagar/Sector/Phase/Block" are common and not decisive. Do not infer from them.
- If evidence is ambiguous, return "Unknown" (do not guess).
- Confidence must be 0-100.

Evidence JSON:
{json.dumps(building_evidence, ensure_ascii=False)}

Return ONLY valid JSON:
{{
  "zone": "Residential|Commercial|Unknown",
  "confidence": <0-100>,
  "reason": "short reason based on types/components/formatted address"
}}
"""
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    raw = json.loads(response.choices[0].message.content)
    result = _normalize_zone_result(raw.get("zone"), raw.get("confidence"), raw.get("reason"))

    # Confidence gate
    if result["zone"] != "Unknown" and result["confidence"] < LLM_MIN_CONFIDENCE:
        return _normalize_zone_result(
            "Unknown",
            result["confidence"],
            f"Confidence<{LLM_MIN_CONFIDENCE}; forcing Unknown. Original: {result['reason']}",
        )

    return result


# ─────────────────────────────────────────
# DB HELPERS (unchanged)
# ─────────────────────────────────────────
def db_insert(record: AddressZoneMaster) -> str | None:
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO public.address_zone_master
                (geo_id, name, address, identifier, identifier_type, entity_type,
                 lat, lng,
                 geocode_status, places_status, llm_status,
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
        logger.error(f"db_insert: {e}", exc_info=True)
        return None
    finally:
        cur.close()
        conn.close()


def db_update(geo_id: str, fields: dict):
    conn = get_db_connection()
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


def is_valid_lat_lng(lat, lng) -> bool:
    try:
        lat = float(lat)
        lng = float(lng)
        return -90 <= lat <= 90 and -180 <= lng <= 180
    except (TypeError, ValueError):
        return False


def db_find_cached(identifier: str, lat: float, lng: float) -> dict | None:
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""
            SELECT
                geo_id,
                lat       AS lat,
                lng       AS lng,
                zone,
                confidence,
                reason,
                geocode_status,
                places_status,
                llm_status,
                updated_at
            FROM public.address_zone_master
            WHERE identifier = %s
              AND lat IS NOT NULL
              AND lng IS NOT NULL
              AND geocode_status = 'passed'
            ORDER BY updated_at DESC
        """, (identifier,))

        rows = cur.fetchall()
        if not rows:
            return None

        best_match = None
        best_dist = None

        for row in rows:
            db_lat = row["lat"]
            db_lng = row["lng"]
            if not is_valid_lat_lng(db_lat, db_lng):
                continue

            lat_diff = abs(db_lat - lat)
            lng_diff = abs(db_lng - lng)

            if lat_diff <= LAT_LNG_DELTA and lng_diff <= LAT_LNG_DELTA:
                dist = distance_sq(db_lat, db_lng, lat, lng)
                if best_dist is None or dist < best_dist:
                    best_dist = dist
                    best_match = row

        if not best_match:
            return None

        return {
            "geo_id": best_match["geo_id"],
            "lat": best_match["lat"],
            "lng": best_match["lng"],
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
async def handle_cached(cached: dict, *, address: str, name: str) -> dict:
    """
    If llm_status passed -> return cached.
    Else -> rerun best algorithm:
      geocode -> place_details -> short-circuit -> LLM -> confidence gate -> persist
    """
    geo_id = cached["geo_id"]
    lat = cached["lat"]
    lng = cached["lng"]

    if cached["llm_status"] == StepStatus.passed.value:
        return {
            "status": 200,
            "data": {
                "address": address,
                "coordinates": {"lat": lat, "lng": lng},
                "zone_result": {
                    "zone": cached["zone"],
                    "confidence": cached["confidence"],
                    "reason": cached["reason"],
                },
                "source": "cache",
            },
        }

    try:
        # STEP 1: Geocode
        geocode_data = geocode_address(address)
        if geocode_data.get("status") != "OK" or not geocode_data.get("results"):
            raise ValueError(f"Google status: {geocode_data.get('status')}")

        geocode_result = geocode_data["results"][0]

        # STEP 2: Place Details try
        place_id = geocode_result.get("place_id")
        place_details = None
        place_details_source = "geocode.place_id"
        try:
            if not place_id:
                place_id = fetch_place_id_from_text(name, address)
                place_details_source = "findplacefromtext"
            if place_id:
                place_details = fetch_place_details(place_id)
        except Exception as e:
            logger.error(f"[CACHE] place_details lookup failed: {e}", exc_info=True)

        _print_debug_evidence(
            address=address,
            geocode_result=geocode_result,
            place_details=place_details,
            place_id=place_id,
            place_details_source=place_details_source,
        )

        # STEP 3: short-circuit
        zone_result = short_circuit_india_high_precision(place_details)

        # STEP 4: LLM if not short-circuited (building-only)
        if not zone_result:
            zone_result = llm_classify_building_residential_commercial_unknown(
                address=address,
                lat=lat,
                lng=lng,
                geocode_result=geocode_result,
                place_details=place_details,
            )

        db_update(geo_id, {
            "zone": zone_result.get("zone"),
            "confidence": zone_result.get("confidence"),
            "reason": zone_result.get("reason"),
            "places_status": StepStatus.not_called.value,  # no nearby search
            "llm_status": StepStatus.passed.value,
        })

        return {
            "status": 200,
            "data": {
                "address": address,
                "coordinates": {"lat": lat, "lng": lng},
                "zone_result": zone_result,
                "source": "cache_rerun",
            },
        }

    except Exception as e:
        logger.error(f"Cache re-run failed: {e}", exc_info=True)
        db_update(geo_id, {"llm_status": StepStatus.failed.value})
        return {"status": 500, "message": "Cache re-run failed"}


# ─────────────────────────────────────────
# MAIN FUNCTION
# ─────────────────────────────────────────
async def get_zone(
    *,
    name: str,
    address: str,
    identifier: str,
    identifier_type: str,
    entity_type: str,
) -> dict:
    # ──────────────────────────────────────
    # STEP 1: GEOCODE
    # ──────────────────────────────────────
    print(f"[1/4] Geocoding: {address}")
    try:
        geocode_data = geocode_address(address)
        if geocode_data.get("status") != "OK" or not geocode_data.get("results"):
            raise ValueError(f"Google status: {geocode_data.get('status')}")

        geocode_result = geocode_data["results"][0]
        location = geocode_result["geometry"]["location"]
        lat, lng = location["lat"], location["lng"]
        geo_id = f"{identifier}_{round(lat, 6)}_{round(lng, 6)}"
        print(f"      ✔ ({lat}, {lng}) → geo_id={geo_id}")

    except Exception as e:
        logger.error(f"Geocoding error: {e}", exc_info=True)
        record = AddressZoneMaster(
            geo_id=f"failed_{identifier}_{datetime.now(timezone.utc).timestamp()}",
            name=name,
            address=address,
            lat=None,
            lng=None,
            identifier=identifier,
            identifier_type=identifier_type,
            entity_type=entity_type,
            geocode_status=StepStatus.failed,
            places_status=StepStatus.failed,
            llm_status=StepStatus.failed,
        )
        db_insert(record)
        return {"status": 404, "message": "Geocoding failed. Exiting."}

    # ──────────────────────────────────────
    # CACHE CHECK
    # ──────────────────────────────────────
    cached = db_find_cached(identifier, lat, lng)
    if cached:
        return await handle_cached(cached, address=address, name=name)

    # ──────────────────────────────────────
    # FRESH RUN — insert initial row
    # ──────────────────────────────────────
    record = AddressZoneMaster(
        geo_id=geo_id,
        name=name,
        address=address,
        lat=lat,
        lng=lng,
        identifier=identifier,
        identifier_type=identifier_type,
        entity_type=entity_type,
        geocode_status=StepStatus.passed,
        places_status=StepStatus.not_called,  # no nearby search
        llm_status=StepStatus.not_called,
    )
    geo_id = db_insert(record)
    if not geo_id:
        return {"status": 500, "message": "DB insert failed"}

    # ──────────────────────────────────────
    # STEP 2: PLACE DETAILS TRY
    # ──────────────────────────────────────
    print("[2/4] Fetching Place Details (same entity, not nearby)...")
    place_id = geocode_result.get("place_id")
    place_details = None
    place_details_source = "geocode.place_id"
    try:
        if not place_id:
            place_id = fetch_place_id_from_text(name, address)
            place_details_source = "findplacefromtext"
        if place_id:
            place_details = fetch_place_details(place_id)
    except Exception as e:
        logger.error(f"Place details lookup failed: {e}", exc_info=True)

    _print_debug_evidence(
        address=address,
        geocode_result=geocode_result,
        place_details=place_details,
        place_id=place_id,
        place_details_source=place_details_source,
    )

    # ──────────────────────────────────────
    # STEP 3: SHORT-CIRCUIT IF OBVIOUS
    # ──────────────────────────────────────
    print("[3/4] Short-circuit decision (India high precision)...")
    zone_result = short_circuit_india_high_precision(place_details)

    # ──────────────────────────────────────
    # STEP 4: LLM CLASSIFICATION (final)
    # ──────────────────────────────────────
    if not zone_result:
        print("[4/4] LLM classification (building-only; place_details if available else geocode only)...")
        try:
            zone_result = llm_classify_building_residential_commercial_unknown(
                address=address,
                lat=lat,
                lng=lng,
                geocode_result=geocode_result,
                place_details=place_details,
            )
        except Exception as e:
            logger.error(f"LLM classification failed: {e}", exc_info=True)
            db_update(geo_id, {"llm_status": StepStatus.failed.value})
            return {"status": 500, "message": "LLM classification failed"}

    db_update(geo_id, {
        "zone": zone_result.get("zone"),
        "confidence": zone_result.get("confidence"),
        "reason": zone_result.get("reason"),
        "places_status": StepStatus.not_called.value,  # no nearby search
        "llm_status": StepStatus.passed.value,
    })

    return {
        "status": 200,
        "data": {
            "name": name,
            "address": address,
            "coordinates": {"lat": lat, "lng": lng},
            "zone_result": zone_result,
            "source": "fresh",
        },
    }


# ─────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────
async def address_validation(request):
    request = request.dict()
    result = await get_zone(
        name=request["name"],
        address=request["address"],
        identifier=request["identifier"],
        identifier_type=request["identifier_type"],
        entity_type=request["entity_type"],
    )
    print("\n── FINAL RESULT ──")
    print(json.dumps(result, indent=2))
    return result