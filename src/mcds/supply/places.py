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

import re
from typing import Iterable, Literal

AGGREGATE_URL = "https://areainsights.googleapis.com/v1:computeInsights"
DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"

#: Above this the Aggregate API returns a count but refuses to name the places.
MAX_NAMED_PLACES = 100

#: Minimum area the Aggregate API accepts, in square metres (~a small city block).
MIN_AREA_SQ_M = 1556.86

#: Field masks select Google's SKU tier, and the tier decides both the price and
#: the monthly free allowance. This is the single biggest cost lever in the
#: system, so the masks are named and layered rather than assembled ad hoc.
#:
#: Billing takes the HIGHEST tier of any field requested -- one `reviews` field
#: pulls the whole call up to Enterprise. See docs/running-free.md.
CENSUS_FIELD_MASK = "places.id,places.types,places.location"

#: Identity only. Cheapest tier.
DETAIL_MASK_ESSENTIALS = "id,displayName,formattedAddress,location,types,primaryType"

#: Adds what entrenchment actually needs: rating and review COUNT (not review
#: text), price level, hours. This is the default -- it is the smallest mask
#: that still supports the Supply Index.
DETAIL_MASK_STANDARD = (
    DETAIL_MASK_ESSENTIALS
    + ",rating,userRatingCount,priceLevel,businessStatus,regularOpeningHours"
)

#: Adds review bodies, which exist solely to estimate business age from the
#: oldest of at most 5 relevance-ranked reviews -- the weakest signal in the
#: system, explicitly barred from being a headline flag on its own. It is also
#: the field that pulls the call into the most expensive tier with the smallest
#: free allowance. Opt in deliberately or not at all.
DETAIL_MASK_WITH_REVIEWS = DETAIL_MASK_STANDARD + ",reviews"

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


#: `includedTypes` matches a place's primary OR secondary types;
#: `includedPrimaryTypes` matches only the primary. Measured on a live Denver
#: laundromat catchment, the narrower filter cut 33 results to 21 and dropped
#: zero genuine laundromats -- the 12 it removed were carpet cleaners,
#: janitorial firms, commercial services, and two dry cleaners carrying
#: `laundry` as a secondary tag.
#:
#: Effect on the Supply Index against a hand-classified ground truth:
#:
#:     includedTypes        + priors           3.26x overstated
#:     includedPrimaryTypes + priors           1.96x overstated
#:     includedTypes        + classification   1.00x  (baseline)
#:     includedPrimaryTypes + classification   0.96x
#:
#: So `primary` is the default: it costs 4% understatement from the two dropped
#: dry cleaners and saves 36% of Place Details calls, which is the binding
#: free-tier constraint. It is NOT a substitute for classification -- five false
#: positives survived into the kept set of 21, including the most-reviewed
#: business in the catchment.
#:
#: Set `type_filter_mode: any` in a vertical config where recall matters more
#: than cost, or where a category's businesses commonly carry the relevant type
#: as a secondary tag.
DEFAULT_TYPE_FILTER_MODE = "primary"


def census_catchment(
    polygon_ring: list[list[float]],
    included_types: list[str],
    *,
    api_key: str,
    type_filter_mode: str = DEFAULT_TYPE_FILTER_MODE,
    operating_status: tuple[str, ...] = ("OPERATING_STATUS_OPERATIONAL",),
) -> dict:
    """Count and, where possible, name every matching place inside the polygon.

    Returns {"count": int, "place_ids": list[str], "complete": bool} where
    `complete` is False when count > MAX_NAMED_PLACES.

    Request shape (verified against the live API):
        {
          "insights": ["INSIGHT_COUNT", "INSIGHT_PLACES"],
          "filter": {
            "locationFilter": {"customArea": {"polygon": {"coordinates": [...]}}},
            "typeFilter": {"includedPrimaryTypes": [...]},
            "operatingStatus": ["OPERATING_STATUS_OPERATIONAL"]
          }
        }

    Note the polygon takes Google LatLng objects -- {"latitude": .., "longitude": ..}
    -- not GeoJSON [lng, lat] pairs. Swapping them yields a valid-looking request
    describing a polygon in the wrong hemisphere, which returns zero results
    rather than an error.

    `type_filter_mode` selects `includedPrimaryTypes` (default) or
    `includedTypes`; see DEFAULT_TYPE_FILTER_MODE for the measurement behind it.

    The polygon ring must close (first coordinate equals last) and hold at least
    3 unique coordinates. A 432-vertex Mapbox contour was accepted as-is against
    the live API, so simplification is a fallback rather than a normal step -- if
    a request is ever rejected for size, use catchment.simplify_ring and record
    the tolerance, because simplification changes the area being measured.
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


_CHAIN_NOISE = re.compile(r"(#\s*\d+|\bno\.?\s*\d+\b|\b\d+\b|[^\w\s])", re.IGNORECASE)
_CHAIN_STOPWORDS = {"the", "inc", "llc", "ltd", "co", "corp"}


def chain_key(name: str | None) -> str | None:
    """Normalise a trade name down to the stem shared by a chain's locations.

    "SuperClean Laundry #3" and "SuperClean Laundry #7" both reduce to
    "superclean laundry". Branch numbers, punctuation, and corporate suffixes
    are the noise; everything else is the brand.
    """
    if not name:
        return None
    stem = _CHAIN_NOISE.sub(" ", name.lower())
    words = [w for w in stem.split() if w not in _CHAIN_STOPWORDS]
    return " ".join(words) or None


def detect_chains(competitors: list[dict]) -> dict[str, str]:
    """Map place_id -> chain group key for locations sharing an operator.

    Six locations of one brand are not six independent competitors, and the
    Supply Index damps them accordingly (see indices.CHAIN_DAMPING_EXPONENT).
    Normalised display name is the cheap signal, and it is deterministic, so it
    runs without a model. The classification stage may also return
    `chain_group`, which takes precedence because a model can recognise a shared
    operator behind two different trade names.

    Only stems shared by two or more locations become groups; a unique name is
    not a chain of one.
    """
    by_stem: dict[str, list[str]] = {}
    for c in competitors:
        stem = chain_key(c.get("name"))
        if stem:
            by_stem.setdefault(stem, []).append(c["place_id"])

    return {
        place_id: stem
        for stem, ids in by_stem.items()
        if len(ids) > 1
        for place_id in ids
    }


def strip_transient(competitor: dict) -> dict:
    """Return a record safe to persist beyond the run.

    Drops every field Google's terms forbid warehousing, keeps `place_id` and
    our own derived judgments, and marks the record `persistable`. This is the
    only function that should ever write a competitor to durable storage.
    """
    kept = {k: v for k, v in competitor.items() if k not in TRANSIENT_FIELDS}
    kept["persistable"] = True
    return kept
