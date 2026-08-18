"""Demand Index, Supply Index, and market balance.

This module is the arithmetic core, and it is deliberately the one place where
no language model participates. Models classify, interpret, and write; they do
not produce numbers. Every value the memo is allowed to cite originates here.

Formulas and the reasoning behind each constant are documented in
docs/scoring-methodology.md. The constants below are tunable, but each one is
named and justified rather than buried as a magic number, because a reader
disagreeing with a weighting needs to be able to find and change it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Literal, Sequence

from ..moe import Estimate, product_moe, sum_estimates, widen

Substitutability = Literal["direct", "partial", "adjacent", "none"]

# --- Supply constants -------------------------------------------------------

#: How much of a competitor's strength survives its substitutability class.
#: `adjacent` is deliberately small but non-zero: a bakery does take some coffee
#: trips, and rounding that to zero is as wrong as counting it in full.
SUBSTITUTABILITY_WEIGHTS: dict[str, float] = {
    "direct": 1.0,
    "partial": 0.5,
    "adjacent": 0.15,
    "none": 0.0,
}

#: Review count of a solidly established local business. Entrenchment is scaled
#: so a competitor at this level scores 1.0 before the rating adjustment.
#: Review counts are heavy-tailed, so the log compression matters: without it a
#: 2,000-review chain would swamp eight independents put together.
ENTRENCHMENT_REFERENCE_REVIEWS = 150

#: Ratings below RATING_FLOOR carry no positive signal; the span sets how fast
#: strength rises above it. Clamps keep a single bad review season from zeroing
#: out a real competitor, and keep a perfect-but-thin rating from dominating.
RATING_FLOOR = 3.0
RATING_SPAN = 1.5
RATING_WEIGHT_MIN = 0.4
RATING_WEIGHT_MAX = 1.3

#: Used when a competitor has no rating and no review count. Absence of reviews
#: is not absence of business, so this sits just below a typical established
#: competitor rather than at zero. Always accompanied by a scorecard warning.
UNKNOWN_ENTRENCHMENT = 0.6

#: Fallback travel speed for competitors missing a routed drive time.
#: Only used when the Routes API returned nothing for a destination.
FALLBACK_SPEED_KMH = 30.0

#: Competitors sharing a brand are damped by k**-CHAIN_DAMPING_EXPONENT each, so
#: a chain's total weight grows as sqrt(k) rather than k. Six locations of one
#: operator are more competitive pressure than one and much less than six.
CHAIN_DAMPING_EXPONENT = 0.5

# --- Balance constants ------------------------------------------------------

#: Ratio thresholds for the over/under-served verdict. The band is wide because
#: the inputs do not support fine discrimination; a narrow band would imply a
#: precision the data cannot back.
OVERSUPPLIED_BELOW = 0.75
UNDERSERVED_ABOVE = 1.25

#: MOE inflation per demographic filter beyond the first, applied when filters
#: are multiplied. A heuristic, not a derived statistic, and flagged as such
#: wherever it is applied.
INDEPENDENCE_PENALTY_PER_EXTRA_FILTER = 0.25


# --- Results ----------------------------------------------------------------


@dataclass
class FilterResult:
    dimension: str
    share: float
    source_table: str | None = None
    band: str | None = None
    unit: str = "household"
    interpolated: bool = False


@dataclass
class DemandResult:
    total_households: Estimate
    qualified_households: Estimate
    qualified_share: float
    filters_applied: list[FilterResult] = field(default_factory=list)
    combination: str = "multiplicative"
    independence_penalty_applied: bool = False
    warnings: list[str] = field(default_factory=list)


@dataclass
class ScoredCompetitor:
    place_id: str
    name: str | None
    substitutability: Substitutability
    entrenchment: float
    accessibility: float
    weight: float
    drive_time_minutes: float | None
    chain_group: str | None = None


@dataclass
class SupplyResult:
    index: float
    competitor_count: int
    scored: list[ScoredCompetitor] = field(default_factory=list)
    by_substitutability: dict[str, int] = field(default_factory=dict)
    chain_groups: dict[str, int] = field(default_factory=dict)
    census_count: int | None = None
    enriched_count: int | None = None
    census_complete: bool = True
    warnings: list[str] = field(default_factory=list)


@dataclass
class BalanceResult:
    households_per_effective_competitor: Estimate
    ratio: float | None
    verdict: Literal["oversupplied", "balanced", "underserved", "suppressed"]
    verdict_reason: str | None
    benchmark: dict | None = None


# --- Demand -----------------------------------------------------------------


def band_share(
    bands: Sequence[dict],
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    include_bands: Sequence[str] | None = None,
) -> tuple[Estimate, Estimate, list[str]]:
    """Share of a distribution falling inside a filter.

    Returns (numerator, denominator, notes) where `notes` describes every
    approximation made, so the caller can surface them rather than presenting an
    apportioned figure as a measured one.

    Two selector styles are supported. `include_bands` names categorical bands
    outright (tenure, household type). `minimum`/`maximum` select a numeric
    range, apportioning any band that straddles a boundary.

    Band edges are normalised before apportioning. ACS brackets print as
    "$50,000 to $99,999" but are contiguous with the next bracket at $100,000,
    so the true span is 50,000 rather than the printed 49,999. Using the printed
    width biases every apportioned band low, and the bias accumulates across a
    distribution.
    """
    denominator = sum_estimates([b["estimate"] for b in bands])
    selected: list[Estimate] = []
    notes: list[str] = []

    if include_bands is not None:
        for band in bands:
            if band.get("band") in include_bands:
                selected.append(band["estimate"])
        numerator = sum_estimates(selected) if selected else Estimate(0.0, 0.0)
        return numerator, denominator, notes

    numeric = [b for b in bands if b.get("min") is not None or b.get("max") is not None]
    ordered = sorted(
        numeric, key=lambda b: (b.get("min") if b.get("min") is not None else b.get("max"))
    )

    for i, band in enumerate(ordered):
        est: Estimate = band["estimate"]
        bmin, bmax = band.get("min"), band.get("max")

        if bmax is None:  # open-topped tail
            if maximum is not None and bmin >= maximum:
                continue
            if minimum is None or bmin >= minimum:
                selected.append(est)
            else:
                # The filter floor lands inside an unbounded band. There is no
                # distribution shape to apportion against, so the band is
                # dropped and the resulting share is a documented undercount
                # rather than a guess at the tail.
                notes.append(
                    f"The filter floor ({minimum:,.0f}) falls inside the "
                    f"open-topped band '{band.get('band')}', which cannot be "
                    f"apportioned. It was excluded, so this share understates "
                    f"the true figure. Set the filter to a band boundary to "
                    f"remove the approximation."
                )
            continue

        lo = bmin if bmin is not None else bmax
        # A band's true upper edge is where the next band begins.
        nxt = ordered[i + 1] if i + 1 < len(ordered) else None
        hi = nxt.get("min") if nxt is not None and nxt.get("min") is not None else bmax + 1
        span = hi - lo

        clip_lo = max(lo, minimum) if minimum is not None else lo
        clip_hi = min(hi, maximum) if maximum is not None else hi
        if clip_hi <= clip_lo:
            continue
        fraction = 1.0 if span <= 0 else (clip_hi - clip_lo) / span
        if fraction < 1.0:
            notes.append(
                f"The filter cut across band '{band.get('band')}'; "
                f"{fraction:.0%} of it was attributed by assuming households are "
                f"distributed uniformly inside the band."
            )
        selected.append(est.scaled(fraction))

    numerator = sum_estimates(selected) if selected else Estimate(0.0, 0.0)
    return numerator, denominator, notes


def compute_demand_index(
    demographics: dict,
    vertical: dict,
) -> DemandResult:
    """Qualified households in the catchment.

        DI = total_households x product(share_d for each demographic filter d)

    `combination: primary_only` uses the single most selective filter instead of
    multiplying, which is the right choice when filters are known to correlate
    and you would rather under-claim than compound an independence error.
    """
    demand_cfg = vertical.get("demand", {}) or {}
    combination = demand_cfg.get("combination", "multiplicative")
    filters_cfg = demand_cfg.get("filters", []) or []
    distributions = demographics.get("distributions", {}) or {}
    total = demographics["households"]
    warnings: list[str] = []
    applied: list[FilterResult] = []

    for f in filters_cfg:
        dim = f["dimension"]
        bands = distributions.get(dim)
        if not bands:
            warnings.append(
                f"Demographic filter '{dim}' was configured but no distribution "
                f"was returned for it; the filter was skipped and qualified "
                f"households are correspondingly overstated."
            )
            continue
        numerator, denominator, band_notes = band_share(
            bands,
            minimum=f.get("min"),
            maximum=f.get("max"),
            include_bands=f.get("include_bands"),
        )
        share = 0.0 if denominator.value == 0 else numerator.value / denominator.value
        unit = f.get("unit", "household")
        if unit == "person":
            warnings.append(
                f"Filter '{dim}' is measured per person (table "
                f"{f.get('source_table')}) but is applied to a household count. "
                f"This assumes household composition is uniform across the "
                f"catchment, which it is not; treat the resulting share as "
                f"approximate."
            )
        warnings.extend(f"Filter '{dim}': {note}" for note in band_notes)
        applied.append(
            FilterResult(
                dimension=dim,
                share=share,
                source_table=f.get("source_table"),
                band=_band_label(f),
                unit=unit,
                interpolated=bool(band_notes),
            )
        )

    if not applied:
        return DemandResult(
            total_households=total,
            qualified_households=total,
            qualified_share=1.0,
            filters_applied=[],
            combination=combination,
            warnings=warnings
            + ["No demographic filter was applied; demand equals total households."],
        )

    if combination == "primary_only":
        chosen = min(applied, key=lambda r: r.share)
        combined_share = chosen.share
        applied = [chosen]
        penalty_applied = False
    else:
        combined_share = 1.0
        for r in applied:
            combined_share *= r.share
        penalty_applied = (
            len(applied) > 1 and bool(demand_cfg.get("independence_penalty", False))
        )

    share_est = Estimate(combined_share, None)
    qualified = Estimate(total.value * combined_share, None)
    if total.moe is not None:
        qualified = Estimate(
            total.value * combined_share,
            product_moe(total, Estimate(combined_share, 0.0)),
        )

    if penalty_applied:
        factor = 1.0 + INDEPENDENCE_PENALTY_PER_EXTRA_FILTER * (len(applied) - 1)
        qualified = widen(qualified, factor)
        dims = " and ".join(r.dimension for r in applied)
        warnings.append(
            f"The {dims} filters were multiplied together, which assumes they are "
            f"independent of each other. Demographic dimensions rarely are, so the "
            f"interval was widened by {factor:.2f}x to reflect it. That widening is "
            f"a judgment-based inflation, not a derived statistic."
        )

    _ = share_est  # retained for readability of the formula above
    return DemandResult(
        total_households=total,
        qualified_households=qualified,
        qualified_share=combined_share,
        filters_applied=applied,
        combination=combination,
        independence_penalty_applied=penalty_applied,
        warnings=warnings,
    )


def _band_label(f: dict) -> str | None:
    if f.get("include_bands"):
        return ", ".join(f["include_bands"])
    lo, hi = f.get("min"), f.get("max")
    if lo is None and hi is None:
        return None
    if hi is None:
        return f">= {lo:,.0f}"
    if lo is None:
        return f"<= {hi:,.0f}"
    return f"{lo:,.0f}-{hi:,.0f}"


# --- Supply -----------------------------------------------------------------


def entrenchment(
    rating: float | None,
    review_count: int | None,
) -> tuple[float, bool]:
    """How established a competitor looks, from rating and review volume.

        e = log1p(reviews)/log1p(REFERENCE) x clamp((rating - FLOOR)/SPAN)

    Review count is a proxy for how long and how busily a place has operated. It
    is a poor proxy for revenue and must never be presented as one: it is biased
    by business age, category norms, and whether the operator asks for reviews.
    Returns (value, is_estimated) where is_estimated marks the no-data fallback.
    """
    if review_count is None and rating is None:
        return UNKNOWN_ENTRENCHMENT, True

    volume = (
        math.log1p(review_count) / math.log1p(ENTRENCHMENT_REFERENCE_REVIEWS)
        if review_count is not None
        else UNKNOWN_ENTRENCHMENT
    )
    if rating is None:
        quality = 1.0
    else:
        quality = min(
            RATING_WEIGHT_MAX,
            max(RATING_WEIGHT_MIN, (rating - RATING_FLOOR) / RATING_SPAN),
        )
    return volume * quality, review_count is None or rating is None


def accessibility(drive_minutes: float | None, catchment_minutes: float) -> float:
    """Distance decay in drive-time space.

        a = exp(-t / tau),  tau = catchment_minutes / ln 2

    Calibrated so a competitor sitting exactly at the catchment edge counts half
    as much as one at the target's front door. Straight-line distance is never
    used here: a competitor two miles away across a river is not two miles away.
    """
    if drive_minutes is None:
        return 0.5  # edge-of-catchment equivalent; caller emits a warning
    tau = catchment_minutes / math.log(2)
    return math.exp(-max(0.0, drive_minutes) / tau)


def compute_supply_index(
    competitors: Iterable[dict],
    *,
    catchment_minutes: float,
    census_count: int | None = None,
) -> SupplyResult:
    """Effective competitor equivalents in the catchment.

        SI = sum over competitors of (substitutability x entrenchment x accessibility)

    Read the result as "how many full-strength, doorstep, direct competitors is
    this market's supply equivalent to". Nine listed businesses can be an SI of
    2.5 if most are weak, distant, or only adjacent substitutes.
    """
    competitors = list(competitors)
    warnings: list[str] = []
    scored: list[ScoredCompetitor] = []
    by_sub: dict[str, int] = {}
    chain_counts: dict[str, int] = {}

    for c in competitors:
        g = c.get("chain_group")
        if g:
            chain_counts[g] = chain_counts.get(g, 0) + 1

    missing_drive_time = 0
    estimated_entrenchment = 0

    for c in competitors:
        sub = c.get("substitutability") or "none"
        by_sub[sub] = by_sub.get(sub, 0) + 1
        s = SUBSTITUTABILITY_WEIGHTS.get(sub, 0.0)

        e, e_estimated = entrenchment(c.get("rating"), c.get("user_rating_count"))
        if e_estimated:
            estimated_entrenchment += 1

        group = c.get("chain_group")
        if group and chain_counts.get(group, 1) > 1:
            e *= chain_counts[group] ** -CHAIN_DAMPING_EXPONENT

        t = c.get("drive_time_minutes")
        if t is None:
            missing_drive_time += 1
        a = accessibility(t, catchment_minutes)

        w = s * e * a
        scored.append(
            ScoredCompetitor(
                place_id=c["place_id"],
                name=c.get("name"),
                substitutability=sub,  # type: ignore[arg-type]
                entrenchment=e,
                accessibility=a,
                weight=w,
                drive_time_minutes=t,
                chain_group=group,
            )
        )

    if missing_drive_time:
        warnings.append(
            f"{missing_drive_time} competitor(s) had no routed drive time and "
            f"were scored at the edge-of-catchment accessibility default."
        )
    if estimated_entrenchment:
        warnings.append(
            f"{estimated_entrenchment} competitor(s) were missing a rating or "
            f"review count; entrenchment for those is a default, not a measurement."
        )
    multi = {g: n for g, n in chain_counts.items() if n > 1}
    if multi:
        warnings.append(
            "Chain locations were damped so a multi-location operator is not "
            "counted as that many independent competitors: "
            + ", ".join(f"{g} x{n}" for g, n in sorted(multi.items()))
            + "."
        )

    enriched = len(competitors)
    complete = census_count is None or census_count <= enriched
    if not complete:
        warnings.append(
            f"The catchment holds {census_count} matching places but only "
            f"{enriched} could be named and enriched (the Places Aggregate API "
            f"returns place IDs only when the match count is 100 or fewer). The "
            f"Supply Index is a documented undercount; treat the balance verdict "
            f"as an upper bound on how underserved this market is."
        )

    return SupplyResult(
        index=sum(s.weight for s in scored),
        competitor_count=enriched,
        scored=scored,
        by_substitutability=by_sub,
        chain_groups=chain_counts,
        census_count=census_count,
        enriched_count=enriched,
        census_complete=complete,
        warnings=warnings,
    )


# --- Balance ----------------------------------------------------------------


def compute_balance(
    demand: DemandResult,
    supply: SupplyResult,
    benchmark: dict | None,
) -> BalanceResult:
    """Demand per effective competitor, against a sourced sustainability benchmark.

        households_per_effective_competitor = DI / (SI + 1)
        ratio = that / benchmark.households_per_location

    The +1 is the subject business itself: the buyer is not entering an empty
    market, they are becoming one of the competitors.

    When no sourced benchmark exists the verdict is SUPPRESSED rather than
    guessed. A confident-sounding "underserved" derived from an invented
    denominator is the single most dangerous output this tool could produce.
    """
    denominator = supply.index + 1.0
    hpec = Estimate(
        demand.qualified_households.value / denominator,
        None
        if demand.qualified_households.moe is None
        else demand.qualified_households.moe / denominator,
    )

    b_value = (benchmark or {}).get("households_per_location")
    b_conf = (benchmark or {}).get("confidence", "none")
    b_source = (benchmark or {}).get("source")

    if not b_value or b_conf == "none" or not b_source:
        return BalanceResult(
            households_per_effective_competitor=hpec,
            ratio=None,
            verdict="suppressed",
            verdict_reason=(
                "No sourced sustainability benchmark is configured for this "
                "vertical, so the over/under-served verdict is withheld. The "
                "demand-per-competitor figure above still stands on its own; it "
                "simply has nothing to be measured against yet. Set "
                "benchmark.method to cbp_derived, or supply a static value with "
                "a citation, in the vertical config."
            ),
            benchmark=benchmark,
        )

    ratio = hpec.value / b_value
    if ratio < OVERSUPPLIED_BELOW:
        verdict = "oversupplied"
        reason = (
            f"Each effective competitor has about {hpec.value:,.0f} qualified "
            f"households to draw on, against a benchmark of {b_value:,.0f} needed "
            f"to sustain one location."
        )
    elif ratio > UNDERSERVED_ABOVE:
        verdict = "underserved"
        reason = (
            f"Each effective competitor has about {hpec.value:,.0f} qualified "
            f"households against a benchmark of {b_value:,.0f}, leaving headroom."
        )
    else:
        verdict = "balanced"
        reason = (
            f"Demand per effective competitor ({hpec.value:,.0f} households) sits "
            f"within the balanced band around the {b_value:,.0f} benchmark."
        )

    return BalanceResult(
        households_per_effective_competitor=hpec,
        ratio=ratio,
        verdict=verdict,  # type: ignore[arg-type]
        verdict_reason=reason,
        benchmark=benchmark,
    )
