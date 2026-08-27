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
from mcds.config import load_dotenv, load_vertical

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
MAPBOX_URL = "https://api.mapbox.com/isochrone/v1/mapbox/{profile}/{lng},{lat}"
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
            return {
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


def details(place_ids: list[str], api_key: str, limit: int) -> None:
    hr(f"4. Place Details (first {limit})")
    mask = ("id,displayName,primaryType,rating,userRatingCount,priceLevel,"
            "businessStatus,regularOpeningHours,reviews")
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
        reviews = d.get("reviews", []) or []
        oldest = min((rv.get("publishTime", "") for rv in reviews), default=None)
        print(f"\n  {d.get('displayName', {}).get('text', pid)}")
        print(f"    type={d.get('primaryType')}  rating={d.get('rating')} "
              f"({d.get('userRatingCount')} reviews)  price={d.get('priceLevel')}")
        print(f"    reviews returned: {len(reviews)} (API caps at 5)")
        print(f"    oldest review publishTime: {oldest or 'none'}")
        print("    ^ this is the ONLY business-age signal available, and it is a "
              "loose lower bound")


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
    if args.details and place_ids:
        details(place_ids, google_key, args.details)

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
