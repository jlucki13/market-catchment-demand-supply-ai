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

from typing import Literal, Sequence

from ..moe import Estimate, sum_estimates

WeightMethod = Literal["areal", "population_weighted"]

#: Below this overlap fraction a block group contributes so little that keeping
#: it adds MOE without adding signal. Dropped, and counted in a warning.
MIN_OVERLAP_FRACTION = 0.02


def polygon_intersection_area(
    ring_a: Sequence[Sequence[float]],
    ring_b: Sequence[Sequence[float]],
) -> float:
    """Area of the intersection of two closed rings, in square kilometres.

    Sutherland-Hodgman clipping is sufficient here: block groups are simple
    polygons, and an isochrone ring is simple after the largest-ring selection
    in catchment/isochrone.py.
    """
    raise NotImplementedError


def overlap_fractions(
    polygon_ring: Sequence[Sequence[float]],
    block_groups: Sequence[dict],
    *,
    method: WeightMethod = "population_weighted",
    blocks_by_bg: dict[str, list[dict]] | None = None,
) -> list[dict]:
    """Fraction of each block group falling inside the catchment.

    Returns [{"geoid", "overlap_fraction", "weight_method", "fully_contained"}].
    Falls back to areal weighting per block group when block geometry for it is
    missing, so one gap in the block data does not force the whole catchment
    onto the weaker method.
    """
    raise NotImplementedError


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
