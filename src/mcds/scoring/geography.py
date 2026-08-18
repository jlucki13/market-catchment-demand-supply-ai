"""Where the competition sits, and where it does not.

Counting competitors answers "how many"; this module answers "arranged how".
Six laundromats in a catchment mean something very different when four of them
line one corridor and the east side has none. That distinction is geometric, so
it is computed here rather than left for a model to eyeball from a list.

Deliberately dependency-free: haversine, DBSCAN, and point-in-polygon are each
short enough to own outright, and owning them keeps the scoring path free of a
numerical stack whose version drift would silently move results.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

from .indices import SUBSTITUTABILITY_WEIGHTS

EARTH_RADIUS_KM = 6371.0088

#: A cluster holding more than this share of all competitors is not telling you
#: about structure, it is telling you the radius is too coarse for the density.
CLUSTER_DOMINANCE_LIMIT = 0.7

#: Distance decay for the gap grid. Gap scoring works in kilometres rather than
#: drive time because scoring every grid cell against every competitor by road
#: would need a route matrix far past the 625-element cap. The clusters and the
#: Supply Index both use real drive time; only this heat grid approximates.
GAP_DECAY_KM = 1.6


@dataclass
class Cluster:
    cluster_id: int
    members: list[str]
    size: int
    total_weight: float
    centroid: dict
    mean_drive_time_minutes: float | None


@dataclass
class GapCell:
    lat: float
    lng: float
    underservice_score: float
    nearest_competitor_minutes: float | None = None


@dataclass
class GeographyResult:
    clusters: list[Cluster] = field(default_factory=list)
    unclustered: list[str] = field(default_factory=list)
    gap_cells: list[GapCell] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance between (lat, lng) pairs, in kilometres."""
    lat1, lng1 = math.radians(a[0]), math.radians(a[1])
    lat2, lng2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlng = lat2 - lat1, lng2 - lng1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


def point_in_polygon(point: tuple[float, float], ring: Sequence[Sequence[float]]) -> bool:
    """Ray-casting test. `point` is (lat, lng); `ring` is GeoJSON [lng, lat] pairs."""
    lat, lng = point
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat):
            x_at = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lng < x_at:
                inside = not inside
        j = i
    return inside


def dbscan(
    points: Sequence[tuple[float, float]],
    *,
    eps_km: float,
    min_samples: int,
) -> list[int]:
    """Density clustering over lat/lng points. Returns a label per point, -1 = noise.

    DBSCAN rather than k-means because the question is "is there a corridor",
    not "split these into k groups". Corridors are dense and arbitrarily shaped,
    and their number is exactly what we are trying to discover.
    """
    n = len(points)
    labels = [-1] * n
    visited = [False] * n
    cluster_id = 0

    def neighbors(i: int) -> list[int]:
        return [
            j for j in range(n) if j != i and haversine_km(points[i], points[j]) <= eps_km
        ]

    for i in range(n):
        if visited[i]:
            continue
        visited[i] = True
        near = neighbors(i)
        if len(near) + 1 < min_samples:
            continue  # may still be picked up later as a border point
        labels[i] = cluster_id
        queue = list(near)
        while queue:
            j = queue.pop()
            if not visited[j]:
                visited[j] = True
                near_j = neighbors(j)
                if len(near_j) + 1 >= min_samples:
                    queue.extend(k for k in near_j if k not in queue)
            if labels[j] == -1:
                labels[j] = cluster_id
        cluster_id += 1

    return labels


def find_clusters(
    competitors: Sequence[dict],
    *,
    eps_km: float = 0.8,
    min_samples: int = 3,
) -> tuple[list[Cluster], list[str]]:
    """Group competitors that sit close enough together to read as one corridor."""
    located = [c for c in competitors if c.get("location")]
    if len(located) < min_samples:
        return [], [c["place_id"] for c in located]

    points = [(c["location"]["lat"], c["location"]["lng"]) for c in located]
    labels = dbscan(points, eps_km=eps_km, min_samples=min_samples)

    clusters: list[Cluster] = []
    unclustered: list[str] = []
    for cid in sorted({l for l in labels if l >= 0}):
        idx = [i for i, l in enumerate(labels) if l == cid]
        members = [located[i] for i in idx]
        times = [m["drive_time_minutes"] for m in members if m.get("drive_time_minutes") is not None]
        clusters.append(
            Cluster(
                cluster_id=cid,
                members=[m["place_id"] for m in members],
                size=len(members),
                total_weight=sum(m.get("weight") or 0.0 for m in members),
                centroid={
                    "lat": sum(points[i][0] for i in idx) / len(idx),
                    "lng": sum(points[i][1] for i in idx) / len(idx),
                },
                mean_drive_time_minutes=(sum(times) / len(times)) if times else None,
            )
        )
    unclustered = [located[i]["place_id"] for i, l in enumerate(labels) if l < 0]
    return clusters, unclustered


