#!/usr/bin/env python3
"""Spike script: answer the three questions that could invalidate the architecture.

Run this against ONE address you know well before building any connector properly.
For roughly the price of a coffee it settles:

  Q1  Does the Places Aggregate API accept a Mapbox isochrone ring as-is?
      Mapbox contours carry hundreds of vertices. If Google rejects the polygon
      for size, every catchment needs simplification first -- and simplification
      changes the area being measured, which is a design decision, not a detail.

  Q2  Does a real catchment blow past the 100-place cap?
      Aggregate returns place IDs only when the match count is 100 or fewer. If
      dense areas routinely exceed it, `census_complete: false` is the NORMAL
      path rather than an edge case, and the undercount warning needs to be far
      more prominent than it currently is.

  Q3  What do the SKUs actually cost?
      Unverified in the PRD -- developers.google.com was unreachable from the
      build environment. Check the console's billing report after this runs.

IMPORTANT: the request shapes below come from documentation research and have
NOT been executed against the live APIs. Discrepancies are the finding, not a
bug -- record what actually comes back. Every call prints its raw status and
body on failure for exactly that reason.

Usage:
    # PowerShell
    $env:GOOGLE_MAPS_API_KEY="..."; $env:MAPBOX_ACCESS_TOKEN="..."
    python scripts/probe.py "4820 Skillman Ave, Sunnyside, NY 11104" --vertical laundromat

    # bash / zsh
    export GOOGLE_MAPS_API_KEY=... MAPBOX_ACCESS_TOKEN=...
    python3 scripts/probe.py "4820 Skillman Ave, Sunnyside NY" --vertical laundromat

Costs nothing beyond the API calls it makes: 1 geocode, 1 isochrone, 1-4
Aggregate calls, and Place Details only if you pass --details N (default 0).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import requests

from mcds.catchment.isochrone import polygon_area_sq_km, simplify_ring
from mcds.scoring.geography import haversine_km
from mcds.config import load_dotenv, load_vertical

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
MAPBOX_URL = "https://api.mapbox.com/isochrone/v1/mapbox/{profile}/{lng},{lat}"
ROUTE_MATRIX_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
AGGREGATE_URL = "https://areainsights.googleapis.com/v1:computeInsights"
DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"

#: Escalating simplification tolerances, tried in order until Google accepts the
#: polygon. Area distortion is reported at each step so the cost is visible.
TOLERANCES_KM = [0.0, 0.05, 0.1, 0.25, 0.5]

TIMEOUT = 30


def hr(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def fail(response: requests.Response, what: str) -> None:
    """Print the actual status and body. The body is the finding."""
    print(f"  FAILED ({what}): HTTP {response.status_code}")
    body = response.text[:1500]
    print(f"  {body}")


def geocode(address: str, api_key: str) -> dict | None:
    hr("1. Geocode")
    r = requests.get(
        GEOCODE_URL, params={"address": address, "key": api_key}, timeout=TIMEOUT
    )
    if r.status_code != 200:
        fail(r, "geocode")
        return None
    data = r.json()
    if data.get("status") != "OK":
        print(f"  status={data.get('status')}  {data.get('error_message', '')}")
        return None

    top = data["results"][0]
    loc = top["geometry"]["location"]
    quality = top["geometry"].get("location_type")
    print(f"  {top['formatted_address']}")
    print(f"  lat={loc['lat']:.6f} lng={loc['lng']:.6f}  precision={quality}")
    if quality not in ("ROOFTOP", "RANGE_INTERPOLATED"):
        print(f"  ! {quality} precision -- a catchment from this origin may be off")
    return {"lat": loc["lat"], "lng": loc["lng"], "quality": quality}


def isochrone(lat: float, lng: float, minutes: int, profile: str, token: str) -> list | None:
    hr(f"2. Mapbox isochrone ({minutes} min, {profile})")
    r = requests.get(
        MAPBOX_URL.format(profile=profile, lng=lng, lat=lat),
        params={
            "contours_minutes": minutes,
            "polygons": "true",
            "denoise": 1.0,
            "access_token": token,
        },
        timeout=TIMEOUT,
    )
    if r.status_code != 200:
        fail(r, "isochrone")
        return None

    features = r.json().get("features", [])
    if not features:
        print("  no contour returned")
        return None

    geom = features[0]["geometry"]
    print(f"  geometry type: {geom['type']}")
    if geom["type"] == "MultiPolygon":
        rings = [poly[0] for poly in geom["coordinates"]]
        areas = [polygon_area_sq_km(x) for x in rings]
        print(f"  ! MultiPolygon with {len(rings)} parts, areas {[round(a,2) for a in areas]} km2")
        print("    Keeping the largest. The discarded area is real catchment.")
        ring = rings[areas.index(max(areas))]
    else:
        ring = geom["coordinates"][0]

    print(f"  vertices: {len(ring)}")
    print(f"  area:     {polygon_area_sq_km(ring):.2f} km2")
    print(f"  traffic-aware: {profile == 'driving-traffic'}")
    return ring


def to_google_polygon(ring: list) -> dict:
    """GeoJSON [lng, lat] pairs -> Google's {latitude, longitude} objects.

    A real gotcha: GeoJSON is lng-first, Google's LatLng is lat-first. Swapping
    them produces a valid-looking request describing a polygon in the wrong
    hemisphere, which returns zero results rather than an error.
    """
    return {"coordinates": [{"latitude": p[1], "longitude": p[0]} for p in ring]}


def aggregate(ring: list, types: list[str], api_key: str) -> dict | None:
    hr("3. Places Aggregate -- computeInsights")
    original_area = polygon_area_sq_km(ring)

    for tolerance in TOLERANCES_KM:
        candidate = ring if tolerance == 0.0 else simplify_ring(ring, tolerance)
        area = polygon_area_sq_km(candidate)
        drift = 100 * (area - original_area) / original_area if original_area else 0.0

        label = "as-is" if tolerance == 0.0 else f"simplified @ {tolerance} km"
        print(f"\n  Attempt: {label} -- {len(candidate)} vertices, "
              f"{area:.2f} km2 ({drift:+.2f}% area)")

        body: dict[str, Any] = {
            "insights": ["INSIGHT_COUNT", "INSIGHT_PLACES"],
            "filter": {
                "locationFilter": {"customArea": {"polygon": to_google_polygon(candidate)}},
                "typeFilter": {"includedTypes": types},
                "operatingStatus": ["OPERATING_STATUS_OPERATIONAL"],
            },
        }
        r = requests.post(
            AGGREGATE_URL,
            headers={"X-Goog-Api-Key": api_key, "Content-Type": "application/json"},
            json=body,
            timeout=TIMEOUT,
        )

        if r.status_code == 200:
            data = r.json()
            count = int(data.get("count", 0))
            places = data.get("placeInsights", []) or []
            print(f"  ACCEPTED. count={count}, place IDs returned={len(places)}")

            # `includedTypes` matches a place's primary OR secondary types, which
            # is how a carpet-cleaning firm carrying `laundry` as a secondary tag
            # lands in a laundromat census. `includedPrimaryTypes` matches only
            # the single primary type. One extra call says how much precision
            # that buys on this market.
            strict_ids: list[str] | None = None
            strict = requests.post(
                AGGREGATE_URL,
                headers={"X-Goog-Api-Key": api_key, "Content-Type": "application/json"},
                json={
                    "insights": ["INSIGHT_COUNT", "INSIGHT_PLACES"],
                    "filter": {
                        "locationFilter": {"customArea": {"polygon": to_google_polygon(candidate)}},
                        "typeFilter": {"includedPrimaryTypes": types},
                        "operatingStatus": ["OPERATING_STATUS_OPERATIONAL"],
                    },
                },
                timeout=TIMEOUT,
            )
            if strict.status_code == 200:
                body = strict.json()
                strict_count = int(body.get("count", 0))
                strict_ids = [
                    p.get("place", "").split("/")[-1]
                    for p in (body.get("placeInsights", []) or [])
                ]
                print(f"\n  Same polygon with includedPrimaryTypes: "
                      f"count={strict_count} (vs {count} with includedTypes)")
                if strict_count < count:
                    print(f"  -> Drops {count - strict_count} places that carry "
                          f"`laundry` only as a secondary tag.")
                    print(f"     Whether that is precision or lost coverage "
                          f"depends on WHICH ones;")
                    print(f"     see the comparison after enrichment.")
                else:
                    print(f"  -> No reduction. The contamination sits in primary "
                          f"types, so only")
                    print(f"     classification can remove it.")
            else:
                fail(strict, "includedPrimaryTypes comparison")

            return {
                "strict_ids": strict_ids,
                "count": count,
                "places": places,
                "tolerance_km": tolerance,
                "area_drift_pct": drift,
                "vertices": len(candidate),
            }

        fail(r, label)
        if not is_size_error(r.status_code, r.text):
            print("\n  This is not a size rejection, so simplifying the polygon "
                  "would not help.")
            if "type is not supported" in r.text.lower():
                bad = r.text.split("type:")[-1].split('"')[0].strip().rstrip(",}")
                print(f"  Google does not accept '{bad}' as a filterable place type.")
                print(f"  Remove it from config/verticals/, or run:")
                print(f"      python scripts/probe.py --validate-types "
                      f"--vertical <name>")
            return None
        print("  Size rejection confirmed. Escalating simplification.")

    print("\n  Rejected at every tolerance. Record the error body above -- that is the finding.")
    return None


#: Google's `laundry` bucket is far wider than dry cleaners. A real Denver
#: catchment returned carpet cleaners, janitorial firms, commercial facility
#: services, and an appliance retailer -- none of which compete with a coin
#: laundry at all. These lists are a rough triage for the probe's report only;
#: the classification stage is what actually decides.
LAUNDROMAT_HINTS = ("laundromat", "laundry", "coin", "wash", "washateria", "spin")
DRY_CLEANER_HINTS = ("cleaner", "dry clean", "drycl", "martinizing", "alteration", "tailor")
NOT_COMPETITOR_HINTS = (
    "carpet", "janitor", "facility", "facilities", "maid", "housekeep", "crew",
    "restoration", "duct", "window", "upholstery", "appliance", "pressure",
    "commercial cleaning", "cleaning services", "corporate",
)


def triage(name: str) -> str:
    """Rough guess at what a `laundry`-typed business actually is.

    Order matters: "Advance Carpet Cleaning" contains no laundry word but does
    contain "carpet", and "Lamar Coin Laundry and Wash Dry Fold" contains both a
    laundry word and none of the exclusions. Checking exclusions first would
    misfile genuine laundromats whose names mention services.
    """
    low = name.lower()
    has_laundry = any(h in low for h in LAUNDROMAT_HINTS)
    if any(h in low for h in NOT_COMPETITOR_HINTS) and not has_laundry:
        return "not-a-competitor"
    if any(h in low for h in DRY_CLEANER_HINTS) and not has_laundry:
        return "dry-cleaner"
    if has_laundry:
        return "laundromat"
    return "unclear"


def details(place_ids: list[str], api_key: str, limit: int) -> list[dict]:
    """Enrich a shortlist and report what the data actually supports.

    Uses the STANDARD mask -- rating and review count, no review bodies. Review
    text is what pushes the call into the most expensive tier, and it feeds only
    the business-age guess. See docs/running-free.md.
    """
    hr(f"4. Place Details (first {limit} of {len(place_ids)})")
    mask = ("id,displayName,formattedAddress,location,primaryType,rating,"
            "userRatingCount,priceLevel,businessStatus,regularOpeningHours")
    out: list[dict] = []

    for pid in place_ids[:limit]:
        r = requests.get(
            DETAILS_URL.format(place_id=pid),
            headers={"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": mask},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            fail(r, f"details {pid}")
            continue
        d = r.json()
        name = d.get("displayName", {}).get("text", pid)
        hours = d.get("regularOpeningHours", {}) or {}
        guess = triage(name)
        out.append({
            "place_id": d.get("id", pid), "name": name,
            "location": d.get("location"), "rating": d.get("rating"),
            "user_rating_count": d.get("userRatingCount"),
            "price_level": d.get("priceLevel"),
            "open_24h": hours.get("openNow") is not None and len(hours.get("periods", [])) == 1,
            "triage": guess,
        })
        flag = "" if guess == "laundromat" else f"  <- {guess}"
        print(f"  {name[:44]:<44} {str(d.get('rating') or '-'):>4} "
              f"({str(d.get('userRatingCount') or 0):>4} rev) "
              f"{d.get('priceLevel') or '':<26}{flag}")

    from collections import Counter
    counts = Counter(d["triage"] for d in out)
    missing_rating = sum(1 for d in out if d["rating"] is None)
    missing_price = sum(1 for d in out if d["price_level"] is None)

    print(f"\n  {len(out)} enriched. Missing rating: {missing_rating}. "
          f"Missing price level: {missing_price}.")
    if missing_price > len(out) * 0.5:
        print(f"  Price level is absent on {missing_price} of {len(out)}. Any "
              f"vertical guidance that leans on price tier to separate formats "
              f"does not apply to this category.")

    print(f"\n  Rough triage by name:")
    for label in ("laundromat", "dry-cleaner", "not-a-competitor", "unclear"):
        if counts[label]:
            print(f"    {counts[label]:>3}  {label}")

    real = counts["laundromat"]
    if real < len(out):
        print(f"\n  Only ~{real} of {len(out)} look like actual self-service "
              f"laundromats. Google files")
        print(f"  carpet cleaners, janitorial firms, and commercial services "
              f"under `laundry` too.")
        print(f"  A category-level read scores all {len(out)} as direct "
              f"competitors, overstating")
        print(f"  supply several-fold. This is what the classification stage "
              f"exists to fix.")

    loudest = max(out, key=lambda d: d.get("user_rating_count") or 0)
    if loudest["triage"] != "laundromat":
        reads = ("does not read as a laundromat" if loudest["triage"] == "unclear"
                 else f"reads as a {loudest['triage']}")
        print(f"\n  Note: the most-reviewed business here "
              f"({loudest['name']}, {loudest['user_rating_count']} reviews)")
        print(f"  {reads}. Entrenchment would rank it the strongest competitor "
              f"in the market.")
    return out


def compare_type_filters(enriched: list[dict], strict_ids: list[str]) -> None:
    """Show WHICH records primary-type filtering removes, bucketed by triage.

    The count alone cannot say whether the narrower filter buys precision or
    loses real competitors. Dropping carpet cleaners is a win; dropping a
    laundromat is a silent understatement of supply, which is the direction of
    error that loses money.
    """
    hr("6. includedTypes vs includedPrimaryTypes -- what changed")
    kept = [e for e in enriched if e["place_id"] in set(strict_ids)]
    dropped = [e for e in enriched if e["place_id"] not in set(strict_ids)]

    from collections import Counter
    print(f"  Dropped by primary-type filtering ({len(dropped)}):")
    for e in sorted(dropped, key=lambda x: x["triage"]):
        print(f"    {e['triage']:<18} {e['name'][:44]}")

    lost = [e for e in dropped if e["triage"] == "laundromat"]
    noise = [e for e in dropped if e["triage"] in ("not-a-competitor", "unclear")]

    print(f"\n  Of the {len(dropped)} dropped: {len(noise)} were noise or "
          f"unclear, {len(lost)} looked like")
    print(f"  genuine laundromats.")
    print(f"  Kept set of {len(kept)}: " +
          ", ".join(f"{n} {t}" for t, n in Counter(e["triage"] for e in kept).most_common()))

    if lost:
        print(f"\n  Primary-type filtering would DROP {len(lost)} real "
              f"laundromat(s):")
        for e in lost:
            print(f"    {e['name']}")
        print(f"  That understates supply, which is the expensive direction of "
              f"error. Prefer")
        print(f"  the wider filter and let classification remove the noise.")
    else:
        print(f"\n  No genuine laundromat was dropped. Primary-type filtering "
              f"is free precision")
        print(f"  here: it removes {len(noise)} records that classification "
              f"would have had to")
        print(f"  reject anyway, at {len(dropped)} fewer Place Details calls "
              f"per deal.")


def route_matrix(origin: dict, enriched: list[dict], api_key: str) -> None:
    """One origin to every competitor in a single request. Validates the shape.

    The 625-element cap means a whole catchment fits in one call, so this is the
    cheapest stage in the pipeline per unit of information.
    """
    hr("5. Routes -- computeRouteMatrix")
    located = [e for e in enriched if e.get("location")]
    if not located:
        print("  No competitor coordinates to route to.")
        return

    body = {
        "origins": [{"waypoint": {"location": {"latLng": {
            "latitude": origin["lat"], "longitude": origin["lng"]}}}}],
        "destinations": [
            {"waypoint": {"location": {"latLng": {
                "latitude": e["location"]["latitude"],
                "longitude": e["location"]["longitude"]}}}}
            for e in located
        ],
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
    }
    r = requests.post(
        ROUTE_MATRIX_URL,
        headers={
            "X-Goog-Api-Key": api_key,
            "Content-Type": "application/json",
            "X-Goog-FieldMask": ("originIndex,destinationIndex,duration,"
                                 "distanceMeters,condition"),
        },
        json=body,
        timeout=TIMEOUT,
    )
    if r.status_code != 200:
        fail(r, "route matrix")
        return

    rows = r.json()
    print(f"  {len(located)} destinations in ONE request "
          f"(cap is 625 elements).\n")
    ok = [x for x in rows if x.get("condition") == "ROUTE_EXISTS"]
    for row in sorted(ok, key=lambda x: x.get("duration", "9999s"))[:8]:
        e = located[row["destinationIndex"]]
        minutes = int(row.get("duration", "0s").rstrip("s")) / 60
        km = row.get("distanceMeters", 0) / 1000
        straight = haversine_km(
            (origin["lat"], origin["lng"]),
            (e["location"]["latitude"], e["location"]["longitude"]),
        )
        detour = km / straight if straight else 0
        marker = "  <- barrier between here and there" if detour > 1.6 else ""
        print(f"  {e['name'][:36]:<36} {minutes:5.1f} min  {km:5.2f} km road / "
              f"{straight:5.2f} km direct = {detour:.2f}x{marker}")

    failed = len(rows) - len(ok)
    if failed:
        print(f"\n  {failed} destination(s) unroutable "
              f"(condition != ROUTE_EXISTS).")
    detours = [
        (row.get("distanceMeters", 0) / 1000) / haversine_km(
            (origin["lat"], origin["lng"]),
            (located[row["destinationIndex"]]["location"]["latitude"],
             located[row["destinationIndex"]]["location"]["longitude"]))
        for row in ok
        if haversine_km(
            (origin["lat"], origin["lng"]),
            (located[row["destinationIndex"]]["location"]["latitude"],
             located[row["destinationIndex"]]["location"]["longitude"])) > 0.05
    ]
    if detours:
        over = sum(1 for d in detours if d > 1.6)
        print(f"\n  Median detour ratio {sorted(detours)[len(detours)//2]:.2f}x. "
              f"{over} of {len(detours)} exceed 1.6x.")
        print("  Every one of those is a competitor that a radius-based analysis")
        print("  would count at full strength and drive time correctly discounts.")


CENSUS_TEST_URL = "https://api.census.gov/data/2023/acs/acs5"


#: Substrings that mark a genuine polygon-size rejection. Anything else that
#: comes back 400 is a different problem and must not trigger simplification --
#: retrying a bad request with worse geometry just burns free-tier calls.
SIZE_ERROR_MARKERS = (
    "too large", "too many", "vertices", "exceeds", "payload", "request size",
    "too complex", "limit",
)


def is_size_error(status_code: int, body: str) -> bool:
    """Is this rejection actually about the polygon being too big?"""
    if status_code == 413:
        return True
    if status_code != 400:
        return False
    low = body.lower()
    if "type is not supported" in low or "invalid_argument" in low and "type" in low:
        return False
    return any(marker in low for marker in SIZE_ERROR_MARKERS)


def google_hint(status: str, message: str) -> str:
    """Translate Google's error strings into the console action that fixes them.

    Google's setup errors are accurate but not actionable -- REQUEST_DENIED
    covers a missing billing account, an unenabled API, and an over-restricted
    key, which are three different fixes in three different console screens.
    """
    text = f"{status} {message}".lower()

    if "billing" in text:
        return ("      -> Billing is not enabled on the project. Console > Billing >\n"
                "         Link a billing account. The free tier still requires one.")
    if "not authorized" in text or "not been used" in text or "disabled" in text:
        return ("      -> The API is not enabled on this project. Console >\n"
                "         APIs & Services > Library, search it, click Enable. Note\n"
                "         'Places API (New)' is a SEPARATE entry from 'Places API'.")
    if "referer" in text or "ip" in text or "restrict" in text:
        return ("      -> The key's Application restriction is blocking a server-side\n"
                "         call. Console > Credentials > your key > Application\n"
                "         restrictions > None. Keep the API restrictions.")
    if "expired" in text or "invalid" in text or status == "INVALID_REQUEST":
        return ("      -> The key string looks wrong. Check for a trailing space or\n"
                "         a partial paste in .env.")
    if status == "OVER_QUERY_LIMIT":
        return ("      -> Free-tier allowance exhausted for this SKU this month, or\n"
                "         a per-minute quota was hit. Check Console > Quotas.")
    return ("      -> Check Console > APIs & Services > Credentials, and confirm\n"
            "         billing is active on the project.")


def check_keys() -> int:
    """Verify each key that is set, with one cheap call apiece.

    Reports per service rather than requiring all of them, so a key can be
    checked the moment it is obtained instead of waiting until the whole set is
    assembled. Every call here lands inside a free tier.
    """
    hr("Key check")
    results: dict[str, str] = {}

    token = os.environ.get("MAPBOX_ACCESS_TOKEN")
    if not token:
        results["Mapbox"] = "not set"
    else:
        r = requests.get(
            MAPBOX_URL.format(profile="driving", lng=-73.9196, lat=40.7433),
            params={"contours_minutes": 5, "polygons": "true", "access_token": token},
            timeout=TIMEOUT,
        )
        if r.status_code == 200 and r.json().get("features"):
            ring = r.json()["features"][0]["geometry"]["coordinates"][0]
            results["Mapbox"] = (
                f"OK -- 5-min test contour returned "
                f"{len(ring)} vertices, {polygon_area_sq_km(ring):.1f} km2"
            )
        else:
            results["Mapbox"] = f"FAILED -- HTTP {r.status_code}: {r.text[:200]}"

    census = os.environ.get("CENSUS_API_KEY")
    if not census:
        results["Census"] = "not set"
    else:
        r = requests.get(
            CENSUS_TEST_URL,
            params={"get": "NAME,B11001_001E", "for": "state:36", "key": census},
            timeout=TIMEOUT,
        )
        if r.status_code == 200:
            try:
                rows = r.json()
                name, households = rows[1][0], int(rows[1][1])
                results["Census"] = f"OK -- {name} has {households:,} households"
            except (ValueError, IndexError):
                results["Census"] = f"FAILED -- unexpected response: {r.text[:200]}"
        else:
            # An unactivated key returns 200 with an HTML error body, or a 403.
            results["Census"] = (
                f"FAILED -- HTTP {r.status_code}: {r.text[:200]}\n"
                "      If this mentions an invalid key, check your email and "
                "click the activation link."
            )

    google = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not google:
        results["Google"] = "not set"
    else:
        r = requests.get(
            GEOCODE_URL,
            params={"address": "1600 Amphitheatre Parkway, Mountain View, CA", "key": google},
            timeout=TIMEOUT,
        )
        data = r.json() if r.status_code == 200 else {}
        if data.get("status") == "OK":
            results["Google"] = "OK -- Geocoding responded (other APIs not tested)"
        else:
            status = data.get("status", str(r.status_code))
            message = data.get("error_message", r.text[:200])
            results["Google"] = f"FAILED -- {status}: {message}\n{google_hint(status, message)}"

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_key:
        results["Anthropic"] = "not set"
    else:
        try:
            from anthropic import Anthropic
        except ImportError:
            results["Anthropic"] = (
                "FAILED -- the `anthropic` package is not installed\n"
                "      -> pip install -e \".[reasoning]\""
            )
        else:
            # models.retrieve is free and validates both the credential and that
            # each configured model is actually available to this org -- a model
            # ID that is real but not enabled fails here rather than mid-run.
            from mcds.reasoning.client import RoutingConfig

            client = Anthropic(api_key=anthropic_key)
            routing = RoutingConfig.from_settings({})
            checked, failed = [], []
            for stage, model in sorted(set(routing.models.items()), key=lambda x: x[1]):
                if model in checked or model in [f[0] for f in failed]:
                    continue
                try:
                    client.models.retrieve(model)
                    checked.append(model)
                except Exception as exc:  # noqa: BLE001 - reporting, not handling
                    failed.append((model, str(exc)[:90]))
            if failed:
                detail = "; ".join(f"{m}: {e}" for m, e in failed)
                results["Anthropic"] = f"FAILED -- {detail}"
            else:
                results["Anthropic"] = (
                    f"OK -- key valid, {len(checked)} model(s) available: "
                    + ", ".join(checked)
                )

    for service, outcome in results.items():
        marker = {"OK": "  OK ", "no": "  -- ", "FA": "  !! "}[outcome[:2]]
        print(f"{marker}{service:<8} {outcome}")

    unset = [s for s, o in results.items() if o == "not set"]
    failed = [s for s, o in results.items() if o.startswith("FAILED")]
    print()
    if failed:
        print(f"Fix these before probing: {', '.join(failed)}")
        return 1
    if unset:
        print(f"Not set yet (fine if you have not got to them): {', '.join(unset)}")
    working = [s for s, o in results.items() if o.startswith("OK")]
    if working:
        print(f"Working: {', '.join(working)}")
    if "Google" in unset or "Mapbox" in unset:
        print("\nThe full probe needs both Mapbox and Google. Until then, try:")
        print("  mcds examples/sample_deal.yaml --dry-run --no-llm")
    elif "Anthropic" in unset:
        print("\nEverything the free path needs is working. Run a real deal at "
              "zero model cost:")
        print("  mcds <deal>.yaml --no-llm")
    else:
        print("\nAll services ready. Validate the data plumbing first at $0:")
        print("  mcds <deal>.yaml --no-llm")
        print("then drop --no-llm for the full analysis (~$2.40/deal).")
    return 0


def validate_types(vertical: dict, api_key: str) -> int:
    """Ask the API which of a vertical's place types it will actually accept.

    Google's Table A is the authority on filterable types and it does not match
    intuition -- `dry_cleaner` reads like a type and is not one. Rather than
    guess, submit the whole list and let the API name what it rejects; each
    rejection names exactly one type, so drop it and resubmit until the request
    is clean. Best case one call, worst case one per bad type, all inside the
    free tier.

    Uses a deliberately tiny area: this asks whether the FILTER is valid, not
    what is in any particular place.
    """
    hr(f"Validate place types -- {vertical['label']}")
    places = vertical["places"]
    candidates = list(dict.fromkeys(
        places.get("direct_types", []) + places.get("adjacent_types", [])
        + places.get("context_types", [])
    ))
    print(f"  Submitting {len(candidates)} types: {', '.join(candidates)}\n")

    # ~500m box near the Denver origin. Small enough to be trivial, large enough
    # to clear the API's 1,556 m2 minimum area.
    tiny = [[-105.045, 39.740], [-105.039, 39.740],
            [-105.039, 39.745], [-105.045, 39.745], [-105.045, 39.740]]

    rejected: list[str] = []
    remaining = list(candidates)
    for _ in range(len(candidates) + 1):
        if not remaining:
            break
        r = requests.post(
            AGGREGATE_URL,
            headers={"X-Goog-Api-Key": api_key, "Content-Type": "application/json"},
            json={
                "insights": ["INSIGHT_COUNT"],
                "filter": {
                    "locationFilter": {"customArea": {"polygon": to_google_polygon(tiny)}},
                    "typeFilter": {"includedTypes": remaining},
                },
            },
            timeout=TIMEOUT,
        )
        if r.status_code == 200:
            break
        if "type is not supported" not in r.text.lower():
            fail(r, "type validation")
            return 1
        bad = r.text.split("type:")[-1].split('"')[0].strip().rstrip(",}")
        if bad not in remaining:
            fail(r, "type validation (could not parse the rejected type)")
            return 1
        rejected.append(bad)
        remaining.remove(bad)
        print(f"  rejected: {bad}")

    print()
    for t in candidates:
        print(f"  {'OK  ' if t in remaining else '!!  '}{t}")

    if rejected:
        print(f"\n{len(rejected)} type(s) are not filterable: {', '.join(rejected)}")
        print("Remove them from the vertical config. Note that Google's type")
        print("hierarchy is implicit: filtering on a parent type already includes")
        print("its subtypes, so a rejected name is often already covered by one")
        print("of the accepted ones.")
        return 1

    print("\nAll types accepted.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("address", nargs="?",
                    help="omit when using --check")
    ap.add_argument("--validate-types", action="store_true",
                    help="ask the API which of this vertical's place types it "
                         "accepts, then exit. One cheap call per bad type.")
    ap.add_argument("--check", action="store_true",
                    help="verify whichever keys are set, then exit. Costs nothing "
                         "beyond three calls inside the free tiers.")
    ap.add_argument("--vertical", default="laundromat")
    ap.add_argument("--minutes", type=int, default=None)
    ap.add_argument("--profile", default=None,
                    help="mapbox profile; driving-traffic costs more but is traffic-aware")
    ap.add_argument("--details", type=int, default=0,
                    help="how many Place Details to fetch (costs money; default 0)")
    args = ap.parse_args()
    load_dotenv()

    if args.check:
        return check_keys()

    if args.validate_types:
        key = os.environ.get("GOOGLE_MAPS_API_KEY")
        if not key:
            print("GOOGLE_MAPS_API_KEY is not set.")
            return 2
        return validate_types(load_vertical(args.vertical), key)
    if not args.address:
        ap.error("an address is required unless you pass --check")

    google_key = os.environ.get("GOOGLE_MAPS_API_KEY")
    mapbox_token = os.environ.get("MAPBOX_ACCESS_TOKEN")
    missing = [n for n, v in
               [("GOOGLE_MAPS_API_KEY", google_key), ("MAPBOX_ACCESS_TOKEN", mapbox_token)]
               if not v]
    if missing:
        print(f"Missing environment variable(s): {', '.join(missing)}")
        return 2

    vertical = load_vertical(args.vertical)
    minutes = args.minutes or vertical["catchment"]["default_minutes"]
    profile = args.profile or vertical["catchment"]["profile"]
    types = vertical["places"]["direct_types"] + vertical["places"]["adjacent_types"]

    print(f"Probing: {args.address}")
    print(f"Vertical: {vertical['label']}  |  {minutes} min {profile}")
    print(f"Types: {', '.join(types)}")

    origin = geocode(args.address, google_key)
    if not origin:
        return 1
    ring = isochrone(origin["lat"], origin["lng"], minutes, profile, mapbox_token)
    if not ring:
        return 1
    result = aggregate(ring, types, google_key)
    if not result:
        return 1

    place_ids = [p.get("place", "").split("/")[-1] for p in result["places"]]
    enriched: list[dict] = []
    if args.details and place_ids:
        enriched = details(place_ids, google_key, args.details)
        if enriched:
            route_matrix(origin, enriched, google_key)
            if result.get("strict_ids") is not None:
                compare_type_filters(enriched, result["strict_ids"])

    hr("VERDICT")
    print(f"Q1  Polygon accepted at tolerance {result['tolerance_km']} km "
          f"({result['vertices']} vertices).")
    if result["tolerance_km"] == 0.0:
        print("    -> Mapbox rings go straight to Google. No simplification step needed.")
    else:
        print(f"    -> Simplification IS required, and it cost "
              f"{result['area_drift_pct']:+.2f}% of catchment area.")
        print("    -> That distortion must be recorded on the catchment and surfaced")
        print("       in the memo, because it moves the demand estimate.")

    print(f"\nQ2  {result['count']} matching places in the catchment; "
          f"{len(place_ids)} named.")
    if result["count"] > 100:
        print("    -> OVER the 100 cap. `census_complete: false` is the normal path")
        print("       for this kind of market, not an edge case. Consider splitting")
        print("       the catchment or narrowing types, and make the undercount")
        print("       warning far more prominent than it is now.")
    else:
        print("    -> Under the cap. Full census available for this catchment.")

    print("\nQ3  Check the Google Cloud console billing report now, filtered to the")
    print("    last hour, to see what these calls actually cost per SKU.")
    print(f"    This run made: 1 geocode, 1 isochrone, "
          f"{TOLERANCES_KM.index(result['tolerance_km']) + 1} Aggregate, "
          f"{min(args.details, len(place_ids))} Details.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
