"""Substitutability classification -- which listed businesses actually compete.

This is the judgment the whole supply side rests on. Google's `types` are far
too coarse to answer it: a fast-casual salad counter and a steakhouse are both
`restaurant`, and treating them as interchangeable produces a Supply Index that
describes no real market.

Run as ONE batched call over every competitor rather than one call each. That is
cheaper at these volumes, and more importantly it is more consistent: ranking is
relative, and a model that sees all thirty competitors together grades them
against each other instead of against thirty separate impressions of "typical".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence

from .client import RoutingConfig, cached_system, call_structured

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schemas" / "classification.schema.json"

SYSTEM_RUBRIC = """\
You are classifying competitive substitutability for a small-business acquisition \
screen. A buyer will use this to decide whether a market is crowded.

Assign each business one of four levels:

  direct    A customer choosing this instead of the subject business is making \
the same purchase. Full competitive weight.
  partial   Overlapping occasions but a different core offer; captures some but \
not most of the same trips.
  adjacent  Occasionally substitutes at the margin. Real, small.
  none      Shares a category label but not a customer decision.

What matters more than the category label:
  - The trip being replaced, not the goods sold. A gas-station coffee counter on \
the inbound side of a commute can substitute more completely than a better cafe \
two blocks off the route.
  - Format and price tier. A high-volume low-price gym and a boutique studio \
barely compete despite sharing a type.
  - Operating hours against the demand pattern. A business closed when the \
demand occurs is not a substitute for it.

You will be given priors from the vertical config. Treat them as a starting \
point, not an instruction. Override any prior the evidence contradicts, set \
overrode_prior true, and give the reason.

Set `chain_group` when several locations share an operator, including where the \
trade names differ but the operator evidently does not. Downstream scoring damps \
chains so a six-store operator is not counted as six independent competitors.

Two things earn a `low` confidence rather than a guess: a business whose nature \
is genuinely unclear from name and type, and one whose price tier is missing \
where price tier is what separates the formats.

