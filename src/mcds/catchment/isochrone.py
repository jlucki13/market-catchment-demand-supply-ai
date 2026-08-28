"""Drive-time catchment polygons from the Mapbox Isochrone API.

Why not a radius circle: a two-mile circle drawn around a business on the far
side of a river, a rail line, or a one-way grid includes thousands of households
that cannot practically reach it. The catchment is the whole foundation of both
the demand and supply numbers, so getting it wrong biases everything downstream
in the same direction at once.

Why not Google: Google Maps Platform has no isochrone endpoint. This is a hard
third-party dependency, and its limits are load-bearing:

  * one origin coordinate per request
  * at most 4 contours per request
  * 60 minutes is the maximum contour, 100 km the maximum distance
  * 300 requests/minute on standard plans
  * the `driving` profile is free-flow: NO live traffic

That last one matters most. A free-flow 10-minute contour is not a 10-minute
contour at 8am. The pipeline compensates by spot-checking a handful of
destinations against the Routes API at peak and off-peak (see supply/routing.py)
and writing the divergence into the memo rather than quietly ignoring it.

Endpoint:
  GET https://api.mapbox.com/isochrone/v1/mapbox/{profile}/{lng},{lat}
      ?contours_minutes=8&polygons=true&denoise=1.0&access_token=...
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Literal, Sequence

from ..http import ApiError, get_json

Profile = Literal["driving", "driving-traffic", "walking", "cycling"]

MAPBOX_ISOCHRONE_URL = "https://api.mapbox.com/isochrone/v1/mapbox/{profile}/{lng},{lat}"
MAX_CONTOUR_MINUTES = 60
MAX_CONTOURS_PER_REQUEST = 4
RATE_LIMIT_PER_MINUTE = 300

EARTH_RADIUS_KM = 6371.0088

#: Mapbox `denoise` drops small disconnected islands from the contour. Some are
#: real (a pocket reachable only by one road), so this is deliberately gentle;
#: aggressive denoising silently deletes genuine catchment area.
DEFAULT_DENOISE = 1.0


def fetch_isochrone(
    lat: float,
    lng: float,
    minutes: int,
    *,
    profile: Profile = "driving",
    access_token: str,
    denoise: float = DEFAULT_DENOISE,
    formatted_address: str | None = None,
    geocode_quality: str | None = None,
) -> tuple[dict, list[str]]:
    """Return (catchment, warnings), the catchment conforming to its schema.

    Raises ValueError above MAX_CONTOUR_MINUTES rather than letting Mapbox
    silently return a 60-minute contour for a 90-minute request.
    """
    if minutes > MAX_CONTOUR_MINUTES:
        raise ValueError(
            f"Mapbox caps a contour at {MAX_CONTOUR_MINUTES} minutes; "
            f"{minutes} was requested. Split the analysis or use a nearer "
            f"catchment rather than accepting a silently truncated one."
        )

    data = get_json(
        MAPBOX_ISOCHRONE_URL.format(profile=profile, lng=lng, lat=lat),
        service="Mapbox Isochrone",
        params={
            "contours_minutes": minutes,
            "polygons": "true",
            "denoise": denoise,
            "access_token": access_token,
        },
    )
    features = data.get("features") or []
    if not features:
        raise ApiError("Mapbox Isochrone", 200, "no contour returned for this origin")

    geometry = features[0]["geometry"]
    warnings: list[str] = []

    if geometry["type"] == "MultiPolygon":
        # A fragmented catchment is real -- a pocket reachable only by one road
        # -- so the discarded area is reported rather than silently dropped.
        rings = [poly[0] for poly in geometry["coordinates"]]
        areas = [polygon_area_sq_km(r) for r in rings]
        ring = rings[areas.index(max(areas))]
        discarded = sum(areas) - max(areas)
        warnings.append(
            f"The isochrone came back in {len(rings)} disconnected pieces. Only "
            f"the largest ({max(areas):.1f} km2) was analysed; {discarded:.1f} "
            f"km2 of reachable area in {len(rings) - 1} other piece(s) was "
            f"excluded, so demand and supply are both understated."
        )
    else:
        ring = geometry["coordinates"][0]

    if ring[0] != ring[-1]:
        ring = list(ring) + [ring[0]]

    traffic_aware = profile == "driving-traffic"
    if not traffic_aware:
        warnings.append(
            "The catchment was generated from a free-flow driving profile with "
            "no live traffic. At peak hours the real reach is smaller than this "
            "polygon, so both the household count and the competitor set are "
            "upper bounds."
        )

    catchment = {
        "origin": {"lat": lat, "lng": lng},
        "minutes": minutes,
        "profile": profile,
        "polygon": {"type": "Polygon", "coordinates": [ring]},
        "area_sq_km": round(polygon_area_sq_km(ring), 2),
        "provider": "mapbox",
        "traffic_aware": traffic_aware,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if formatted_address:
        catchment["origin"]["formatted_address"] = formatted_address
    if geocode_quality:
        catchment["origin"]["geocode_quality"] = geocode_quality
    return catchment, warnings


GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"

#: Below these precisions the origin is a guess, and a catchment drawn from a
#: guessed origin is wrong in every direction at once.
ACCEPTABLE_GEOCODE = ("ROOFTOP", "RANGE_INTERPOLATED")


def geocode(address: str, *, api_key: str) -> dict:
    """Resolve an address to coordinates, refusing to proceed on a vague match.

    Google returns `status` inside a 200 body, so a failure here is not an HTTP
    error and must be checked explicitly.
    """
    data = get_json(
        GEOCODE_URL, service="Google Geocoding",
        params={"address": address, "key": api_key},
    )
    if data.get("status") != "OK" or not data.get("results"):
        raise ApiError(
            "Google Geocoding", 200,
            f"status={data.get('status')} {data.get('error_message', '')}",
        )

    top = data["results"][0]
    location = top["geometry"]["location"]
    quality = top["geometry"].get("location_type", "APPROXIMATE")
    if quality not in ACCEPTABLE_GEOCODE:
        raise ApiError(
            "Google Geocoding", 200,
            f"'{address}' resolved only to {quality} precision "
            f"({top['formatted_address']}). A catchment drawn from an "
            f"approximate origin is unreliable in every direction; supply a "
            f"more specific address.",
        )
    return {
        "lat": location["lat"], "lng": location["lng"],
        "formatted_address": top["formatted_address"], "quality": quality,
    }


def polygon_area_sq_km(ring: Sequence[Sequence[float]]) -> float:
    """Spherical excess area of a closed GeoJSON ring, in square kilometres.

    Used for sanity-checking a contour -- an 8-minute drive catchment covering
    400 km2 means the geocode landed on a highway -- and as the areal
    interpolation fallback in demand/interpolate.py.

    Planar shoelace on raw degrees would be wrong by the cosine of the latitude,
    which is a 24% error at 40 degrees north. This uses the spherical formula so
    the number is right anywhere.
    """
    if len(ring) < 4:
        return 0.0
    total = 0.0
    for (lng1, lat1), (lng2, lat2) in zip(ring, ring[1:]):
        total += math.radians(lng2 - lng1) * (
            2 + math.sin(math.radians(lat1)) + math.sin(math.radians(lat2))
        )
    return abs(total * EARTH_RADIUS_KM * EARTH_RADIUS_KM / 2.0)


def _perpendicular_km(
    point: Sequence[float],
    start: Sequence[float],
    end: Sequence[float],
) -> float:
    """Distance from `point` to the segment start-end, in kilometres.

    Works in a local equirectangular projection around the segment. Over the
    few kilometres a catchment spans, the distortion is far below the
    simplification tolerances this feeds.
    """
    mid_lat = math.radians((start[1] + end[1]) / 2)
    kx = 111.320 * math.cos(mid_lat)
    ky = 110.574

    px, py = (point[0] - start[0]) * kx, (point[1] - start[1]) * ky
    ex, ey = (end[0] - start[0]) * kx, (end[1] - start[1]) * ky

    seg_sq = ex * ex + ey * ey
    if seg_sq == 0:
        return math.hypot(px, py)
    t = max(0.0, min(1.0, (px * ex + py * ey) / seg_sq))
    return math.hypot(px - t * ex, py - t * ey)


def _douglas_peucker(
    points: Sequence[Sequence[float]], tolerance_km: float
) -> list[list[float]]:
    """Classic Douglas-Peucker on an open polyline."""
    if len(points) < 3:
        return [list(p) for p in points]

    worst_index, worst_distance = 0, 0.0
    for i in range(1, len(points) - 1):
        d = _perpendicular_km(points[i], points[0], points[-1])
        if d > worst_distance:
            worst_index, worst_distance = i, d

    if worst_distance <= tolerance_km:
        return [list(points[0]), list(points[-1])]

    left = _douglas_peucker(points[: worst_index + 1], tolerance_km)
    right = _douglas_peucker(points[worst_index:], tolerance_km)
    return left[:-1] + right


def simplify_ring(
    ring: Sequence[Sequence[float]], tolerance_km: float
) -> list[list[float]]:
    """Reduce a closed ring's vertex count, keeping its shape within a tolerance.

    Mapbox contours carry hundreds of vertices. Where a downstream API rejects a
    polygon for size, this is the fix -- but simplification CHANGES THE AREA
    BEING MEASURED, so callers must record the tolerance used and compare areas
    before and after. A catchment quietly shrunk by 8% to fit a request limit is
    a silently wrong demand estimate.

    The ring is split at the vertex farthest from its first point and each half
    simplified independently. Running Douglas-Peucker straight down a closed
    sequence collapses detail near the seam, because the first and last points
    are the same point and anchor nothing.
    """
    if len(ring) < 5:
        return [list(p) for p in ring]

    open_ring = list(ring[:-1])
    anchor = open_ring[0]
    far_index = max(
        range(1, len(open_ring)),
        key=lambda i: _perpendicular_km(open_ring[i], anchor, anchor) or
        math.dist(open_ring[i], anchor),
    )

    first = _douglas_peucker(open_ring[: far_index + 1], tolerance_km)
    second = _douglas_peucker(open_ring[far_index:] + [anchor], tolerance_km)
    combined = first[:-1] + second[:-1]

    if len(combined) < 3:
        return [list(p) for p in ring]
    return combined + [list(combined[0])]


def spot_check_traffic_divergence(
    origin: tuple[float, float],
    sample_destinations: list[tuple[float, float]],
    *,
    api_key: str,
    sample_size: int = 8,
) -> dict:
    """Bound how much a free-flow catchment overstates peak reach.

    Routes a small sample at a weekday peak and an off-peak departure and
    returns the ratio. A handful of destinations bounds the error at a fraction
    of the cost of re-routing everything, which is the point: this is a
    correction factor for the memo, not a replacement catchment.
    """
    from ..supply.routing import route_matrix, next_weekday_at

    sample = sample_destinations[:sample_size]
    if not sample:
        return {"ratio": None, "note": "no destinations to sample"}

    peak = route_matrix(
        origin, sample, api_key=api_key,
        routing_preference="TRAFFIC_AWARE_OPTIMAL",
        departure_time=next_weekday_at(hour=8),
    )
    off = route_matrix(
        origin, sample, api_key=api_key,
        routing_preference="TRAFFIC_AWARE_OPTIMAL",
        departure_time=next_weekday_at(hour=14),
    )
    pairs = [
        (p["minutes"], o["minutes"])
        for p, o in zip(peak, off)
        if p.get("minutes") and o.get("minutes")
    ]
    if not pairs:
        return {"ratio": None, "note": "no routable sample destinations"}

    ratios = sorted(p / o for p, o in pairs)
    median = ratios[len(ratios) // 2]
    return {
        "ratio": round(median, 3),
        "sample_size": len(pairs),
        "note": (
            f"At a weekday 8am departure, trips in this catchment take "
            f"{median:.2f}x as long as off-peak (median of {len(pairs)} "
            f"sampled destinations). The free-flow contour therefore overstates "
            f"peak reach by roughly that factor."
        ),
    }
