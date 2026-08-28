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

import json
from typing import Sequence

from ..http import ApiError, get_json, post_json
from ..moe import Estimate, sum_estimates

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

#: ACS band structures, transcribed from the published table shells.
#:
#: Income bands carry their real numeric edges so scoring/indices.band_share can
#: apportion a filter that cuts mid-band. The top band is open-topped (max=None)
#: and is handled as all-or-nothing, because there is no honest way to split an
#: unbounded tail.
INCOME_BANDS_B19001 = [
    ("002", "Less than $10,000", None, 9999),
    ("003", "$10,000 to $14,999", 10000, 14999),
    ("004", "$15,000 to $19,999", 15000, 19999),
    ("005", "$20,000 to $24,999", 20000, 24999),
    ("006", "$25,000 to $29,999", 25000, 29999),
    ("007", "$30,000 to $34,999", 30000, 34999),
    ("008", "$35,000 to $39,999", 35000, 39999),
    ("009", "$40,000 to $44,999", 40000, 44999),
    ("010", "$45,000 to $49,999", 45000, 49999),
    ("011", "$50,000 to $59,999", 50000, 59999),
    ("012", "$60,000 to $74,999", 60000, 74999),
    ("013", "$75,000 to $99,999", 75000, 99999),
    ("014", "$100,000 to $124,999", 100000, 124999),
    ("015", "$125,000 to $149,999", 125000, 149999),
    ("016", "$150,000 to $199,999", 150000, 199999),
    ("017", "$200,000 or more", 200000, None),
]

TENURE_BANDS_B25003 = [
    ("002", "owner_occupied", None, None),
    ("003", "renter_occupied", None, None),
]

#: B01001 is Sex by Age. Each age group appears twice -- male, then female --
#: so both are summed. This table is PERSON-level; applying its shares to a
#: household count is an approximation that compute_demand_index warns about.
AGE_GROUPS_B01001 = [
    ("003", "027", "Under 5", 0, 4),      ("004", "028", "5 to 9", 5, 9),
    ("005", "029", "10 to 14", 10, 14),   ("006", "030", "15 to 17", 15, 17),
    ("007", "031", "18 and 19", 18, 19),  ("008", "032", "20", 20, 20),
    ("009", "033", "21", 21, 21),         ("010", "034", "22 to 24", 22, 24),
    ("011", "035", "25 to 29", 25, 29),   ("012", "036", "30 to 34", 30, 34),
    ("013", "037", "35 to 39", 35, 39),   ("014", "038", "40 to 44", 40, 44),
    ("015", "039", "45 to 49", 45, 49),   ("016", "040", "50 to 54", 50, 54),
    ("017", "041", "55 to 59", 55, 59),   ("018", "042", "60 and 61", 60, 61),
    ("019", "043", "62 to 64", 62, 64),   ("020", "044", "65 and 66", 65, 66),
    ("021", "045", "67 to 69", 67, 69),   ("022", "046", "70 to 74", 70, 74),
    ("023", "047", "75 to 79", 75, 79),   ("024", "048", "80 to 84", 80, 84),
    ("025", "049", "85 and over", 85, None),
]

#: Census returns this sentinel for a suppressed or unavailable value. Treating
#: it as a number produces catastrophic garbage, so it maps to None.
SUPPRESSED = {"-666666666", "-999999999", "-888888888", "*", "-", "N", "null"}

#: The API rejects requests above roughly this many variables. B01001 alone
#: needs 92 columns with margins, so requests are chunked.
MAX_VARIABLES_PER_REQUEST = 45



def block_groups_intersecting(
    polygon_ring: Sequence[Sequence[float]],
    *,
    vintage: int = 2023,
) -> list[dict]:
    """Block groups touching the catchment, with geometry and GEOID.

    Returns [{"geoid", "state", "county", "tract", "block_group", "ring",
              "area_sq_km"}].

    The geometry comes back so interpolate.py can compute what fraction of each
    block group actually falls inside the catchment. A block group clipped by
    the isochrone edge must not contribute its whole population.
    """
    envelope = {
        "rings": [[[p[0], p[1]] for p in polygon_ring]],
        "spatialReference": {"wkid": 4326},
    }
    data = post_json(
        TIGERWEB_URL.format(vintage=vintage, layer=BLOCK_GROUP_LAYER),
        service="TIGERweb",
        data={
            "geometry": json.dumps(envelope),
            "geometryType": "esriGeometryPolygon",
            "inSR": "4326",
            "outSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "GEOID,STATE,COUNTY,TRACT,BLKGRP",
            "returnGeometry": "true",
            "f": "json",
        },
    )
    if "error" in data:
        raise ApiError("TIGERweb", 200, json.dumps(data["error"])[:400])

    out: list[dict] = []
    for feature in data.get("features", []):
        attrs = feature.get("attributes", {})
        rings = (feature.get("geometry") or {}).get("rings") or []
        if not rings:
            continue
        # Esri rings are [x, y] = [lng, lat], the same order as GeoJSON.
        ring = [[float(x), float(y)] for x, y in max(rings, key=len)]
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        out.append({
            "geoid": attrs.get("GEOID"),
            "state": attrs.get("STATE"),
            "county": attrs.get("COUNTY"),
            "tract": attrs.get("TRACT"),
            "block_group": attrs.get("BLKGRP"),
            "ring": ring,
        })
    return out