Use `coverage_concerns` for competition you believe exists but this data cannot \
show: municipal recreation facilities, employer or apartment-complex amenities, \
in-home substitutes, businesses too new to be listed. A buyer needs to know what \
the count is missing, and that is not visible from the records themselves.
"""


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def build_prompt(competitors: Sequence[dict], vertical: dict) -> tuple[list[dict], str]:
    """Return (system_blocks, user_message).

    The rubric and the vertical config form the cached prefix; the competitor
    list is the volatile suffix. Across a batch of deals in the same vertical
    this is one full-price call and the rest cache reads.
    """
    sub = vertical.get("substitution", {}) or {}
    stable = (
        SYSTEM_RUBRIC
        + f"\n\nVertical: {vertical.get('label')} ({vertical.get('id')})\n"
        + f"Priors by place type: {json.dumps(sub.get('priors', {}), indent=2)}\n"
        + f"Category guidance: {sub.get('guidance', '').strip()}\n"
    )
    rows = [
        {
            "place_id": c["place_id"],
            "name": c.get("name"),
            "primary_type": c.get("primary_type"),
            "types": c.get("types"),
            "price_level": c.get("price_level"),
            "rating": c.get("rating"),
            "user_rating_count": c.get("user_rating_count"),
            "open_24h": c.get("open_24h"),
            "drive_time_minutes": c.get("drive_time_minutes"),
        }
        for c in competitors
    ]
    user = (
        "Classify every business below. Return one entry per place_id, none "
        "omitted.\n\n" + json.dumps(rows, indent=2)
    )
    return cached_system(stable), user


def classify_from_priors(competitors: Sequence[dict], vertical: dict) -> dict:
    """Classify using only the vertical config priors -- no model, no cost.

    Every vertical config already maps Google place types to a substitutability
    level. Applying that map directly is deterministic, free, and instant, which
    makes the whole pipeline runnable at zero LLM spend.

    What it gives up is the judgment the classification stage exists for. Priors
    see a category label; a model sees the business. It cannot tell a $10/month
    high-volume gym from a $200/month boutique studio, both typed `gym`. It
    cannot separate a self-service laundromat from a drop-off dry cleaner when
    Google files both under `laundry`. It cannot notice that a gas-station
    coffee counter on the commute side of the road substitutes more completely
    than a better cafe two blocks off it.

    So every classification here is marked `low` confidence and the run carries
    a warning saying the supply read is category-level only. That is the honest
    label: this is a usable first pass, not the analysis the PRD describes.
    """
    priors = (vertical.get("substitution", {}) or {}).get("priors", {}) or {}
    out = []
    unknown_types: set[str] = set()

    for c in competitors:
        candidates = [c.get("primary_type")] + list(c.get("types") or [])
        level = None
        matched = None
        for t in candidates:
            if t and t in priors:
                level, matched = priors[t], t
                break
        if level is None:
            level = "none"
            for t in candidates:
                if t:
                    unknown_types.add(t)

        out.append({
            "place_id": c["place_id"],
            "substitutability": level,
            "confidence": "low",
            "reason": (
                f"Config prior for place type '{matched}'."
                if matched
                else "No config prior matched this place's types; scored as a "
                     "non-competitor, which may understate supply."
            ),
            "overrode_prior": False,
            "chain_group": c.get("chain_group"),
        })

    concerns = [
        "Substitutability came from category priors rather than a model, so "
        "every call is category-level. Businesses sharing a Google type but "
        "serving different customers -- a budget gym and a boutique studio, a "
        "self-service laundromat and a drop-off dry cleaner -- are scored "
        "identically here.",
        "Competition invisible to Places data was not assessed at all: "
        "municipal facilities, employer and apartment-complex amenities, "
        "in-home substitutes, and businesses too new to be listed.",
    ]
    if unknown_types:
        concerns.append(
            "These place types had no config prior and were scored as "
            "non-competitors: " + ", ".join(sorted(unknown_types)) + ". If any "
            "of them do compete, add them to the vertical config."
        )

    return {"classifications": out, "coverage_concerns": concerns}


def classify(
    competitors: Sequence[dict],
    vertical: dict,
    routing: RoutingConfig,
    *,
    api_key: str | None = None,
) -> tuple[dict, list[str]]:
    """Classify a catchment's competitors in one call.

    Returns (result, warnings). Validates that every place_id came back before
    returning: a silently dropped competitor understates supply, which is the
    direction of error that loses money. Anything missing is filled from config
    priors and reported rather than left absent.
    """
    if not competitors:
        return {"classifications": [], "coverage_concerns": []}, []

    system, user = build_prompt(competitors, vertical)
    result = call_structured(
        "classify", routing, system=system, user=user,
        schema=load_schema(), api_key=api_key,
    )

    warnings: list[str] = []
    returned = {c["place_id"] for c in result.get("classifications", [])}
    expected = {c["place_id"] for c in competitors}

    missing = expected - returned
    if missing:
        fallback = classify_from_priors(
            [c for c in competitors if c["place_id"] in missing], vertical
        )
        result["classifications"].extend(fallback["classifications"])
        warnings.append(
            f"The classifier did not return {len(missing)} of {len(expected)} "
            f"competitors. Those fell back to config priors and are tagged low "
            f"confidence; supply for them is category-level only."
        )

    unexpected = returned - expected
    if unexpected:
        result["classifications"] = [
            c for c in result["classifications"] if c["place_id"] in expected
        ]
        warnings.append(
            f"The classifier returned {len(unexpected)} place_id(s) that were "
            f"not in the catchment. They were discarded."
        )

    overrides = [c for c in result["classifications"] if c.get("overrode_prior")]
    if overrides:
        warnings.append(
            f"The classifier overrode the vertical config prior on "
            f"{len(overrides)} competitor(s). Reasons are recorded on each record."
        )
    low = [c for c in result["classifications"] if c.get("confidence") == "low"]
    if low:
        warnings.append(
            f"{len(low)} competitor classification(s) are low confidence, "
            f"usually because the business type is unclear from the name or "
            f"price tier is missing."
        )
    return result, warnings


def apply_classifications(competitors: list[dict], result: dict) -> None:
    """Write classification results onto the competitor records in place."""
    by_id = {c["place_id"]: c for c in result.get("classifications", [])}
    for competitor in competitors:
        decision = by_id.get(competitor["place_id"])
        if not decision:
            continue
        competitor["substitutability"] = decision["substitutability"]
        competitor["substitutability_reason"] = decision.get("reason")
        competitor["classifier_confidence"] = decision.get("confidence")
        competitor["overrode_prior"] = decision.get("overrode_prior", False)
        if decision.get("chain_group"):
            competitor["chain_group"] = decision["chain_group"]
