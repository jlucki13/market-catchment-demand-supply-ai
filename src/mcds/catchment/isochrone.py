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

from typing import Literal

Profile = Literal["driving", "driving-traffic", "walking", "cycling"]

MAPBOX_ISOCHRONE_URL = "https://api.mapbox.com/isochrone/v1/mapbox/{profile}/{lng},{lat}"
MAX_CONTOUR_MINUTES = 60
MAX_CONTOURS_PER_REQUEST = 4
RATE_LIMIT_PER_MINUTE = 300

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
) -> dict:
    """Return a catchment dict conforming to schemas/catchment.schema.json.

    Raises ValueError when `minutes` exceeds MAX_CONTOUR_MINUTES, rather than
    letting Mapbox silently return a 60-minute contour for a 90-minute request.

    Implementation notes for the build session:
      * The response is a FeatureCollection; take the Polygon geometry of the
        contour whose `contour` property equals `minutes`.
      * Mapbox occasionally returns a MultiPolygon for fragmented catchments.
        Keep the largest ring and record the discarded area in a warning; do not
        silently drop it.
      * Set `traffic_aware` on the result from whether `profile` is
        `driving-traffic`. The memo branches on this field.
    """
    raise NotImplementedError


def polygon_area_sq_km(ring: list[list[float]]) -> float:
    """Spherical excess area of a closed GeoJSON ring, in square kilometres.

    Used for sanity-checking a contour (an 8-minute drive catchment covering
    400 km2 means the geocode landed on a highway) and for areal interpolation
    fallback in demand/interpolate.py.
    """
    raise NotImplementedError


def spot_check_traffic_divergence(
    origin: tuple[float, float],
    sample_destinations: list[tuple[float, float]],
    *,
    api_key: str,
) -> dict:
    """Compare free-flow contour assumptions against real routed times.

    Routes a small sample at a weekday peak and off-peak departure time and
    returns the ratio between them, so the memo can state how much the free-flow
    catchment is likely overstating reach. A handful of destinations is enough
    to bound the error and costs a fraction of re-routing everything.
    """
    raise NotImplementedError
