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

from typing import Literal, Sequence

ROUTE_MATRIX_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"

MAX_ELEMENTS = 625
MAX_ELEMENTS_TRAFFIC_OPTIMAL = 100

RoutingPreference = Literal["TRAFFIC_UNAWARE", "TRAFFIC_AWARE", "TRAFFIC_AWARE_OPTIMAL"]

#: Above this ratio of routed distance to straight-line distance, something
#: physical sits between the two points. Surfaced in the memo because it is the
#: clearest evidence that a radius-based read of this market would be wrong.
DETOUR_RATIO_FLAG = 1.6


def route_matrix(
    origin: tuple[float, float],
    destinations: Sequence[tuple[float, float]],
    *,
    api_key: str,
    routing_preference: RoutingPreference = "TRAFFIC_AWARE",
    departure_time: str | None = None,
) -> list[dict]:
    """Drive time and distance from one origin to many destinations.

    Returns one dict per destination: {"index", "minutes", "meters", "status"}.

    Notes for the build session:
      * The field mask must include `originIndex,destinationIndex,duration,
        distanceMeters,condition` -- responses arrive out of order and are keyed
        by those indices, never by position.
      * `condition: ROUTE_NOT_FOUND` is a real outcome (an island, a gated
        community, a bad geocode on the competitor's side). Return it as a null
        duration rather than dropping the row, so the Supply Index can count the
        competitor at its documented default and warn.
      * `departure_time` must be in the future for TRAFFIC_AWARE_OPTIMAL. For
        the peak/off-peak comparison, pick the next occurrence of the configured
        weekday and hour.
    """
    raise NotImplementedError


def detour_ratio(routed_meters: float | None, straight_line_km: float) -> float | None:
    """How much further the road is than the crow flies."""
    if routed_meters is None or straight_line_km <= 0:
        return None
    return (routed_meters / 1000.0) / straight_line_km
