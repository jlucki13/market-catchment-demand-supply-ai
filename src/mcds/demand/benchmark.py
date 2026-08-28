"""Where the sustainability benchmark comes from.

`balance.verdict` needs a denominator: how many qualified households does one
location of this kind need to survive? Getting that number wrong, or inventing
it, produces a confident "underserved" verdict that is worse than no verdict at
all -- which is why compute_balance suppresses the verdict outright when the
benchmark is unsourced.

Two ways to source it honestly:

  static -- a published industry figure, entered with its citation and vintage
    in the vertical config. Trade associations and industry research publish
    these. It must carry `source` and `source_url`, or it is treated as absent.

  cbp_derived -- computed from public data at run time, which is the default
    because it is both free and locally calibrated:

      households_per_location = ACS households in county (or CBSA)
                                / CBP establishments for the NAICS code

    County Business Patterns publishes establishment counts by NAICS by county.
    Dividing local households by local establishments gives what the market
    currently supports HERE, rather than a national average that may describe
    nowhere. A dense urban county and a rural one genuinely differ, and this
    captures that instead of averaging it away.

    The honest caveat, which belongs in the memo: this measures the CURRENT
    equilibrium, not a healthy one. If the county is already saturated, the
    derived benchmark encodes saturation as normal. It is a comparison against
    peers, not against profitability -- so a "balanced" verdict from a
    cbp_derived benchmark means "typical for this county", not "viable".

      https://api.census.gov/data/{year}/cbp?get=ESTAB,NAICS2017_LABEL
        &for=county:{ccc}&in=state:{ss}&NAICS2017={naics}&key=...
"""

from __future__ import annotations

from ..http import ApiError, get_json

CBP_URL = "https://api.census.gov/data/{year}/cbp"

#: Fewer establishments than this in the reference geography makes the ratio too
#: noisy to use. Widen to the CBSA, then the state, before giving up.
MIN_ESTABLISHMENTS = 5


#: Establishment counts below these thresholds make the ratio too noisy to
#: carry a verdict. Fewer than MIN_ESTABLISHMENTS and no benchmark is returned
#: at all, which suppresses the verdict rather than guessing one.
CONFIDENCE_THRESHOLDS = ((30, "high"), (10, "medium"), (MIN_ESTABLISHMENTS, "low"))

ACS_URL = "https://api.census.gov/data/{year}/acs/acs5"


def derive_benchmark(
    naics: str,
    state_fips: str,
    county_fips: str,
    *,
    api_key: str,
    year: int = 2022,
    acs_vintage: int = 2023,
    fallback_to_cbsa: bool = True,
) -> dict | None:
    """Households per location for this NAICS in this county.

    Returns a benchmark dict ready for compute_balance -- with `source`,
    `source_url`, `vintage`, and a `confidence` set from how many
    establishments backed the ratio -- or None when the geography is too thin,
    in which case the verdict stays suppressed.

    The caveat that belongs in every memo using this: it measures the CURRENT
    equilibrium, not a healthy one. In an already-saturated county the derived
    benchmark encodes saturation as normal, so "balanced" means typical for this
    county, not viable.
    """
    try:
        cbp = get_json(
            CBP_URL.format(year=year), service="Census CBP",
            params={
                "get": "ESTAB", "for": f"county:{county_fips}",
                "in": f"state:{state_fips}", "NAICS2017": naics, "key": api_key,
            },
        )
        households = get_json(
            ACS_URL.format(year=acs_vintage), service="Census ACS",
            params={
                "get": "B11001_001E", "for": f"county:{county_fips}",
                "in": f"state:{state_fips}", "key": api_key,
            },
        )
    except ApiError:
        # A NAICS code with no establishments in a county returns an error
        # rather than a zero row, which is a legitimate "no benchmark" answer.
        return None

    establishments = _first_value(cbp, "ESTAB")
    total_households = _first_value(households, "B11001_001E")
    if not establishments or not total_households or establishments < MIN_ESTABLISHMENTS:
        return None

    confidence = next(
        (label for floor, label in CONFIDENCE_THRESHOLDS if establishments >= floor),
        None,
    )
    if confidence is None:
        return None

    return {
        "households_per_location": round(total_households / establishments),
        "method": "cbp_derived",
        "source": (
            f"Census County Business Patterns {year} (NAICS {naics}: "
            f"{establishments:,.0f} establishments) over ACS {acs_vintage} "
            f"5-year households ({total_households:,.0f}) for county "
            f"{state_fips}{county_fips}"
        ),
        "source_url": CBP_URL.format(year=year),
        "vintage": year,
        "confidence": confidence,
        "establishments": int(establishments),
        "reference_geography": f"county {state_fips}{county_fips}",
        "caveat": (
            "Derived from the county's current equilibrium, not from a "
            "profitability threshold. A 'balanced' verdict against it means "
            "typical for this county, which in an already-saturated county "
            "means saturated."
        ),
    }


def _first_value(rows: list, column: str) -> float | None:
    """Census returns [header, row, ...]; pull one column from the first row."""
    if not rows or len(rows) < 2:
        return None
    try:
        return float(rows[1][rows[0].index(column)])
    except (ValueError, IndexError):
        return None


def resolve_benchmark(vertical: dict, *, county_fips: tuple[str, str] | None, api_key: str | None) -> dict | None:
    """Pick the benchmark for a run: static if configured and sourced, else derived.

    A static benchmark missing `source` or `source_url` is treated as absent
    rather than trusted, because an uncited number in a diligence memo is
    indistinguishable from a guess.
    """
    cfg = (vertical.get("benchmark") or {}).copy()
    method = cfg.get("method", "static")

    if method == "static":
        if cfg.get("households_per_location") and cfg.get("source") and cfg.get("source_url"):
            return cfg
        return None

    if method == "cbp_derived":
        if not (county_fips and api_key and cfg.get("naics")):
            return None
        return derive_benchmark(
            cfg["naics"], county_fips[0], county_fips[1], api_key=api_key
        )

    return None
