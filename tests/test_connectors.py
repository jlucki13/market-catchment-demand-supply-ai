"""Tests for the live connectors, exercised without touching the network.

The value here is in the transformations, not the HTTP: coordinate order,
suppressed-value handling, band construction, polygon clipping, and the
index-keyed reordering of route-matrix rows. Each of those fails silently in
production -- a swapped lat/lng returns an empty catchment that looks like an
empty market, and positional reading of route rows attributes every drive time
to the wrong competitor.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mcds.catchment.isochrone import (  # noqa: E402
    MAX_CONTOUR_MINUTES, fetch_isochrone, polygon_area_sq_km,
)
from mcds.demand.benchmark import _first_value, derive_benchmark  # noqa: E402
from mcds.demand.census import (  # noqa: E402
    SUPPRESSED, _to_number, to_bands, total_households, variables_for,
)
from mcds.demand.interpolate import (  # noqa: E402
    build_demographics, overlap_fractions, polygon_intersection_area,
)
from mcds.http import ApiError, RETRYABLE_STATUS  # noqa: E402
from mcds.moe import Estimate  # noqa: E402
from mcds.reasoning.claims import IMPLAUSIBLE_CAPTURE_RATE, reconcile  # noqa: E402
from mcds.reasoning.classify import apply_classifications  # noqa: E402
from mcds.supply.places import _is_open_24h, to_latlng_polygon  # noqa: E402
from mcds.supply.routing import detour_ratio, next_weekday_at  # noqa: E402

SQUARE = [[0, 40], [1, 40], [1, 41], [0, 41], [0, 40]]


# --- HTTP -------------------------------------------------------------------


def test_permanent_errors_are_not_retried():
    """A 400 fails identically forever; retrying it just burns free-tier calls."""
    assert 400 not in RETRYABLE_STATUS
    assert 403 not in RETRYABLE_STATUS
    assert 429 in RETRYABLE_STATUS and 503 in RETRYABLE_STATUS


def test_api_error_carries_the_response_body():
    """Every API here puts the actionable detail in the body, not the status."""
    error = ApiError("Places", 400, "Type is not supported, type: dry_cleaner")
    assert "dry_cleaner" in str(error)


# --- coordinate order -------------------------------------------------------


def test_google_polygon_swaps_geojson_order():
    """GeoJSON is lng-first; Google's LatLng is lat-first.

    Getting this backwards produces a valid request describing a polygon in the
    wrong hemisphere, which returns zero results rather than an error.
    """
    result = to_latlng_polygon([[-105.04, 39.74]])
    assert result["coordinates"][0] == {"latitude": 39.74, "longitude": -105.04}


# --- isochrone --------------------------------------------------------------


def test_contour_over_the_cap_raises_rather_than_truncating():
    with pytest.raises(ValueError, match="60 minutes"):
        fetch_isochrone(40.0, -74.0, MAX_CONTOUR_MINUTES + 1, access_token="x")


def test_area_is_spherical():
    assert polygon_area_sq_km(SQUARE) == pytest.approx(9400, rel=0.02)


# --- census -----------------------------------------------------------------


def test_suppressed_values_become_none_not_numbers():
    """Census returns -666666666 for suppressed data.

    Treating that as a number produces catastrophic garbage, and it would pass
    every downstream type check.
    """
    for sentinel in SUPPRESSED:
        assert _to_number(sentinel) is None
    assert _to_number("1234") == 1234.0


def test_negative_margin_means_no_sampling_error():
    assert _to_number("-1") is None


def test_income_bands_carry_real_edges_and_an_open_top():
    bands = to_bands({"B19001_002E": 100, "B19001_002M": 20}, "household_income")
    assert len(bands) == 16
    assert bands[0]["max"] == 9999
    assert bands[-1]["max"] is None, "the top bracket must stay open-topped"
    assert bands[0]["estimate"].value == 100


def test_age_bands_sum_male_and_female_with_combined_margins():
    raw = {
        "B01001_003E": 50, "B01001_003M": 12,
        "B01001_027E": 48, "B01001_027M": 11,
    }
    under_five = to_bands(raw, "age")[0]
    assert under_five["estimate"].value == 98
    assert under_five["estimate"].moe == pytest.approx((12**2 + 11**2) ** 0.5)


def test_variable_lists_pair_every_estimate_with_its_margin():
    for dimension in ("household_income", "tenure", "age"):
        variables = variables_for(dimension)
        estimates = {v[:-1] for v in variables if v.endswith("E")}
        margins = {v[:-1] for v in variables if v.endswith("M")}
        assert estimates == margins, f"{dimension} has unpaired variables"


def test_total_households_reads_b11001():
    assert total_households({"B11001_001E": 1500, "B11001_001M": 180}).value == 1500


# --- interpolation ----------------------------------------------------------


def test_clipping_a_square_to_its_left_half():
    half = [[0, 40], [0.5, 40], [0.5, 41], [0, 41], [0, 40]]
    fraction = polygon_intersection_area(SQUARE, half) / polygon_area_sq_km(SQUARE)
    assert fraction == pytest.approx(0.5, abs=0.001)


def test_disjoint_polygons_have_no_intersection():
    far = [[5, 40], [6, 40], [6, 41], [5, 41], [5, 40]]
    assert polygon_intersection_area(SQUARE, far) == 0.0


def test_slivers_are_dropped_and_reported():
    """A 1% overlap adds margin of error without adding signal."""
    sliver = [[0.99, 40], [1.0, 40], [1.0, 41], [0.99, 41], [0.99, 40]]
    block_groups = [{"geoid": "sliver", "ring": SQUARE}]
    fractions, warnings = overlap_fractions(sliver, block_groups)
    assert fractions == []
    assert any("less than" in w for w in warnings)


def test_partial_overlap_warns_about_the_uniformity_assumption():
    half = [[0, 40], [0.5, 40], [0.5, 41], [0, 41], [0, 40]]
    fractions, warnings = overlap_fractions(half, [{"geoid": "bg", "ring": SQUARE}])
    assert fractions[0]["overlap_fraction"] == pytest.approx(0.5, abs=0.01)
    assert fractions[0]["fully_contained"] is False
    assert any("spread evenly" in w for w in warnings)


def test_demographics_scale_by_overlap_and_widen_on_thin_estimates():
    """Half a block group inside the catchment contributes half its households."""
    block_groups = [{"geoid": "bg1", "ring": SQUARE, "state": "08", "county": "031"}]
    acs = {"bg1": {
        "B11001_001E": 1000, "B11001_001M": 400,
        "B19001_002E": 400, "B19001_002M": 100,
        "B19001_017E": 600, "B19001_017M": 150,
    }}
    half = [[0, 40], [0.5, 40], [0.5, 41], [0, 41], [0, 40]]
    demographics, warnings = build_demographics(
        half, block_groups, acs, ["household_income"]
    )
    assert demographics["households"]["value"] == pytest.approx(500, abs=5)
    assert any("margin of error" in w for w in warnings), (
        "a +/-40% household estimate must warn that the verdict may flip inside it"
    )


# --- routing ----------------------------------------------------------------


def test_detour_ratio_matches_the_live_denver_measurement():
    assert detour_ratio(3940, 2.32) == pytest.approx(1.70, abs=0.01)


def test_detour_ratio_is_none_without_a_route():
    assert detour_ratio(None, 2.0) is None


def test_departure_time_is_always_in_the_future():
    """TRAFFIC_AWARE_OPTIMAL rejects a past departure."""
    from datetime import datetime, timezone
    when = datetime.fromisoformat(next_weekday_at(8).replace("Z", "+00:00"))
    assert when > datetime.now(timezone.utc)


# --- places -----------------------------------------------------------------


@pytest.mark.parametrize("hours,expected", [
    ({"periods": [{"open": {"day": 0}}]}, True),
    ({"periods": [{"open": {}, "close": {}}]}, False),
    ({}, None),
])
def test_open_24h_detection(hours, expected):
    assert _is_open_24h(hours) is expected


# --- claims -----------------------------------------------------------------


SCORECARD = {
    "demand": {"qualified_households": {"value": 16990},
               "total_households": {"value": 45580}},
    "supply": {"by_substitutability": {"direct": 9, "adjacent": 2}},
}


def test_competitor_count_claim_is_tested_against_the_census():
    claims = {"claims": [{
        "claim_id": "C4", "kind": "competitor_count",
        "verbatim": "only two other laundromats nearby",
        "parsed": {"competitor_count": 2},
    }]}
    result = reconcile(claims, SCORECARD, {"distributions": {}})[0]
    assert result["testable"] is True
    assert result["computed_actual"] == 9
    assert "9 direct" in result["note"]


def test_volume_claim_inverts_to_an_implied_capture_rate():
    claims = {"claims": [{
        "claim_id": "C3", "kind": "customer_volume", "verbatim": "1,400 a week",
        "parsed": {"customers_per_period": 1400, "period": "week"},
    }]}
    result = reconcile(claims, SCORECARD, {"distributions": {}})[0]
    assert result["computed_actual"] == pytest.approx(1400 / 16990, abs=0.001)


def test_implausible_capture_rate_is_flagged_for_management():
    claims = {"claims": [{
        "claim_id": "C9", "kind": "customer_volume", "verbatim": "9,000 a week",
        "parsed": {"customers_per_period": 9000, "period": "week"},
    }]}
    result = reconcile(claims, SCORECARD, {"distributions": {}})[0]
    assert result["computed_actual"] > IMPLAUSIBLE_CAPTURE_RATE
    assert "asking management" in result["note"]


def test_untestable_claims_say_so_rather_than_being_dropped():
    """A reader must not assume an untested claim was tested."""
    claims = {"claims": [{
        "claim_id": "C1", "kind": "customer_household_type",
        "verbatim": "mostly renters", "parsed": {},
    }]}
    result = reconcile(claims, SCORECARD, {"distributions": {}})[0]
    assert result["testable"] is False
    assert "not tested" in result["note"]


# --- classification application ---------------------------------------------


def test_apply_classifications_only_touches_matched_records():
    competitors = [{"place_id": "a"}, {"place_id": "b"}]
    apply_classifications(competitors, {"classifications": [
        {"place_id": "a", "substitutability": "direct",
         "confidence": "high", "reason": "coin laundry", "chain_group": "X"},
    ]})
    assert competitors[0]["substitutability"] == "direct"
    assert competitors[0]["chain_group"] == "X"
    assert "substitutability" not in competitors[1]


# --- benchmark --------------------------------------------------------------


def test_cbp_row_parsing():
    assert _first_value([["ESTAB", "state"], ["47", "08"]], "ESTAB") == 47.0
    assert _first_value([["ESTAB"]], "ESTAB") is None
    assert _first_value([], "ESTAB") is None
