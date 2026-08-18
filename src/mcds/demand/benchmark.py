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

CBP_URL = "https://api.census.gov/data/{year}/cbp"

#: Fewer establishments than this in the reference geography makes the ratio too
#: noisy to use. Widen to the CBSA, then the state, before giving up.
MIN_ESTABLISHMENTS = 5


def derive_benchmark(
    naics: str,
    state_fips: str,
    county_fips: str,
    *,
    api_key: str,
    year: int = 2022,
    fallback_to_cbsa: bool = True,
) -> dict | None:
    """Households per location for this NAICS in this county.

    Returns a benchmark dict ready for compute_balance -- including `source`,
    `source_url`, `vintage`, and a `confidence` set from how many establishments
    backed the ratio -- or None when even the widened geography is too thin,
    in which case the verdict stays suppressed.

    Confidence mapping: >= 30 establishments high, >= 10 medium, >= 5 low,
    below that None. The verdict band is wide enough that a low-confidence
    benchmark still separates a clearly crowded market from a clearly empty one.
    """
    raise NotImplementedError


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
        return derive_benchmark(cfg["naics"], county_fips[0], county_fips[1], api_key=api_key)

    return None
