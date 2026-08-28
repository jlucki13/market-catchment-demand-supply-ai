"""Attribute block-group demographics onto a drive-time polygon.

The catchment boundary does not respect Census geography. A 10-minute contour
will cut a block group in half, and how you split that block group decides a
meaningful share of the demand estimate.

Two methods, in order of preference:

  population_weighted -- intersect the block group with the polygon, then weight
    by 2020 Decennial blocks (PL 94-171) whose centroids fall inside. Blocks are
    small enough that this tracks where people actually live. Half a block group
    by area can hold 90% of its households if the other half is a golf course.

  areal -- fall back to the intersected area fraction. Assumes uniform density
    inside the block group, which is wrong wherever land use varies, and land
    use always varies. Used only when block geometry is unavailable, and it sets
    a warning when it is.

Both understate uncertainty in the same way: the overlap fraction is treated as
exact when computing the scaled margin of error, so the reported interval covers
sampling error in the ACS estimate but not error in the apportionment itself.
That limitation is documented rather than hidden -- see Estimate.scaled().
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Sequence

from ..catchment.isochrone import polygon_area_sq_km
from ..moe import Estimate, sum_estimates
from ..scoring.geography import point_in_polygon

WeightMethod = Literal["areal", "population_weighted"]

#: Below this overlap fraction a block group contributes so little that keeping
#: it adds MOE without adding signal. Dropped, and counted in a warning.
MIN_OVERLAP_FRACTION = 0.02


def clip_polygon(
    subject: Sequence[Sequence[float]],
    clip: Sequence[Sequence[float]],
) -> list[list[float]]:
    """Sutherland-Hodgman: the part of `subject` lying inside convex `clip`.

    Sufficient here because block groups are simple polygons and an isochrone
    ring is simple after the largest-ring selection in catchment/isochrone.py.
    The known limitation: Sutherland-Hodgman assumes a CONVEX clip region, and a
    drive-time contour is often concave. Against a concave clip it can leave
    degenerate edges along the boundary, which inflates the intersection
    slightly -- so the fraction is clamped to 1.0 by the caller and the method
    is recorded on every block group.
    """
    def inside(p, a, b):
        return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]) >= 0

    def intersect(p1, p2, a, b):
        x1, y1, x2, y2 = p1[0], p1[1], p2[0], p2[1]
        x3, y3, x4, y4 = a[0], a[1], b[0], b[1]
        denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
        if denom == 0:
            return [x2, y2]
        t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / denom
        return [x1 + t * (x2 - x1), y1 + t * (y2 - y1)]

    output = [list(p) for p in subject[:-1]] if len(subject) > 1 else []
    clip_edges = list(zip(clip[:-1], clip[1:]))

    # Orientation decides which side "inside" means, so normalise to CCW.
    def signed_area(ring):
        return sum(
            (ring[i][0] * ring[(i + 1) % len(ring)][1])
            - (ring[(i + 1) % len(ring)][0] * ring[i][1])
            for i in range(len(ring))
        ) / 2

    if signed_area([list(c) for c in clip[:-1]]) < 0:
        clip_edges = [(b, a) for a, b in reversed(clip_edges)]

    for a, b in clip_edges:
        if not output:
            return []
        buffer, previous = [], output[-1]
        for current in output:
            if inside(current, a, b):
                if not inside(previous, a, b):
                    buffer.append(intersect(previous, current, a, b))
                buffer.append(current)
            elif inside(previous, a, b):
                buffer.append(intersect(previous, current, a, b))
            previous = current
        output = buffer

    return output + [output[0]] if output else []


def polygon_intersection_area(
    ring_a: Sequence[Sequence[float]],
    ring_b: Sequence[Sequence[float]],
) -> float:
    """Area of the intersection of two closed rings, in square kilometres."""
    clipped = clip_polygon(ring_a, ring_b)
    return polygon_area_sq_km(clipped) if len(clipped) >= 4 else 0.0


def overlap_fractions(
    polygon_ring: Sequence[Sequence[float]],
    block_groups: Sequence[dict],
    *,
    method: WeightMethod = "areal",
    blocks_by_bg: dict[str, list[dict]] | None = None,
) -> tuple[list[dict], list[str]]:
    """Fraction of each block group falling inside the catchment.

    Returns (fractions, warnings) where each fraction is
    {"geoid", "overlap_fraction", "weight_method", "fully_contained"}.

    Areal weighting assumes population is spread evenly inside a block group.
    That is wrong wherever land use varies, and land use always varies -- half a
    block group by area can hold 90% of its households if the other half is a
    golf course or a rail yard. It is the honest default only because block
    geometry is a much larger data pull; the assumption is recorded on every
    block group and surfaced in the memo.
    """
    fractions: list[dict] = []
    warnings: list[str] = []
    dropped = 0
    fell_back = 0

    for bg in block_groups:
        ring = bg.get("ring")
        if not ring:
            continue
        whole = polygon_area_sq_km(ring)
        if whole <= 0:
            continue

        blocks = (blocks_by_bg or {}).get(bg["geoid"])
        if method == "population_weighted" and blocks:
            inside = sum(
                b.get("population", 0) for b in blocks
                if point_in_polygon((b["lat"], b["lng"]), polygon_ring)
            )
            total = sum(b.get("population", 0) for b in blocks) or 1
            fraction, used = inside / total, "population_weighted"
        else:
            if method == "population_weighted":
                fell_back += 1
            fraction = min(1.0, polygon_intersection_area(ring, polygon_ring) / whole)
            used = "areal"

        if fraction < MIN_OVERLAP_FRACTION:
            dropped += 1
            continue

        fractions.append({
            "geoid": bg["geoid"],
            "overlap_fraction": round(fraction, 4),
            "weight_method": used,
            "fully_contained": fraction >= 0.999,
        })

    partial = sum(1 for f in fractions if not f["fully_contained"])
    if partial:
        warnings.append(
            f"{partial} of {len(fractions)} block groups are only partly inside "
            f"the catchment and were apportioned by area, which assumes "
            f"population is spread evenly within each. Where land use varies "
            f"sharply -- a rail yard, a park, an industrial strip -- that "
            f"assumption moves the household estimate."
        )
    if dropped:
        warnings.append(
            f"{dropped} block group(s) overlapped the catchment by less than "
            f"{MIN_OVERLAP_FRACTION:.0%} and were excluded; they would add "
            f"margin of error without adding signal."
        )
    if fell_back:
        warnings.append(
            f"Population weighting was requested but block geometry was missing "
            f"for {fell_back} block group(s), which fell back to areal weighting."
        )
    return fractions, warnings


def build_demographics(
    polygon_ring: Sequence[Sequence[float]],
    block_groups: Sequence[dict],
    acs_by_geoid: dict[str, dict],
    dimensions: Sequence[str],
    *,
    method: WeightMethod = "areal",
    vintage: int = 2023,
    dataset: str = "acs/acs5",
) -> tuple[dict, list[str]]:
    """Assemble the catchment demographics object from per-block-group ACS rows.

    This is where the demand half of the analysis is actually decided: every
    household count downstream traces to the fractions computed here.
    """
    from .census import to_bands, total_households

    fractions, warnings = overlap_fractions(
        polygon_ring, block_groups, method=method
    )
    frac_by_geoid = {f["geoid"]: f["overlap_fraction"] for f in fractions}

    households = sum_estimates([
        total_households(acs_by_geoid.get(geoid, {})).scaled(fraction)
        for geoid, fraction in frac_by_geoid.items()
    ]) if frac_by_geoid else Estimate(0.0, 0.0)

    distributions: dict[str, list[dict]] = {}
    for dimension in dimensions:
        per_bg = {
            geoid: to_bands(acs_by_geoid.get(geoid, {}), dimension)
            for geoid in frac_by_geoid
        }
        bands = interpolate_distribution(per_bg, fractions)
        if bands:
            distributions[dimension] = bands

    demographics = {
        "households": households.as_dict(),
        "distributions": {
            dim: [
                {
                    "band": b["band"], "min": b["min"], "max": b["max"],
                    "estimate": b["estimate"].as_dict(),
                }
                for b in bands
            ]
            for dim, bands in distributions.items()
        },
        "block_groups": fractions,
        "source": {
            "dataset": dataset,
            "vintage": vintage,
            "confidence_level": 0.90,
            "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    }

    relative = households.relative_moe
    if relative is not None and relative > 0.25:
        warnings.append(
            f"The catchment household estimate carries a margin of error of "
            f"+/-{relative:.0%} at 90% confidence, which is wide enough that "
            f"the balance verdict may flip inside it. Quote the interval, not "
            f"the point estimate."
        )
    return demographics, warnings


def interpolate_distribution(
    per_block_group: dict[str, list[dict]],
    fractions: Sequence[dict],
) -> list[dict]:
    """Combine per-block-group band distributions into one catchment distribution.

    Each block group's band estimate is scaled by its overlap fraction, then
    summed band-wise with MOEs combined as a root sum of squares.
    """
    by_band: dict[str, list[Estimate]] = {}
    meta: dict[str, dict] = {}
    frac_by_geoid = {f["geoid"]: f["overlap_fraction"] for f in fractions}

    for geoid, bands in per_block_group.items():
        fraction = frac_by_geoid.get(geoid, 0.0)
        if fraction < MIN_OVERLAP_FRACTION:
            continue
        for band in bands:
            key = band["band"]
            by_band.setdefault(key, []).append(band["estimate"].scaled(fraction))
            meta.setdefault(key, {"min": band.get("min"), "max": band.get("max")})

    return [
        {
            "band": key,
            "min": meta[key]["min"],
            "max": meta[key]["max"],
            "estimate": sum_estimates(estimates),
        }
        for key, estimates in by_band.items()
    ]
