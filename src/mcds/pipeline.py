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

from .catchment.isochrone import fetch_isochrone, geocode
from .config import REPO_ROOT, load_settings, load_vertical, validate
from .demand.benchmark import resolve_benchmark
from .demand.census import block_groups_intersecting, fetch_acs
from .demand.interpolate import build_demographics
from .reasoning.classify import apply_classifications, classify
from .reasoning.claims import reconcile
from .reasoning.client import RoutingConfig
from .reasoning.review import review
from .reasoning.synthesize import synthesize
from .supply.places import census_catchment, fetch_details
from .supply.routing import attach_drive_times
from .moe import Estimate
from .provenance import verify
from .reasoning.classify import classify_from_priors
from .render.memo import render_markdown
from .scoring.geography import analyze_geography
from .scoring.indices import compute_balance, compute_demand_index, compute_supply_index
from .scoring.scorecard import build_scorecard
from .supply.places import detect_chains

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
    extra_warnings: list[str] | None = None,
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

    extra: list[str] = list(extra_warnings or [])
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


def apply_priors(competitors: list[dict], vertical: dict) -> list[str]:
    """Classify and group chains deterministically. Returns warnings to surface.

    The zero-cost path: chain detection is name-based and substitutability comes
    from the vertical config's category priors. Mutates `competitors` in place
    and hands back the caveats the memo has to carry, because a category-level
    supply read that does not announce itself as one is worse than no read.
    """
    chains = detect_chains(competitors)
    for c in competitors:
        c.setdefault("chain_group", chains.get(c["place_id"]))

    result = classify_from_priors(competitors, vertical)
    by_id = {r["place_id"]: r for r in result["classifications"]}
    for c in competitors:
        r = by_id.get(c["place_id"])
        if r:
            c["substitutability"] = r["substitutability"]
            c["substitutability_reason"] = r["reason"]
            c["classifier_confidence"] = r["confidence"]

    return [
        "Substitutability was assigned from vertical-config category priors "
        "rather than by a model, so every competitor call is category-level and "
        "tagged low confidence."
    ] + result["coverage_concerns"]


def run(
    deal: dict,
    *,
    dry_run: bool = False,
    settings: dict | None = None,
    strict_provenance: bool = True,
    use_llm: bool = True,
) -> RunResult:
    """Full pipeline for one deal.

    `use_llm=False` runs the whole thing with no model calls and therefore no
    Anthropic spend: classification falls back to config priors and the
    synthesis and review stages are skipped, leaving the deterministic scorecard
    and a memo built from it. See docs/running-free.md.
    """
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
        live_warnings: list[str] = []
    else:
        (catchment, demographics, competitors, census_count, benchmark,
         live_warnings) = _fetch_live(deal, vertical, settings, use_llm=use_llm)

    # In the live path classification already ran inside _fetch_live, because
    # substitutability feeds the Supply Index. Only the dry run needs it here.
    priors_warnings: list[str] = list(live_warnings)
    if dry_run and not use_llm:
        priors_warnings.extend(apply_priors(competitors, vertical))

    scorecard = score(
        deal, vertical, catchment, demographics, competitors,
        benchmark=benchmark, census_count=census_count,
        extra_warnings=priors_warnings,
    )

    findings = review_result = None
    if not dry_run and use_llm:
        findings, review_result, reason_warnings = _reason(
            scorecard, competitors, demographics, deal, vertical, settings
        )
        scorecard["warnings"].extend(reason_warnings)
        verify(findings, scorecard, strict=strict_provenance)

    memo = render_markdown(
        scorecard, findings, deal=deal, catchment=catchment, review=review_result
    )
    return RunResult(
        scorecard=scorecard, memo_markdown=memo, findings=findings,
        review=review_result,
        catchment=catchment, competitors=competitors, demographics=demographics,
    )


