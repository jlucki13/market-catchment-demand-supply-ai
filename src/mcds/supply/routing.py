"""Drive times from the target to every competitor, via the Routes API.

Straight-line distance is the wrong metric and it fails asymmetrically: it never
overstates how far away a competitor is, only understates it. A competitor
1.2 miles away across a river with the nearest bridge two miles north is, for
every customer, a competitor four miles away. Radius-based analysis counts it at
full strength; this counts it at its real one.

  POST https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix

Limits that shape the batching:
  * origins x destinations <= 625 elements per request
  * that drops to 100 with routingPreference TRAFFIC_AWARE_OPTIMAL
  * TRANSIT is not supported by computeRouteMatrix

With one origin (the target) the 625 cap means a single request covers any
realistic catchment. The reason to batch at all is TRAFFIC_AWARE_OPTIMAL, which
is worth it only for the peak/off-peak spot check on a small sample.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal, Sequence

from ..http import post_json

ROUTE_MATRIX_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"

MAX_ELEMENTS = 625
MAX_ELEMENTS_TRAFFIC_OPTIMAL = 100

RoutingPreference = Literal["TRAFFIC_UNAWARE", "TRAFFIC_AWARE", "TRAFFIC_AWARE_OPTIMAL"]

#: Above this ratio of routed distance to straight-line distance, something
#: physical sits between the two points. Surfaced in the memo because it is the
#: clearest evidence that a radius-based read of this market would be wrong.
DETOUR_RATIO_FLAG = 1.6


def next_weekday_at(hour: int, weekday: int = 1) -> str:
    """RFC3339 timestamp for the next occurrence of a weekday at an hour, in UTC.

    TRAFFIC_AWARE_OPTIMAL requires a future departure time. Tuesday is the
    default because Monday and Friday traffic are atypical of a normal week.
    """
    now = datetime.now(timezone.utc)
    days_ahead = (weekday - now.weekday()) % 7
    target = (now + timedelta(days=days_ahead)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    if target <= now:
        target += timedelta(days=7)
    return target.isoformat().replace("+00:00", "Z")


def route_matrix(
    origin: tuple[float, float],
    destinations: Sequence[tuple[float, float]],
    *,
    api_key: str,
    routing_preference: RoutingPreference = "TRAFFIC_AWARE",
    departure_time: str | None = None,
) -> list[dict]:
    """Drive time and distance from one origin to many destinations.

    Returns one dict per destination in INPUT order:
    {"index", "minutes", "meters", "condition"}. Rows arrive out of order and
    keyed by index, never by position, so they are reordered here -- reading them
    positionally silently attributes every drive time to the wrong competitor.

    An unroutable destination (an island, a gated community, a bad geocode on
    the competitor's side) returns a null duration rather than being dropped, so
    the Supply Index can score it at its documented default and warn.
    """
    if not destinations:
        return []

    cap = (MAX_ELEMENTS_TRAFFIC_OPTIMAL
           if routing_preference == "TRAFFIC_AWARE_OPTIMAL" else MAX_ELEMENTS)
    results: list[dict] = [
        {"index": i, "minutes": None, "meters": None, "condition": "NOT_REQUESTED"}
        for i in range(len(destinations))
    ]

    for start in range(0, len(destinations), cap):
        chunk = destinations[start:start + cap]
        body = {
            "origins": [{"waypoint": {"location": {"latLng": {
                "latitude": origin[0], "longitude": origin[1]}}}}],
            "destinations": [
                {"waypoint": {"location": {"latLng": {
                    "latitude": lat, "longitude": lng}}}}
                for lat, lng in chunk
            ],
            "travelMode": "DRIVE",
            "routingPreference": routing_preference,
        }
        if departure_time:
            body["departureTime"] = departure_time

        rows = post_json(
            ROUTE_MATRIX_URL, service="Routes",
            headers={
                "X-Goog-Api-Key": api_key,
                "Content-Type": "application/json",
                "X-Goog-FieldMask": ("originIndex,destinationIndex,duration,"
                                     "distanceMeters,condition"),
            },
            json=body,
        )
        for row in rows:
            i = start + row.get("destinationIndex", 0)
            condition = row.get("condition", "ROUTE_NOT_FOUND")
            duration = row.get("duration")
            results[i] = {
                "index": i,
                "minutes": (
                    round(int(str(duration).rstrip("s")) / 60, 2)
                    if duration and condition == "ROUTE_EXISTS" else None
                ),
                "meters": row.get("distanceMeters") if condition == "ROUTE_EXISTS" else None,
                "condition": condition,
            }

    return results


def attach_drive_times(
    competitors: list[dict],
    origin: tuple[float, float],
    *,
    api_key: str,
    routing_preference: RoutingPreference = "TRAFFIC_AWARE",
) -> list[str]:
    """Route the whole catchment in one call and write the results onto records.

    Also computes the detour ratio, which is the clearest evidence a memo can
    show that a radius-based read of this market would be wrong: a competitor
    1.2 miles away across a river with the nearest bridge two miles north is,
    for every customer, four miles away.
    """
    from ..scoring.geography import haversine_km

    located = [c for c in competitors if c.get("location")]
    if not located:
        return ["No competitor had coordinates, so no drive times were computed."]

    rows = route_matrix(
        origin,
        [(c["location"]["lat"], c["location"]["lng"]) for c in located],
        api_key=api_key, routing_preference=routing_preference,
    )

    unroutable = 0
    flagged: list[str] = []
    for c, row in zip(located, rows):
        straight = haversine_km(origin, (c["location"]["lat"], c["location"]["lng"]))
        c["drive_time_minutes"] = row["minutes"]
        c["drive_distance_m"] = row["meters"]
        c["straight_line_km"] = round(straight, 3)
        c["detour_ratio"] = detour_ratio(row["meters"], straight)
        if row["minutes"] is None:
            unroutable += 1
        elif (c["detour_ratio"] or 0) > DETOUR_RATIO_FLAG:
            flagged.append(c.get("name") or c["place_id"])

    warnings: list[str] = []
    if unroutable:
        warnings.append(
            f"{unroutable} competitor(s) could not be routed to and were scored "
            f"at the edge-of-catchment accessibility default."
        )
    if flagged:
        warnings.append(
            f"{len(flagged)} competitor(s) sit behind a physical barrier -- road "
            f"distance exceeds {DETOUR_RATIO_FLAG}x the straight line: "
            f"{', '.join(flagged[:5])}"
            f"{'...' if len(flagged) > 5 else ''}. A radius-based analysis would "
            f"count these at full strength."
        )
    return warnings


def detour_ratio(routed_meters: float | None, straight_line_km: float) -> float | None:
    """How much further the road is than the crow flies."""
    if routed_meters is None or straight_line_km <= 0:
        return None
    return (routed_meters / 1000.0) / straight_line_km
