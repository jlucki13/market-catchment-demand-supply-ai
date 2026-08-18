"""Competitor discovery via the Places Aggregate API, enriched via Place Details.

The obvious approach -- Nearby Search around the target -- does not work at this
scale. `places.searchNearby` returns at most 20 results and the new API offers
no pagination on it, so a catchment with 40 laundromats silently returns 20 and
the analysis proceeds as though that were the market.

The Places Aggregate API (`computeInsights`) is the right primitive instead: it
accepts a CUSTOM POLYGON -- the isochrone itself, not an approximating circle --
and returns:

  * INSIGHT_COUNT  : how many matching places the area holds, uncapped
  * INSIGHT_PLACES : their place IDs, but ONLY when the count is <= 100

That split is load-bearing. The count is always trustworthy; the named list is
not always complete. When count > 100 the Supply Index is a known undercount and
the scorecard says so rather than presenting a partial census as a full one.

  POST https://areainsights.googleapis.com/v1:computeInsights

Then Place Details on the shortlist for ratings, review counts, price level,
and hours.

Google Maps ToS, which constrains the whole design:
  * `place_id` may be stored indefinitely
  * coordinates may be cached at most 30 days
  * names, ratings, reviews, and photos may NOT be warehoused; they are fetched
    live per run and displayed with Google attribution

So this tool does not accumulate a competitor database. It persists place IDs
and OUR OWN derived judgments (substitutability, chain grouping, scores), and
re-fetches content each run. Every memo is a point-in-time artifact stamped with
its fetch time. `strip_transient()` below enforces this at the storage boundary.
"""

from __future__ import annotations

from typing import Iterable, Literal

AGGREGATE_URL = "https://areainsights.googleapis.com/v1:computeInsights"
DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"

#: Above this the Aggregate API returns a count but refuses to name the places.
MAX_NAMED_PLACES = 100

#: Minimum area the Aggregate API accepts, in square metres (~a small city block).
MIN_AREA_SQ_M = 1556.86

#: Field mask for the census pass. Field masks select Google's SKU tier, so the
#: cheap pass stays cheap and only the shortlist pays Enterprise+Atmosphere rates.
CENSUS_FIELD_MASK = "places.id,places.types,places.location"

#: Fields that Google's terms forbid warehousing. Stripped before persistence.
TRANSIENT_FIELDS = (
    "name",
    "formatted_address",
    "rating",
    "user_rating_count",
    "price_level",
    "business_status",
    "open_24h",
    "types",
    "primary_type",
)

InsightType = Literal["INSIGHT_COUNT", "INSIGHT_PLACES"]


def census_catchment(
    polygon_ring: list[list[float]],
    included_types: list[str],
    *,
    api_key: str,
    operating_status: tuple[str, ...] = ("OPERATING_STATUS_OPERATIONAL",),
) -> dict:
    """Count and, where possible, name every matching place inside the polygon.

    Returns {"count": int, "place_ids": list[str], "complete": bool} where
    `complete` is False when count > MAX_NAMED_PLACES.

    Request shape:
        {
          "insights": ["INSIGHT_COUNT", "INSIGHT_PLACES"],
          "filter": {
            "locationFilter": {"customArea": {"polygon": {"coordinates": [...]}}},
            "typeFilter": {"includedTypes": [...]},
            "operatingStatus": [...]
          }
        }

    The polygon ring must close (first coordinate equals last) and hold at least
    3 unique coordinates. Mapbox contours can carry hundreds of vertices; if the
    request is rejected for size, simplify with Douglas-Peucker and record the
    tolerance used, because simplification changes the area being measured.
    """
    raise NotImplementedError


def fetch_details(
    place_ids: Iterable[str],
    *,
    api_key: str,
    field_mask: str,
) -> list[dict]:
    """Enrich a shortlist into records matching schemas/competitor.schema.json.

    Notes for the build session:
      * `reviews` returns at most 5 per place, selected by relevance rather than
        date. The oldest of those five is a weak LOWER BOUND on business age and
        must be labelled as such wherever it is used -- see `earliest_review_at`
        in the competitor schema and the business-age caveat in the PRD.
      * `openingDate` exists but only populates for businesses opening in the
        FUTURE. It does not give you the age of an existing business.
      * `priceLevel` is frequently absent, especially for services. Absence is
        not "inexpensive"; leave it null.
      * Batch with concurrency but respect per-project QPM; a 429 here mid-run
        leaves a half-enriched catchment, so retry with backoff rather than
        dropping the place.
    """
    raise NotImplementedError


def detect_chains(competitors: list[dict]) -> dict[str, str]:
    """Map place_id -> chain group key for locations sharing an operator.

    Six locations of one brand are not six independent competitors, and the
    Supply Index damps them accordingly (see indices.CHAIN_DAMPING_EXPONENT).
    Normalised display name is the cheap signal; the classification stage can
    also return `chain_group`, which takes precedence because it can recognise a
    shared operator behind two different trade names.
    """
    raise NotImplementedError


def strip_transient(competitor: dict) -> dict:
    """Return a record safe to persist beyond the run.

    Drops every field Google's terms forbid warehousing, keeps `place_id` and
    our own derived judgments, and marks the record `persistable`. This is the
    only function that should ever write a competitor to durable storage.
    """
    kept = {k: v for k, v in competitor.items() if k not in TRANSIENT_FIELDS}
    kept["persistable"] = True
    return kept
