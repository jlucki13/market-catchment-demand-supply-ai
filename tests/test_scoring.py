"""Golden-number tests for the deterministic half.

The scoring formulas are the part of this system with no second opinion: a
model's classification can be sanity-checked by reading it, but a wrong constant
in the Supply Index produces a plausible number that nobody catches. So the
constants are pinned to hand-computed values here, and a change to any of them
has to be deliberate enough to update a test.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mcds.catchment.isochrone import polygon_area_sq_km, simplify_ring  # noqa: E402
from mcds.config import load_deal, load_vertical, validate  # noqa: E402
from mcds.moe import Estimate, proportion_moe, sum_estimates  # noqa: E402
from mcds.pipeline import apply_priors, run, score  # noqa: E402
from mcds.reasoning.classify import classify_from_priors  # noqa: E402
from mcds.supply.places import chain_key, detect_chains, strip_transient  # noqa: E402
from mcds.provenance import ProvenanceError, verify  # noqa: E402
from mcds.reasoning.client import RoutingConfig, build_request  # noqa: E402
from mcds.scoring.geography import find_clusters, haversine_km, point_in_polygon  # noqa: E402
from mcds.scoring.indices import (  # noqa: E402
    accessibility,
    band_share,
    compute_balance,
    compute_demand_index,
    compute_supply_index,
    entrenchment,
)

FIXTURES = ROOT / "fixtures"


# --- entrenchment -----------------------------------------------------------


def test_entrenchment_reference_point_is_one():
    """150 reviews at 4.5 stars is the calibration point and must score exactly 1.0."""
    value, estimated = entrenchment(rating=4.5, review_count=150)
    assert value == pytest.approx(1.0, abs=1e-9)
    assert estimated is False


def test_entrenchment_compresses_heavy_tail():
    """A 10x review count must not produce anything close to 10x the weight.

    Without log compression a single chain would outweigh every independent in
    the catchment combined, which is not how competitive pressure works.
    """
    small, _ = entrenchment(rating=4.5, review_count=100)
    large, _ = entrenchment(rating=4.5, review_count=1000)
    assert large / small < 1.6


def test_entrenchment_rating_clamped_at_both_ends():
    low, _ = entrenchment(rating=1.0, review_count=150)
    high, _ = entrenchment(rating=5.0, review_count=150)
    assert low == pytest.approx(0.4, abs=1e-9)   # RATING_WEIGHT_MIN
    assert high == pytest.approx(1.3, abs=1e-9)  # RATING_WEIGHT_MAX


def test_entrenchment_missing_data_flags_itself():
    value, estimated = entrenchment(rating=None, review_count=None)
    assert value == 0.6
    assert estimated is True, "a defaulted entrenchment must announce itself"


# --- accessibility ----------------------------------------------------------


def test_accessibility_halves_at_catchment_edge():
    """The decay constant is defined so the catchment edge counts exactly half."""
    assert accessibility(10.0, catchment_minutes=10.0) == pytest.approx(0.5, abs=1e-9)
    assert accessibility(0.0, catchment_minutes=10.0) == pytest.approx(1.0)


def test_accessibility_monotonic():
    times = [0, 2, 5, 9, 14]
    values = [accessibility(t, 10.0) for t in times]
    assert values == sorted(values, reverse=True)


# --- supply index -----------------------------------------------------------


def _competitor(pid, sub="direct", rating=4.5, reviews=150, mins=0.0, chain=None):
    return {
        "place_id": pid, "name": pid, "substitutability": sub, "rating": rating,
        "user_rating_count": reviews, "drive_time_minutes": mins, "chain_group": chain,
        "location": {"lat": 40.74, "lng": -73.92},
    }


def test_supply_index_hand_computed():
    """One direct competitor at the reference point and at the door weighs 1.0."""
    result = compute_supply_index([_competitor("a")], catchment_minutes=10.0)
    assert result.index == pytest.approx(1.0, abs=1e-9)


def test_substitutability_scales_weight():
    for sub, expected in [("direct", 1.0), ("partial", 0.5), ("adjacent", 0.15), ("none", 0.0)]:
        r = compute_supply_index([_competitor("a", sub=sub)], catchment_minutes=10.0)
        assert r.index == pytest.approx(expected, abs=1e-9), sub


def test_chain_damping_is_sublinear():
    """Four locations of one operator must weigh less than four independents.

    Damped at k**-0.5 each, so the chain totals sqrt(4) = 2.0 rather than 4.0.
    """
    chain = [_competitor(f"c{i}", chain="MegaWash") for i in range(4)]
    independents = [_competitor(f"i{i}") for i in range(4)]
    chained = compute_supply_index(chain, catchment_minutes=10.0)
    solo = compute_supply_index(independents, catchment_minutes=10.0)
    assert solo.index == pytest.approx(4.0, abs=1e-9)
    assert chained.index == pytest.approx(2.0, abs=1e-9)
    assert any("Chain locations were damped" in w for w in chained.warnings)


def test_incomplete_census_warns_and_marks_undercount():
    """More places in the catchment than the API would name is a known undercount."""
    r = compute_supply_index(
        [_competitor(f"c{i}") for i in range(20)], catchment_minutes=10.0, census_count=340
    )
    assert r.census_complete is False
    assert any("undercount" in w for w in r.warnings)


def test_missing_drive_time_warns():
    r = compute_supply_index(
        [_competitor("a", mins=None)], catchment_minutes=10.0
    )
    assert any("no routed drive time" in w for w in r.warnings)


# --- demand -----------------------------------------------------------------


def _bands():
    return [
        {"band": "low", "min": 0, "max": 49999, "estimate": Estimate(400, 40)},
        {"band": "mid", "min": 50000, "max": 99999, "estimate": Estimate(400, 40)},
        {"band": "top", "min": 100000, "max": None, "estimate": Estimate(200, 20)},
    ]


def test_band_share_on_exact_boundary():
    num, den, notes = band_share(_bands(), minimum=50000)
    assert den.value == 1000
    assert num.value == 600
    assert notes == [], "a filter on a band edge approximates nothing"


def test_band_share_interpolates_and_says_so():
    """A filter cutting mid-band apportions uniformly and must flag that it did.

    Band edges are normalised to the next band's floor, so the 50k band spans a
    true 50,000 and a 75,000 filter takes exactly half. Using the printed
    "$50,000 to $99,999" width would take 49.999%, and that bias compounds
    across every apportioned band in the distribution.
    """
    num, _, notes = band_share(_bands(), minimum=75000)
    assert num.value == pytest.approx(400 * 0.5 + 200, abs=1e-9)
    assert any("uniformly" in n for n in notes)


def test_open_topped_tail_is_excluded_and_flagged_not_guessed():
    """A filter floor landing inside an unbounded band has no honest answer.

    Including the whole "$100,000 or more" band against a $150,000 floor
    overstates; apportioning it invents a shape for the tail. The module drops
    it, which understates in a known direction, and says so.
    """
    num, _, notes = band_share(_bands(), minimum=150000)
    assert num.value == 0.0
    assert any("understates" in n for n in notes)


def test_open_topped_tail_included_when_its_floor_clears_the_filter():
    num, _, notes = band_share(_bands(), minimum=100000)
    assert num.value == 200
    assert notes == []


def test_band_share_categorical_selector():
    bands = [
        {"band": "owner_occupied", "min": None, "max": None, "estimate": Estimate(300, 30)},
        {"band": "renter_occupied", "min": None, "max": None, "estimate": Estimate(700, 70)},
    ]
    num, den, _ = band_share(bands, include_bands=["renter_occupied"])
    assert (num.value, den.value) == (700, 1000)


def _demographics(total=10_000, moe=1_000):
    return {
        "households": Estimate(total, moe),
        "distributions": {"household_income": _bands()},
    }


def test_demand_index_multiplies_shares():
    vertical = {
        "demand": {
            "combination": "multiplicative",
            "filters": [{"dimension": "household_income", "min": 50000, "unit": "household"}],
        }
    }
    r = compute_demand_index(_demographics(), vertical)
    assert r.qualified_share == pytest.approx(0.6)
    assert r.qualified_households.value == pytest.approx(6000)


def test_independence_penalty_widens_only_the_interval():
    """The penalty must move the bounds, never the point estimate."""
    demo = _demographics()
    demo["distributions"]["tenure"] = [
        {"band": "owner_occupied", "min": None, "max": None, "estimate": Estimate(200, 20)},
        {"band": "renter_occupied", "min": None, "max": None, "estimate": Estimate(800, 80)},
    ]
    filters = [
        {"dimension": "household_income", "min": 50000, "unit": "household"},
        {"dimension": "tenure", "include_bands": ["renter_occupied"], "unit": "household"},
    ]
    without = compute_demand_index(demo, {"demand": {"filters": filters, "independence_penalty": False}})
    with_penalty = compute_demand_index(demo, {"demand": {"filters": filters, "independence_penalty": True}})

    assert with_penalty.qualified_households.value == pytest.approx(without.qualified_households.value)
    assert with_penalty.qualified_households.moe > without.qualified_households.moe
    assert with_penalty.independence_penalty_applied is True
    assert any("independent of each other" in w for w in with_penalty.warnings)


def test_primary_only_picks_the_most_selective_filter():
    demo = _demographics()
    demo["distributions"]["tenure"] = [
        {"band": "a", "min": None, "max": None, "estimate": Estimate(100, 10)},
        {"band": "b", "min": None, "max": None, "estimate": Estimate(900, 90)},
    ]
    vertical = {
        "demand": {
            "combination": "primary_only",
            "filters": [
                {"dimension": "household_income", "min": 50000},
                {"dimension": "tenure", "include_bands": ["a"]},
            ],
        }
    }
    r = compute_demand_index(demo, vertical)
    assert r.qualified_share == pytest.approx(0.1), "0.1 is more selective than 0.6"
    assert len(r.filters_applied) == 1


def test_person_level_filter_on_households_warns():
    demo = _demographics()
    demo["distributions"]["age"] = [
        {"band": "18-64", "min": 18, "max": 64, "estimate": Estimate(600, 60)},
        {"band": "65+", "min": 65, "max": None, "estimate": Estimate(400, 40)},
    ]
    vertical = {"demand": {"filters": [{"dimension": "age", "min": 18, "max": 64, "unit": "person"}]}}
    r = compute_demand_index(demo, vertical)
    assert any("measured per person" in w for w in r.warnings)


def test_missing_distribution_warns_about_overstating():
    vertical = {"demand": {"filters": [{"dimension": "nonexistent", "min": 1}]}}
    r = compute_demand_index(_demographics(), vertical)
    assert any("overstated" in w for w in r.warnings)


# --- balance ----------------------------------------------------------------


def test_unsourced_benchmark_suppresses_the_verdict():
    """The single most important behaviour in the module.

    A confident "underserved" derived from an invented denominator is the most
    dangerous output this tool could produce, so an unsourced benchmark must
    yield no verdict rather than a plausible one.
    """
    demand = compute_demand_index(
        _demographics(), {"demand": {"filters": [{"dimension": "household_income", "min": 50000}]}}
    )
    supply = compute_supply_index([_competitor("a")], catchment_minutes=10.0)

    for benchmark in (
        None,
        {},
        {"households_per_location": 1500},                                  # no source
        {"households_per_location": 1500, "source": "x", "confidence": "none"},
        {"households_per_location": 1500, "confidence": "high"},            # still no source
    ):
        b = compute_balance(demand, supply, benchmark)
        assert b.verdict == "suppressed", benchmark
        assert b.ratio is None
        # The underlying figure still stands; only the verdict is withheld.
        assert b.households_per_effective_competitor.value > 0


def test_sourced_benchmark_produces_a_verdict():
    demand = compute_demand_index(
        _demographics(), {"demand": {"filters": [{"dimension": "household_income", "min": 50000}]}}
    )
    supply = compute_supply_index([_competitor("a")], catchment_minutes=10.0)
    sourced = {
        "households_per_location": 1500, "source": "CBP 2022 / ACS 2023",
        "source_url": "https://api.census.gov/data/2022/cbp", "confidence": "medium",
        "method": "cbp_derived", "vintage": 2022,
    }
    b = compute_balance(demand, supply, sourced)
    # 6000 qualified / (1.0 supply + 1 subject) = 3000 per competitor, vs 1500.
    assert b.households_per_effective_competitor.value == pytest.approx(3000)
    assert b.ratio == pytest.approx(2.0)
    assert b.verdict == "underserved"


def test_balance_counts_the_subject_business_itself():
    """The +1 matters: a buyer entering an empty market becomes its competitor."""
    demand = compute_demand_index(
        _demographics(), {"demand": {"filters": [{"dimension": "household_income", "min": 50000}]}}
    )
    empty = compute_supply_index([], catchment_minutes=10.0)
    b = compute_balance(demand, empty, None)
    assert b.households_per_effective_competitor.value == pytest.approx(6000), (
        "with zero competitors demand is divided by 1, not by 0"
    )


@pytest.mark.parametrize(
    "supply_index,expected",
    [(0.0, "underserved"), (3.0, "balanced"), (10.0, "oversupplied")],
)
def test_verdict_bands(supply_index, expected):
    demand = compute_demand_index(
        _demographics(), {"demand": {"filters": [{"dimension": "household_income", "min": 50000}]}}
    )
    supply = compute_supply_index(
        [_competitor(f"c{i}") for i in range(int(supply_index))], catchment_minutes=10.0
    )
    sourced = {
        "households_per_location": 1500, "source": "s", "source_url": "u", "confidence": "high",
    }
    assert compute_balance(demand, supply, sourced).verdict == expected


# --- margins of error -------------------------------------------------------


def test_moe_of_a_sum_is_root_sum_of_squares():
    total = sum_estimates([Estimate(100, 30), Estimate(200, 40)])
    assert total.value == 300
    assert total.moe == pytest.approx(50.0)  # sqrt(900 + 1600)


def test_missing_component_moe_propagates_as_unknown():
    total = sum_estimates([Estimate(100, 30), Estimate(200, None)])
    assert total.moe is None, "an unknown component must not be treated as zero"


def test_proportion_moe_falls_back_when_radicand_negative():
    """Census directs a switch to the ratio formula; the result must stay real."""
    result = proportion_moe(Estimate(900, 10), Estimate(1000, 400))
    assert result is not None and result > 0 and not math.isnan(result)


def test_scaled_estimate_scales_its_moe():
    scaled = Estimate(1000, 200).scaled(0.25)
    assert (scaled.value, scaled.moe) == (250.0, 50.0)


def test_lower_bound_clamped_at_zero():
    assert Estimate(100, 250).lo == 0.0


# --- geography --------------------------------------------------------------


def test_dbscan_isolates_a_corridor():
    corridor = [
        {"place_id": f"c{i}", "location": {"lat": 40.700 + 0.003 * i, "lng": -74.0},
         "substitutability": "direct"}
        for i in range(4)
    ]
    outliers = [
        {"place_id": "far", "location": {"lat": 40.760, "lng": -74.06}, "substitutability": "direct"}
    ]
    clusters, unclustered = find_clusters(corridor + outliers, eps_km=0.8, min_samples=3)
    assert len(clusters) == 1
    assert clusters[0].size == 4
    assert unclustered == ["far"]


def test_haversine_against_known_distance():
    # Roughly one degree of latitude.
    assert haversine_km((40.0, -74.0), (41.0, -74.0)) == pytest.approx(111.2, abs=0.5)


def test_point_in_polygon():
    ring = [[-74.05, 40.65], [-73.95, 40.65], [-73.95, 40.75], [-74.05, 40.75], [-74.05, 40.65]]
    assert point_in_polygon((40.70, -74.00), ring) is True
    assert point_in_polygon((40.80, -74.00), ring) is False


# --- catchment geometry -----------------------------------------------------


def test_polygon_area_is_spherical_not_planar():
    """A planar shoelace on raw degrees is off by cos(latitude) -- 24% at 40N."""
    one_degree_square = [[0, 40], [1, 40], [1, 41], [0, 41], [0, 40]]
    assert polygon_area_sq_km(one_degree_square) == pytest.approx(9400, rel=0.02)


def test_polygon_area_is_orientation_independent():
    ring = [[0, 40], [1, 40], [1, 41], [0, 41], [0, 40]]
    assert polygon_area_sq_km(ring) == pytest.approx(polygon_area_sq_km(ring[::-1]))


def test_degenerate_ring_has_no_area():
    assert polygon_area_sq_km([[0, 40], [1, 40], [0, 40]]) == 0.0


def test_simplify_reduces_vertices_and_stays_closed():
    ring = json.loads((FIXTURES / "sunnyside_catchment.json").read_text())
    ring = ring["polygon"]["coordinates"][0]
    simplified = simplify_ring(ring, 0.25)
    assert len(simplified) < len(ring)
    assert simplified[0] == simplified[-1], "a simplified ring must still close"
    assert len(simplified) >= 4


def test_simplification_tolerance_trades_shape_for_vertices():
    """The tolerance-to-distortion relationship must be monotonic and visible.

    Simplification changes the area being measured, so callers have to be able
    to reason about the cost. A catchment quietly shrunk to fit a request limit
    is a silently wrong demand estimate.
    """
    ring = json.loads((FIXTURES / "sunnyside_catchment.json").read_text())
    ring = ring["polygon"]["coordinates"][0]
    base = polygon_area_sq_km(ring)

    counts, drifts = [], []
    for tol in (0.05, 0.1, 0.25, 0.5):
        s = simplify_ring(ring, tol)
        counts.append(len(s))
        drifts.append(abs(polygon_area_sq_km(s) - base) / base)

    assert counts == sorted(counts, reverse=True), "more tolerance, fewer vertices"
    assert drifts == sorted(drifts), "more tolerance, more area distortion"
    assert drifts[0] < 0.01, "a gentle tolerance should barely move the area"


def test_tiny_ring_passes_through_untouched():
    triangle = [[0, 40], [1, 40], [0.5, 41], [0, 40]]
    assert simplify_ring(triangle, 5.0) == triangle


# --- provenance -------------------------------------------------------------


SCORECARD = {
    "demand": {"qualified_households": {"value": 12431.0}, "qualified_share": 0.338},
    "supply": {"index": 4.68},
    "balance": {"ratio": 0.84},
}


def _findings(memo: str, *, ref="demand.qualified_households.value", falsifier="a site visit"):
    return {
        "flags": [{
            "severity": "material", "statement": "s", "confidence": "medium",
            "falsifier": falsifier,
            "evidence": [{"source_ref": ref, "value": 12431}],
        }],
        "claim_verdicts": [],
        "memo_markdown": memo,
    }


def test_provenance_rejects_a_fabricated_figure():
    """The rail that matters: a number with no computed origin stops the render."""
    findings = _findings("Revenue is roughly $847,000 per year.")
    violations = verify(findings, SCORECARD, strict=False)
    assert any(v.kind == "untraceable_number" for v in violations)
    with pytest.raises(ProvenanceError):
        verify(findings, SCORECARD, strict=True)


def test_provenance_accepts_rounded_and_percentage_forms():
    """Rounding for readability is not fabrication."""
    memo = "About 12,400 qualified households (33.8% of the catchment), ratio 0.84."
    assert verify(_findings(memo), SCORECARD, strict=False) == []


def test_provenance_rejects_an_unresolvable_citation():
    violations = verify(_findings("Fine.", ref="demand.does_not_exist"), SCORECARD, strict=False)
    assert any(v.kind == "unresolved_ref" for v in violations)


def test_provenance_requires_a_falsifier_on_every_flag():
    violations = verify(_findings("Fine.", falsifier="  "), SCORECARD, strict=False)
    assert any(v.kind == "missing_falsifier" for v in violations)


def test_provenance_allows_years_and_small_counts():
    memo = "3 of the 9 competitors opened before 2019."
    assert verify(_findings(memo), SCORECARD, strict=False) == []


# --- vertical config integrity ----------------------------------------------


@pytest.mark.parametrize("vertical_id", ["coffee_shop", "laundromat", "fitness_studio"])
def test_every_listed_place_type_has_a_prior(vertical_id):
    """A type pulled from Google but absent from the priors scores as `none`.

    That is a silent understatement of supply, so the two lists must not drift
    apart. This is the offline half of the check; the live half is
    `probe.py --validate-types`, which asks Google whether the types exist.
    """
    vertical = load_vertical(vertical_id)
    places = vertical["places"]
    listed = set(
        places.get("direct_types", [])
        + places.get("adjacent_types", [])
        + places.get("context_types", [])
    )
    priors = set((vertical["substitution"]["priors"] or {}).keys())
    assert listed - priors == set(), (
        f"{vertical_id}: types fetched but with no prior: {sorted(listed - priors)}"
    )
    assert priors - listed == set(), (
        f"{vertical_id}: priors for types never fetched: {sorted(priors - listed)}"
    )


def test_laundromat_does_not_reference_the_rejected_dry_cleaner_type():
    """Regression: the live API rejects `dry_cleaner` as a filterable type."""
    vertical = load_vertical("laundromat")
    all_types = (
        vertical["places"]["direct_types"]
        + vertical["places"]["adjacent_types"]
        + vertical["places"]["context_types"]
    )
    assert "dry_cleaner" not in all_types


# --- zero-cost path ---------------------------------------------------------


def test_chain_key_strips_branch_numbers_not_brands():
    assert chain_key("SuperClean Laundry #3") == chain_key("SuperClean Laundry #7")
    assert chain_key("SuperClean Laundry #3") != chain_key("Skillman Wash & Fold")


def test_detect_chains_needs_two_locations():
    """A unique name is not a chain of one."""
    solo = [{"place_id": "a", "name": "Corner Wash"}]
    assert detect_chains(solo) == {}

    pair = [
        {"place_id": "a", "name": "SuperClean Laundry #3"},
        {"place_id": "b", "name": "SuperClean Laundry #7"},
        {"place_id": "c", "name": "Corner Wash"},
    ]
    groups = detect_chains(pair)
    assert groups["a"] == groups["b"]
    assert "c" not in groups


def test_priors_classification_is_always_low_confidence():
    """Category priors cannot see the business, only its label. Say so."""
    vertical = load_vertical("laundromat")
    competitors = [
        {"place_id": "a", "primary_type": "laundry", "types": ["laundry"]},
        {"place_id": "b", "primary_type": "convenience_store", "types": ["convenience_store"]},
    ]
    result = classify_from_priors(competitors, vertical)
    assert [c["substitutability"] for c in result["classifications"]] == ["direct", "none"]
    assert all(c["confidence"] == "low" for c in result["classifications"])
    assert any("category-level" in c for c in result["coverage_concerns"])


def test_priors_cannot_separate_businesses_google_files_together():
    """The concrete cost of the zero-cost path, pinned as a test.

    Google has no dry-cleaner type: both a coin laundromat and a drop-off dry
    cleaner arrive typed `laundry`. Priors see one label and score both as
    direct competitors, overstating supply. Only a classifier reading the name
    can tell them apart, and this asserts the gap rather than papering over it.
    """
    vertical = load_vertical("laundromat")
    competitors = [
        {"place_id": "a", "name": "24hr Coin Laundry", "primary_type": "laundry",
         "types": ["laundry"]},
        {"place_id": "b", "name": "Elite Dry Cleaning", "primary_type": "laundry",
         "types": ["laundry"]},
    ]
    levels = [
        c["substitutability"]
        for c in classify_from_priors(competitors, vertical)["classifications"]
    ]
    assert levels == ["direct", "direct"], (
        "priors score both as direct; the dry cleaner is a false positive that "
        "a model would catch from the name"
    )


def test_unknown_place_type_is_reported_not_hidden():
    """Scoring an unrecognised type as a non-competitor understates supply."""
    vertical = load_vertical("laundromat")
    result = classify_from_priors(
        [{"place_id": "a", "primary_type": "car_wash", "types": ["car_wash"]}], vertical
    )
    assert result["classifications"][0]["substitutability"] == "none"
    assert any("car_wash" in c for c in result["coverage_concerns"])


def test_no_llm_run_produces_a_scorecard_and_warns_about_it():
    """The whole pipeline must be runnable at zero model spend."""
    deal = load_deal(ROOT / "examples" / "sample_deal.yaml")
    result = run(deal, dry_run=True, use_llm=False)
    validate(result.scorecard, "scorecard")
    assert result.findings is None
    assert result.scorecard["supply"]["index"] > 0
    assert any(
        "category priors" in w for w in result.scorecard["warnings"]
    ), "a priors-only supply read must announce itself in the memo"


def test_apply_priors_finds_the_chain_without_a_model():
    competitors = json.loads((FIXTURES / "sunnyside_competitors.json").read_text())
    for c in competitors:
        c.pop("substitutability", None)
        c.pop("chain_group", None)
    apply_priors(competitors, load_vertical("laundromat"))
    chained = [c for c in competitors if c.get("chain_group")]
    assert len(chained) == 2
    assert all(c["substitutability"] for c in competitors)


def test_strip_transient_drops_everything_the_tos_forbids():
    record = {
        "place_id": "abc", "name": "Joe Coffee", "rating": 4.5,
        "user_rating_count": 210, "substitutability": "direct", "weight": 0.9,
    }
    kept = strip_transient(record)
    assert kept["place_id"] == "abc"
    assert kept["persistable"] is True
    assert kept["substitutability"] == "direct", "our own judgments are ours to keep"
    for forbidden in ("name", "rating", "user_rating_count"):
        assert forbidden not in kept


# --- model routing ----------------------------------------------------------


def test_fable_gets_no_thinking_parameter():
    """Sending `thinking` to Fable 5 is a 400: thinking is unconditionally on."""
    req = build_request("synthesize", RoutingConfig.from_settings({}), system="s", messages=[])
    assert req["model"] == "claude-fable-5"
    assert "thinking" not in req
    assert req["output_config"]["effort"] == "xhigh"


def test_older_generation_gets_a_token_budget_and_no_effort():
    """Haiku 4.5 rejects `effort` and needs an explicit thinking budget."""
    req = build_request("normalize", RoutingConfig.from_settings({}), system="s", messages=[])
    assert req["model"] == "claude-haiku-4-5"
    assert req["thinking"] == {"type": "enabled", "budget_tokens": 4000}
    assert "output_config" not in req


def test_adaptive_models_get_adaptive_thinking_and_effort():
    req = build_request("classify", RoutingConfig.from_settings({}), system="s", messages=[])
    assert req["thinking"] == {"type": "adaptive"}
    assert req["output_config"]["effort"] == "medium"


def test_refusal_fallbacks_only_on_the_models_that_can_refuse():
    routing = RoutingConfig.from_settings({})
    assert build_request("synthesize", routing, system="s", messages=[])["fallbacks"] == "default"
    assert "fallbacks" not in build_request("normalize", routing, system="s", messages=[])


def test_zero_data_retention_degrades_fable_rather_than_failing():
    """Fable 5 is not offered under ZDR; degrade at config time, not request time."""
    routing = RoutingConfig.from_settings({"reasoning": {"org_requires_zdr": True}})
    assert routing.model_for("synthesize") == "claude-opus-5"


# --- schemas and end-to-end -------------------------------------------------


@pytest.mark.parametrize(
    "fixture,schema",
    [
        ("sunnyside_catchment.json", "catchment"),
        ("sunnyside_demographics.json", "demographics"),
    ],
)
def test_fixtures_validate(fixture, schema):
    validate(json.loads((FIXTURES / fixture).read_text()), schema)


def test_competitor_fixtures_validate():
    for record in json.loads((FIXTURES / "sunnyside_competitors.json").read_text()):
        validate(record, "competitor")


def test_sample_deal_validates():
    assert load_deal(ROOT / "examples" / "sample_deal.yaml")["vertical"] == "laundromat"


@pytest.mark.parametrize("vertical_id", ["coffee_shop", "laundromat", "fitness_studio"])
def test_every_seed_vertical_loads_and_scores(vertical_id):
    """A vertical config must be sufficient to drive a full scoring run."""
    vertical = load_vertical(vertical_id)
    deal = {"deal_id": f"T-{vertical_id}", "vertical": vertical_id, "address": "x"}
    catchment = json.loads((FIXTURES / "sunnyside_catchment.json").read_text())
    demographics = json.loads((FIXTURES / "sunnyside_demographics.json").read_text())
    competitors = json.loads((FIXTURES / "sunnyside_competitors.json").read_text())
    card = score(deal, vertical, catchment, demographics, competitors)
    assert card["vertical"] == vertical_id
    assert card["balance"]["verdict"] == "suppressed", "seed configs ship unsourced"


def test_dry_run_end_to_end():
    deal = load_deal(ROOT / "examples" / "sample_deal.yaml")
    result = run(deal, dry_run=True)
    card = result.scorecard
    validate(card, "scorecard")
    assert card["demand"]["qualified_households"]["value"] > 0
    assert card["supply"]["index"] > 0
    assert card["supply"]["by_substitutability"]["direct"] == 9, (
        "the seller claimed two nearby laundromats; the fixture holds nine direct"
    )
    assert "Read this first" in result.memo_markdown
    assert result.memo_markdown.index("Read this first") < result.memo_markdown.index("Headline"), (
        "caveats must precede conclusions"
    )
    assert result.findings is None, "a dry run must not invoke any model"
