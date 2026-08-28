"""Turn the seller's story into propositions that can be checked against data.

v1 takes claims already structured (examples/sample_deal.yaml). This stage
exists for two jobs: normalising a hand-entered claim into the `parsed` shape
the reconciliation reads, and -- in v2 -- extracting claims from a CIM PDF.

The v2 form is why this is a Sonnet 5 stage with a 1M context rather than a
regex: a CIM buries its testable assertions in prose across forty pages, and the
useful ones are rarely in a table.
"""

from __future__ import annotations

from .client import RoutingConfig

SYSTEM_PROMPT = """\
Extract testable claims about the customer base and market from the seller's \
material. A claim is testable when it asserts something a demographic or \
competitive dataset could confirm or contradict: who the customers are, where \
they come from, how many there are, how many competitors exist.

Quote the seller verbatim. A paraphrase loses the hedging, and the hedging is \
often the whole point -- "most of our customers are families" and "our customer \
is a family with $90k household income" are different claims with different \
burdens of proof.

Do not extract financial claims (revenue, SDE, margins). Those are checked \
against the books, not against census data, and mixing them in produces verdicts \
this tool has no basis to give.

Where the seller states a catchment ("we draw from a 5-mile radius"), capture it \
as a catchment_extent claim. It is the most directly checkable claim in most \
listings and the one most often wrong.
"""


def extract_claims(
    document_text: str,
    routing: RoutingConfig,
    *,
    api_key: str | None = None,
) -> dict:
    """v2: pull claims from CIM text into schemas/seller_claims.schema.json shape."""
    raise NotImplementedError(
        "CIM extraction is a v2 item. v1 takes claims as structured input; see "
        "examples/sample_deal.yaml."
    )


#: A claimed capture rate above this share of qualified households is not
#: impossible, but it is a question for management rather than an assumption.
IMPLAUSIBLE_CAPTURE_RATE = 0.35

#: Visits per household per period, used to turn a claimed customer count into
#: an implied household count. Deliberately generous -- the point is to catch
#: claims that fail even under favourable assumptions.
VISITS_PER_HOUSEHOLD = {"day": 0.15, "week": 1.0, "month": 4.0, "year": 50.0}


def reconcile(claims: dict, scorecard: dict, demographics: dict) -> list[dict]:
    """Pre-compute what each claim should be tested against.

    Runs BEFORE synthesis and in code, not in a model: mapping a claim of kind
    `customer_income` to the income distribution and computing the share of the
    catchment inside the claimed band is arithmetic, and arithmetic belongs
    here. The model's job is deciding what the gap means, not measuring it.
    """
    from ..moe import Estimate
    from ..scoring.indices import band_share

    results: list[dict] = []
    distributions = demographics.get("distributions") or {}
    qualified = (scorecard.get("demand") or {}).get("qualified_households") or {}
    households = (scorecard.get("demand") or {}).get("total_households") or {}

    def bands(dimension: str) -> list[dict] | None:
        raw = distributions.get(dimension)
        if not raw:
            return None
        return [
            {
                "band": b["band"], "min": b.get("min"), "max": b.get("max"),
                "estimate": Estimate(b["estimate"]["value"], b["estimate"].get("moe")),
            }
            for b in raw
        ]

    for claim in claims.get("claims", []) or []:
        parsed = claim.get("parsed") or {}
        entry = {
            "claim_id": claim["claim_id"],
            "kind": claim["kind"],
            "verbatim": claim["verbatim"],
            "computed_actual": None,
            "tested_against": None,
            "testable": False,
            "note": None,
        }

        if claim["kind"] == "customer_income":
            income = bands("household_income")
            if income and (parsed.get("income_min") or parsed.get("income_max")):
                numerator, denominator, notes = band_share(
                    income,
                    minimum=parsed.get("income_min"),
                    maximum=parsed.get("income_max"),
                )
                share = numerator.value / denominator.value if denominator.value else 0.0
                entry.update({
                    "computed_actual": round(share, 4),
                    "tested_against": "demand.filters_applied",
                    "testable": True,
                    "note": (
                        f"Households in the claimed income band are "
                        f"{share:.1%} of the catchment."
                        + (" " + " ".join(notes) if notes else "")
                    ),
                })

        elif claim["kind"] == "catchment_extent":
            minutes = (scorecard.get("demand") or {}).get("catchment_minutes")
            radius = parsed.get("radius_miles")
            if radius:
                entry.update({
                    "computed_actual": radius,
                    "tested_against": "supply.strongest.0.drive_time_minutes",
                    "testable": True,
                    "note": (
                        f"The seller claims a {radius}-mile radius. Compare "
                        f"against the drive-time catchment actually analysed: a "
                        f"radius claim ignores barriers, and the detour ratios "
                        f"on nearby competitors show how much that matters here."
                    ),
                })

        elif claim["kind"] == "customer_volume":
            count = parsed.get("customers_per_period")
            period = parsed.get("period")
            base = qualified.get("value")
            if count and period and base:
                visits = VISITS_PER_HOUSEHOLD.get(period, 1.0)
                implied_households = count / visits
                rate = implied_households / base if base else None
                entry.update({
                    "computed_actual": round(rate, 4) if rate else None,
                    "tested_against": "demand.qualified_households.value",
                    "testable": True,
                    "note": (
                        f"{count:,.0f} customers per {period} implies roughly "
                        f"{implied_households:,.0f} households, or {rate:.1%} of "
                        f"the {base:,.0f} qualified households in the catchment."
                        + (
                            " That capture rate is high enough to be worth "
                            "asking management about directly."
                            if rate and rate > IMPLAUSIBLE_CAPTURE_RATE else ""
                        )
                    ),
                })

        elif claim["kind"] == "competitor_count":
            claimed = parsed.get("competitor_count")
            by_sub = (scorecard.get("supply") or {}).get("by_substitutability") or {}
            actual = by_sub.get("direct", 0)
            if claimed is not None:
                entry.update({
                    "computed_actual": actual,
                    "tested_against": "supply.by_substitutability",
                    "testable": True,
                    "note": (
                        f"The seller says {claimed}. The catchment holds "
                        f"{actual} direct substitute(s) plus "
                        f"{by_sub.get('partial', 0)} partial and "
                        f"{by_sub.get('adjacent', 0)} adjacent."
                    ),
                })

        if not entry["testable"]:
            entry["note"] = (
                "No field in this analysis bears on this claim. It is recorded "
                "so the memo can say it was not tested, rather than leaving a "
                "reader to assume it was."
            )
        results.append(entry)

    _ = households  # available for future claim kinds
    return results