def _fetch_live(
    deal: dict, vertical: dict, settings: dict, *, use_llm: bool = True
) -> tuple:
    """Geocode, isochrone, competitor census, enrichment, routing, and demographics.

    Order is load-bearing. The catchment must exist before either the demand or
    supply pull, because both are defined against that one polygon; the moment
    either falls back to a radius they are measuring different markets and the
    ratio between them means nothing. Classification runs before scoring because
    substitutability is an input to the Supply Index, not a label applied after.
    """
    credentials = settings.get("credentials") or {}
    google = credentials.get("google_maps")
    mapbox = credentials.get("mapbox")
    census_key = credentials.get("census")
    missing = [
        name for name, value in
        [("GOOGLE_MAPS_API_KEY", google), ("MAPBOX_ACCESS_TOKEN", mapbox),
         ("CENSUS_API_KEY", census_key)]
        if not value
    ]
    if missing:
        raise RuntimeError(
            f"Missing credential(s): {', '.join(missing)}. Put them in .env, or "
            f"run with --dry-run to use fixtures."
        )

    warnings: list[str] = []
    places_cfg = settings.get("places") or {}
    minutes = deal.get("catchment_minutes") or vertical["catchment"]["default_minutes"]

    # 1-2. Origin and catchment.
    origin = geocode(deal["address"], api_key=google)
    catchment, iso_warnings = fetch_isochrone(
        origin["lat"], origin["lng"], minutes,
        profile=vertical["catchment"].get("profile", "driving"),
        access_token=mapbox,
        formatted_address=origin["formatted_address"],
        geocode_quality=origin["quality"],
    )
    warnings.extend(iso_warnings)
    validate(catchment, "catchment")
    ring = catchment["polygon"]["coordinates"][0]

    # 3-4. Supply census, then enrichment of the shortlist.
    places = vertical["places"]
    census = census_catchment(
        ring,
        places["direct_types"] + places.get("adjacent_types", []),
        api_key=google,
        type_filter_mode=places.get("type_filter_mode", "primary"),
    )
    cap = places_cfg.get("max_enriched", 100)
    competitors, detail_warnings = fetch_details(
        census["place_ids"][:cap], api_key=google,
    )
    warnings.extend(detail_warnings)

    # 5. Drive times for the whole catchment in one request.
    warnings.extend(attach_drive_times(
        competitors, (origin["lat"], origin["lng"]), api_key=google,
    ))

    # 6. Substitutability -- before scoring, because it feeds the Supply Index.
    chains = detect_chains(competitors)
    for competitor in competitors:
        competitor.setdefault("chain_group", chains.get(competitor["place_id"]))
    if use_llm:
        routing = RoutingConfig.from_settings(settings)
        result, classify_warnings = classify(
            competitors, vertical, routing,
            api_key=(settings.get("credentials") or {}).get("anthropic"),
        )
        apply_classifications(competitors, result)
        warnings.extend(classify_warnings)
        for concern in result.get("coverage_concerns", []) or []:
            warnings.append(f"Coverage concern: {concern}")
    else:
        warnings.extend(apply_priors(competitors, vertical))

    # 7-8. Demand: block groups intersecting the same polygon, then ACS.
    census_cfg = settings.get("census") or {}
    vintage = census_cfg.get("vintage", 2023)
    dimensions = [
        f["dimension"] for f in (vertical.get("demand", {}).get("filters") or [])
    ]
    block_groups = block_groups_intersecting(ring, vintage=vintage)
    if not block_groups:
        raise RuntimeError(
            "No Census block groups intersect this catchment. That usually means "
            "the polygon is outside the United States or the coordinate order "
            "was swapped somewhere upstream."
        )
    acs = fetch_acs(
        block_groups, dimensions + ["households_total"],
        api_key=census_key, vintage=vintage,
        dataset=census_cfg.get("dataset", "acs/acs5"),
    )
    demographics, demand_warnings = build_demographics(
        ring, block_groups, acs, dimensions, vintage=vintage,
    )
    warnings.extend(demand_warnings)
    validate(demographics, "demographics")

    # 9. Benchmark, from the county the origin falls in.
    county = _dominant_county(block_groups)
    benchmark = resolve_benchmark(
        vertical, county_fips=county, api_key=census_key,
    )
    if benchmark is None:
        warnings.append(
            "No sustainability benchmark could be derived for this county and "
            "NAICS code, so the over/under-served verdict stays suppressed."
        )

    return catchment, demographics, competitors, census["count"], benchmark, warnings


def _dominant_county(block_groups: list[dict]) -> tuple[str, str] | None:
    """The county most of the catchment sits in, for benchmark derivation.

    A catchment spanning a county line gets the county holding the most block
    groups. The benchmark is a coarse denominator and the difference between
    neighbouring counties is smaller than its own confidence interval.
    """
    from collections import Counter

    counties = Counter(
        (bg["state"], bg["county"]) for bg in block_groups
        if bg.get("state") and bg.get("county")
    )
    return counties.most_common(1)[0][0] if counties else None


def _reason(
    scorecard: dict, competitors: list[dict], demographics: dict,
    deal: dict, vertical: dict, settings: dict,
) -> tuple[dict, dict | None, list[str]]:
    """Synthesis and adversarial review over a finished scorecard.

    Classification is not here: it runs inside _fetch_live, because
    substitutability is an input to the Supply Index rather than a comment on it.
    """
    routing = RoutingConfig.from_settings(settings)
    api_key = (settings.get("credentials") or {}).get("anthropic")
    warnings: list[str] = []

    claims = deal.get("seller_claims") or {"claims": []}
    reconciled = reconcile(claims, scorecard, demographics)
    enriched_claims = {"claims": claims.get("claims", []), "reconciled": reconciled}

    findings, synth_warnings = synthesize(
        scorecard, competitors, demographics, enriched_claims, vertical,
        routing, api_key=api_key,
    )
    warnings.extend(synth_warnings)

    review_result = None
    if settings.get("reasoning", {}).get("adversarial_review", True):
        review_result, review_warnings = review(
            findings, scorecard, routing, api_key=api_key,
        )
        warnings.extend(review_warnings)

    return findings, review_result, warnings