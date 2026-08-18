"""Orchestration: address in, scorecard and memo out.

Two paths through here. The live path calls the external APIs and the reasoning
stages. The dry-run path loads fixtures/ and skips every network call, which is
how the deterministic half is exercised in tests and how a change to the scoring
formulas gets checked without spending a cent on API calls.

Stage ordering is not arbitrary. The catchment must exist before either the
demand or supply pull, because both are defined against that polygon and against
nothing else -- the moment one of them falls back to a radius, the two halves are
measuring different markets and the ratio between them means nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import REPO_ROOT, load_settings, load_vertical, validate
from .moe import Estimate
from .provenance import verify
from .render.memo import render_markdown
from .scoring.geography import analyze_geography
from .scoring.indices import compute_balance, compute_demand_index, compute_supply_index
from .scoring.scorecard import build_scorecard

FIXTURES = REPO_ROOT / "fixtures"


@dataclass
class RunResult:
    scorecard: dict
    memo_markdown: str
    findings: dict | None = None
    review: dict | None = None
    catchment: dict | None = None
    competitors: list[dict] = field(default_factory=list)
    demographics: dict | None = None


def _hydrate_estimates(demographics: dict) -> dict:
    """Turn the JSON estimate dicts into Estimate objects for the scoring path."""
    out = dict(demographics)
    hh = demographics["households"]
    out["households"] = Estimate(hh["value"], hh.get("moe"))
    out["distributions"] = {
        dim: [
            {
                "band": b["band"],
                "min": b.get("min"),
                "max": b.get("max"),
                "estimate": Estimate(b["estimate"]["value"], b["estimate"].get("moe")),
            }
            for b in bands
        ]
        for dim, bands in (demographics.get("distributions") or {}).items()
    }
    return out


def score(
    deal: dict,
    vertical: dict,
    catchment: dict,
    demographics: dict,
    competitors: list[dict],
    *,
    benchmark: dict | None = None,
    census_count: int | None = None,
) -> dict:
    """The deterministic half: everything from raw data to scorecard.

    Separated from run() so tests can drive it directly with fixtures, and so
    the scoring path is provably free of any model call -- there is nowhere in
    this function for one to hide.
    """
    minutes = deal.get("catchment_minutes") or vertical["catchment"]["default_minutes"]

    demand = compute_demand_index(_hydrate_estimates(demographics), vertical)
    supply = compute_supply_index(
        competitors, catchment_minutes=minutes, census_count=census_count
    )
    for scored in supply.scored:  # feed weights back for the geography pass
        for c in competitors:
            if c["place_id"] == scored.place_id:
                c["weight"] = scored.weight
    balance = compute_balance(demand, supply, benchmark)
    clustering = vertical.get("clustering") or {}
    geography = analyze_geography(
        catchment["polygon"]["coordinates"][0],
        competitors,
        eps_km=clustering.get("eps_km", 0.8),
        min_samples=clustering.get("min_samples", 3),
    )

    extra: list[str] = []
    if catchment.get("traffic_aware") is False:
        extra.append(
            "The catchment was generated from a free-flow driving profile with no "
            "live traffic. At peak hours the real 10-minute reach is smaller than "
            "this polygon, so both the household count and the competitor set are "
            "upper bounds."
        )
    quality = (catchment.get("origin") or {}).get("geocode_quality")
    if quality and quality not in ("ROOFTOP", "RANGE_INTERPOLATED"):
        extra.append(
            f"The target address geocoded to {quality} precision. A catchment "
            f"drawn from an approximate origin can be materially off."
        )

    scorecard = build_scorecard(
        deal_id=deal["deal_id"],
        vertical=vertical["id"],
        demand=demand,
        supply=supply,
        balance=balance,
        geography=geography,
        extra_warnings=extra,
    )
    validate(scorecard, "scorecard")
    return scorecard


def run(
    deal: dict,
    *,
    dry_run: bool = False,
    settings: dict | None = None,
    strict_provenance: bool = True,
) -> RunResult:
    """Full pipeline for one deal."""
    settings = settings or load_settings()
    vertical = load_vertical(deal["vertical"])

    if dry_run:
        catchment = json.loads((FIXTURES / "sunnyside_catchment.json").read_text())
        demographics = json.loads((FIXTURES / "sunnyside_demographics.json").read_text())
        competitors = json.loads((FIXTURES / "sunnyside_competitors.json").read_text())
        validate(catchment, "catchment")
        validate(demographics, "demographics")
        census_count = len(competitors)
        benchmark = None  # fixtures carry no sourced benchmark; verdict suppresses
    else:
        catchment, demographics, competitors, census_count, benchmark = _fetch_live(
            deal, vertical, settings
        )

    scorecard = score(
        deal, vertical, catchment, demographics, competitors,
        benchmark=benchmark, census_count=census_count,
    )

    findings = review = None
    if not dry_run:
        findings, review = _reason(
            scorecard, competitors, demographics, deal, vertical, settings
        )
        verify(findings, scorecard, strict=strict_provenance)

    memo = render_markdown(
        scorecard, findings, deal=deal, catchment=catchment, review=review
    )
    return RunResult(
        scorecard=scorecard, memo_markdown=memo, findings=findings, review=review,
        catchment=catchment, competitors=competitors, demographics=demographics,
    )


def _fetch_live(deal: dict, vertical: dict, settings: dict) -> tuple[Any, ...]:
    """Geocode, isochrone, Places census, Place Details, Routes matrix, ACS.

    Order matters and each step gates the next:
      1. geocode the address; abort on anything worse than RANGE_INTERPOLATED
      2. isochrone from the geocoded point
      3. Places Aggregate computeInsights over the polygon -> count + place IDs
      4. Place Details on the shortlist (capped at settings.places.max_enriched)
      5. Routes computeRouteMatrix, one origin to all competitors
      6. TIGERweb block groups intersecting the polygon, then ACS on those
      7. benchmark.resolve_benchmark against the county the origin falls in
    """
    raise NotImplementedError


def _reason(
    scorecard: dict, competitors: list[dict], demographics: dict,
    deal: dict, vertical: dict, settings: dict,
) -> tuple[dict, dict]:
    """Classification, synthesis, adversarial review.

    Note classification must run BEFORE score(), not here -- substitutability is
    an input to the Supply Index. This function covers only the stages that read
    a finished scorecard. Wiring it in live means calling classify() inside
    _fetch_live's step 4/5 window, once details are in and before routing.
    """
    raise NotImplementedError
