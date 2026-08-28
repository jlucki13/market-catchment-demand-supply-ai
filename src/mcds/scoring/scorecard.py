"""Assemble the deterministic scorecard the reasoning stages are allowed to cite."""

from __future__ import annotations

from datetime import datetime, timezone

from .geography import GeographyResult
from .indices import BalanceResult, DemandResult, SupplyResult

#: Relative MOE above which an estimate is too imprecise to lead with.
WIDE_MOE_THRESHOLD = 0.30

#: How many competitors to name in `supply.strongest`.
STRONGEST_N = 10

#: A catchment holding more than this share of its whole county's establishments
#: for the same industry is not credible. County Business Patterns counts real
#: firms with payroll; a drive-time catchment is a small slice of a county, so a
#: direct-competitor count approaching the county total means the census pulled
#: in businesses that are not in this industry at all.
IMPLAUSIBLE_COUNTY_SHARE = 0.5


def build_scorecard(
    *,
    deal_id: str,
    vertical: str,
    demand: DemandResult,
    supply: SupplyResult,
    balance: BalanceResult,
    geography: GeographyResult | None = None,
    extra_warnings: list[str] | None = None,
) -> dict:
    """Produce the single JSON object every downstream stage reads.

    Warnings are collected from every stage into one list rather than left
    scattered, because the memo renders them verbatim and a caveat that does not
    reach the reader may as well not have been computed.
    """
    warnings = list(demand.warnings) + list(supply.warnings) + list(extra_warnings or [])

    rel = demand.qualified_households.relative_moe
    if rel is not None and rel > WIDE_MOE_THRESHOLD:
        warnings.append(
            f"The qualified-household estimate carries a margin of error of "
            f"+/-{rel:.0%} at 90% confidence. That is wide enough that the point "
            f"estimate should not be quoted without the interval, and the "
            f"balance verdict may flip within it."
        )
    if balance.verdict == "suppressed":
        warnings.append(
            "Balance verdict suppressed: no sourced benchmark for this vertical."
        )
    if geography and geography.notes:
        warnings.extend(geography.notes)

    # Cross-check the supply census against an independent source. CBP counts
    # establishments with payroll under a specific NAICS code; the Places census
    # counts whatever Google files under a type. When the two disagree sharply
    # the Places bucket is contaminated, and that is worth saying out loud
    # rather than leaving in the numbers.
    establishments = (balance.benchmark or {}).get("establishments")
    if establishments:
        direct = supply.by_substitutability.get("direct", 0)
        where = (balance.benchmark or {}).get("reference_geography", "the county")
        if direct > establishments:
            warnings.append(
                f"This catchment shows {direct} direct competitor(s), but County "
                f"Business Patterns records only {establishments} establishment(s) "
                f"in this industry across all of {where}. A catchment cannot "
                f"hold more of an industry than its county does, so the supply "
                f"census has pulled in businesses that are not in it. Treat the "
                f"Supply Index as an overstatement."
            )
        elif direct > establishments * IMPLAUSIBLE_COUNTY_SHARE:
            warnings.append(
                f"This catchment accounts for {direct} of the {establishments} "
                f"establishments County Business Patterns records for this "
                f"industry across {where} ({direct / establishments:.0%}). "
                f"That concentration is possible in a dense urban core but is "
                f"worth verifying before relying on the supply count."
            )

    strongest = sorted(supply.scored, key=lambda s: s.weight, reverse=True)[:STRONGEST_N]

    card: dict = {
        "deal_id": deal_id,
        "vertical": vertical,
        "demand": {
            "total_households": demand.total_households.as_dict(),
            "qualified_households": demand.qualified_households.as_dict(),
            "qualified_share": demand.qualified_share,
            "filters_applied": [
                {
                    "dimension": f.dimension,
                    "share": f.share,
                    "source_table": f.source_table,
                    "band": f.band,
                }
                for f in demand.filters_applied
            ],
            "combination": demand.combination,
            "independence_penalty_applied": demand.independence_penalty_applied,
        },
        "supply": {
            "index": supply.index,
            "competitor_count": supply.competitor_count,
            "effective_competitors": supply.index,
            "by_substitutability": supply.by_substitutability,
            "census_count": supply.census_count,
            "enriched_count": supply.enriched_count,
            "census_complete": supply.census_complete,
            "chain_groups": supply.chain_groups,
            "strongest": [
                {
                    "place_id": s.place_id,
                    "name": s.name,
                    "weight": s.weight,
                    "drive_time_minutes": s.drive_time_minutes,
                }
                for s in strongest
            ],
        },
        "balance": {
            "households_per_effective_competitor": balance.households_per_effective_competitor.as_dict(),
            "benchmark": balance.benchmark,
            "ratio": balance.ratio,
            "verdict": balance.verdict,
            "verdict_reason": balance.verdict_reason,
        },
        "warnings": warnings,
        "computed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    if geography is not None:
        card["geography"] = {
            "clusters": [
                {
                    "cluster_id": c.cluster_id,
                    "size": c.size,
                    "members": c.members,
                    "total_weight": c.total_weight,
                    "centroid": c.centroid,
                    "mean_drive_time_minutes": c.mean_drive_time_minutes,
                }
                for c in geography.clusters
            ],
            "unclustered": geography.unclustered,
            "gap_cells": [
                {
                    "lat": g.lat,
                    "lng": g.lng,
                    "underservice_score": g.underservice_score,
                    "nearest_competitor_minutes": g.nearest_competitor_minutes,
                }
                for g in geography.gap_cells
            ],
        }
    return card