def find_gaps(
    polygon_ring: Sequence[Sequence[float]],
    competitors: Sequence[dict],
    *,
    spacing_km: float = 0.5,
    top_n: int = 10,
) -> list[GapCell]:
    """Grid the catchment and rank cells by how poorly served they are.

        underservice = 1 - max over competitors of (substitutability x decay(distance))

    A cell scores high only when every competitor that could serve it is either
    far away or a weak substitute. Note this weights by substitutability but not
    by demand: a genuinely empty quarter of the catchment may be empty because
    nobody lives there. Cross-reading against the demographics is the synthesis
    stage's job, and the memo is required to do it before calling a gap an
    opportunity.
    """
    if not polygon_ring:
        return []

    lngs = [p[0] for p in polygon_ring]
    lats = [p[1] for p in polygon_ring]
    min_lat, max_lat, min_lng, max_lng = min(lats), max(lats), min(lngs), max(lngs)

    mid_lat = (min_lat + max_lat) / 2
    dlat = spacing_km / 110.574
    dlng = spacing_km / (111.320 * max(0.05, math.cos(math.radians(mid_lat))))

    located = [
        (
            (c["location"]["lat"], c["location"]["lng"]),
            SUBSTITUTABILITY_WEIGHTS.get(c.get("substitutability") or "none", 0.0),
            c.get("drive_time_minutes"),
        )
        for c in competitors
        if c.get("location")
    ]

    cells: list[GapCell] = []
    lat = min_lat
    while lat <= max_lat:
        lng = min_lng
        while lng <= max_lng:
            if point_in_polygon((lat, lng), polygon_ring):
                best = 0.0
                nearest_minutes = None
                nearest_km = float("inf")
                for (cl, s, mins) in located:
                    d = haversine_km((lat, lng), cl)
                    best = max(best, s * math.exp(-d / GAP_DECAY_KM))
                    if d < nearest_km:
                        nearest_km, nearest_minutes = d, mins
                cells.append(
                    GapCell(
                        lat=round(lat, 6),
                        lng=round(lng, 6),
                        underservice_score=round(1.0 - best, 4),
                        nearest_competitor_minutes=nearest_minutes,
                    )
                )
            lng += dlng
        lat += dlat

    cells.sort(key=lambda c: c.underservice_score, reverse=True)
    return cells[:top_n]


def analyze_geography(
    polygon_ring: Sequence[Sequence[float]],
    competitors: Sequence[dict],
    *,
    eps_km: float = 0.8,
    min_samples: int = 3,
    spacing_km: float = 0.5,
) -> GeographyResult:
    clusters, unclustered = find_clusters(
        competitors, eps_km=eps_km, min_samples=min_samples
    )
    gaps = find_gaps(polygon_ring, competitors, spacing_km=spacing_km)
    notes: list[str] = []
    located = [c for c in competitors if c.get("location")]
    if clusters:
        biggest = max(clusters, key=lambda c: c.size)
        notes.append(
            f"{len(clusters)} competitor cluster(s) detected at a {eps_km} km "
            f"radius; the largest holds {biggest.size} locations."
        )
        if located and biggest.size / len(located) > CLUSTER_DOMINANCE_LIMIT:
            notes.append(
                f"That cluster holds {biggest.size} of {len(located)} competitors, "
                f"which means the {eps_km} km radius is too coarse to separate "
                f"anything in a market this dense. Read it as 'the whole catchment "
                f"is one corridor', not as a distinct cluster, and re-run with a "
                f"smaller clustering.eps_km in the vertical config to see structure."
            )
    if gaps:
        notes.append(
            f"Gap grid scored at {spacing_km} km spacing using straight-line "
            f"distance, not drive time; a gap cell separated from competitors by "
            f"a barrier is under-scored here and should be checked against the "
            f"detour ratios of nearby competitors."
        )
    return GeographyResult(
        clusters=clusters, unclustered=unclustered, gap_cells=gaps, notes=notes
    )