def variables_for(dimension: str) -> list[str]:
    """Every ACS variable code a dimension needs, estimates and margins together.

    Fetched together on purpose: a pipeline that pulls estimates now and margins
    later will drift, and an interval assembled from mismatched vintages is
    worse than no interval.
    """
    table = TABLES[dimension]
    if dimension == "household_income":
        suffixes = [b[0] for b in INCOME_BANDS_B19001] + ["001"]
    elif dimension == "tenure":
        suffixes = [b[0] for b in TENURE_BANDS_B25003] + ["001"]
    elif dimension == "age":
        suffixes = [s for g in AGE_GROUPS_B01001 for s in (g[0], g[1])] + ["001"]
    else:
        suffixes = ["001"]
    return [f"{table}_{s}{kind}" for s in suffixes for kind in ("E", "M")]


def _to_number(raw: str | None) -> float | None:
    if raw is None or str(raw).strip() in SUPPRESSED:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    # A negative margin is Census's way of saying the estimate is controlled to
    # an independent total and has no sampling error.
    return None if value < 0 else value


def fetch_acs(
    block_groups: Sequence[dict],
    dimensions: Sequence[str],
    *,
    api_key: str,
    vintage: int = 2023,
    dataset: str = "acs/acs5",
) -> dict:
    """ACS estimates and MOEs, keyed by GEOID then variable code.

    One request per county per variable chunk. Grouping by county rather than by
    tract keeps a 30-block-group catchment to a couple of calls instead of a
    dozen, and a catchment spanning a county line is handled by the grouping
    rather than as a special case.
    """
    wanted = {bg["geoid"] for bg in block_groups}
    by_county: dict[tuple[str, str], list[dict]] = {}
    for bg in block_groups:
        by_county.setdefault((bg["state"], bg["county"]), []).append(bg)

    variables: list[str] = []
    for dim in dimensions:
        variables.extend(v for v in variables_for(dim) if v not in variables)

    out: dict[str, dict[str, float | None]] = {}
    for (state, county), _ in by_county.items():
        for start in range(0, len(variables), MAX_VARIABLES_PER_REQUEST):
            chunk = variables[start:start + MAX_VARIABLES_PER_REQUEST]
            rows = get_json(
                CENSUS_API_URL.format(vintage=vintage, dataset=dataset),
                service="Census ACS",
                params={
                    "get": ",".join(chunk),
                    "for": "block group:*",
                    "in": f"state:{state} county:{county}",
                    "key": api_key,
                },
            )
            header, *data_rows = rows
            index = {name: i for i, name in enumerate(header)}
            for row in data_rows:
                geoid = (
                    row[index["state"]] + row[index["county"]]
                    + row[index["tract"]] + row[index["block group"]]
                )
                if geoid not in wanted:
                    continue
                bucket = out.setdefault(geoid, {})
                for var in chunk:
                    bucket[var] = _to_number(row[index[var]])
    return out


def to_bands(raw: dict[str, float | None], dimension: str) -> list[dict]:
    """Reshape raw ACS variables into the band list scoring/indices.py expects.

    Each band is {"band", "min", "max", "estimate"} with an Estimate carrying
    its margin. Age sums the male and female columns for each group, combining
    their margins as a root sum of squares per the Census formula.
    """
    table = TABLES[dimension]

    def est(suffix: str) -> Estimate:
        return Estimate(
            raw.get(f"{table}_{suffix}E") or 0.0,
            raw.get(f"{table}_{suffix}M"),
        )

    if dimension == "household_income":
        return [
            {"band": label, "min": lo, "max": hi, "estimate": est(suffix)}
            for suffix, label, lo, hi in INCOME_BANDS_B19001
        ]
    if dimension == "tenure":
        return [
            {"band": label, "min": None, "max": None, "estimate": est(suffix)}
            for suffix, label, _, _ in TENURE_BANDS_B25003
        ]
    if dimension == "age":
        return [
            {
                "band": label, "min": lo, "max": hi,
                "estimate": sum_estimates([est(male), est(female)]),
            }
            for male, female, label, lo, hi in AGE_GROUPS_B01001
        ]
    raise ValueError(f"No band mapping defined for dimension '{dimension}'")


def total_households(raw: dict[str, float | None]) -> Estimate:
    """Total households for a block group, from B11001."""
    table = TABLES["households_total"]
    return Estimate(raw.get(f"{table}_001E") or 0.0, raw.get(f"{table}_001M"))

