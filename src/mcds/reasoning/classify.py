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

from .client import RoutingConfig, build_request, cached_system, check_refusal, get_client

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


def classify(
    competitors: Sequence[dict],
    vertical: dict,
    routing: RoutingConfig,
    *,
    api_key: str | None = None,
) -> dict:
    """Classify a catchment's competitors in one call.

    Validates that every place_id came back before returning: a silently dropped
    competitor understates supply, which is the direction of error that loses
    money.
    """
    raise NotImplementedError
