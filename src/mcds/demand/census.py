"""ACS demographics for the catchment, and the block groups it covers.

Two calls make this work:

1. TIGERweb (ArcGIS REST) to find which block groups intersect the isochrone.
     https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/
       tigerWMS_ACS{vintage}/MapServer/{layer}/query
   POST with geometry=<esriGeometryPolygon>, geometryType=esriGeometryPolygon,
   spatialRel=esriSpatialRelIntersects, returnGeometry=true, f=json.

2. The Census API for ACS estimates on those block groups.
     https://api.census.gov/data/{vintage}/acs/acs5
       ?get=NAME,B19001_001E,B19001_001M,...&for=block%20group:*
       &in=state:{ss}%20county:{ccc}%20tract:{tttttt}&key=...

Why ACS 5-year: it is the only ACS product published at block-group level.
1-year has better currency but stops at 65,000-population geographies, which no
drive-time catchment resembles. The cost is currency -- a 2023 5-year estimate
pools 2019-2023 -- and that vintage belongs in the memo, because a neighbourhood
that turned over in 2024 will not show it here.

Margins of error are requested alongside every estimate (the `_M` suffix) and
carried through the whole pipeline. Block-group MOEs are routinely +/-30%.
"""

from __future__ import annotations

from typing import Sequence

TIGERWEB_URL = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
    "tigerWMS_ACS{vintage}/MapServer/{layer}/query"
)
CENSUS_API_URL = "https://api.census.gov/data/{vintage}/{dataset}"

#: TIGERweb layer id for block groups in the ACS map services.
BLOCK_GROUP_LAYER = 8

#: The tables each demographic dimension reads from. Every vertical config
#: references these by name, so a new dimension means one entry here and one
#: line in a vertical config.
TABLES = {
    "household_income": "B19001",   # Household Income in the Past 12 Months
    "age": "B01001",                # Sex by Age (PERSON-level, not household)
    "tenure": "B25003",             # Tenure (owner vs renter occupied)
    "household_size": "B11016",     # Household Type by Household Size
    "households_total": "B11001",   # Household Type (total households)
}

#: ACS publishes margins of error at 90% confidence.
CONFIDENCE_LEVEL = 0.90


def block_groups_intersecting(
    polygon_ring: Sequence[Sequence[float]],
    *,
    vintage: int = 2023,
) -> list[dict]:
    """Block groups touching the catchment, with geometry and GEOID.

    Returns [{"geoid", "state", "county", "tract", "block_group", "geometry",
              "area_sq_km"}].

    The geometry comes back so interpolate.py can compute what fraction of each
    block group actually falls inside the catchment. A block group clipped by
    the isochrone edge must not contribute its whole population.
    """
    raise NotImplementedError


def fetch_acs(
    block_groups: Sequence[dict],
    dimensions: Sequence[str],
    *,
    api_key: str,
    vintage: int = 2023,
    dataset: str = "acs/acs5",
) -> dict:
    """ACS estimates and MOEs for the given block groups and dimensions.

    Notes for the build session:
      * The `in=` clause takes one state+county+tract at a time, so group the
        block groups by tract and issue one call per tract. A catchment spanning
        a county line needs calls to both.
      * Request every variable's `E` (estimate) and `M` (margin) together. A
        pipeline that fetches estimates now and margins later will drift.
      * B01001 is person-level. Applying a person-level share to a household
        count is an approximation, and indices.compute_demand_index emits a
        warning whenever a config does it. Do not silence that warning here.
      * Census returns the string "-666666666" for suppressed values. Map it to
        None; treating it as a number produces catastrophic garbage.
    """
    raise NotImplementedError


def to_bands(raw: dict, dimension: str) -> list[dict]:
    """Reshape raw ACS variables into the band list scoring/indices.py expects.

    Each band is {"band": str, "min": float|None, "max": float|None,
    "estimate": Estimate}. Income bands come straight from B19001's brackets;
    the top bracket ("$200,000 or more") has max=None and is handled as an
    open-topped tail by band_share.
    """
    raise NotImplementedError
