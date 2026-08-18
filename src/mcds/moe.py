"""Margin-of-error arithmetic, following the Census Bureau's published formulas.

ACS estimates at block-group level carry margins of error that are routinely
+/-30% of the estimate itself. A catchment analysis that reports a point estimate
and drops the interval is not a more precise analysis, it is a less honest one.
Everything downstream of the ACS pull therefore moves intervals, not scalars.

Confidence level throughout is 90%, which is what ACS publishes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Estimate:
    """A value with an optional margin of error.

    `moe` is at the 90% confidence level. `lo`/`hi` are derived, not stored, so
    they can never drift out of sync with `value` and `moe`.
    """

    value: float
    moe: float | None = None

    @property
    def lo(self) -> float | None:
        if self.moe is None:
            return None
        # ACS counts cannot be negative; a lower bound below zero means the
        # estimate is not distinguishable from zero, which is worth showing as 0.
        return max(0.0, self.value - self.moe)

    @property
    def hi(self) -> float | None:
        return None if self.moe is None else self.value + self.moe

    @property
    def relative_moe(self) -> float | None:
        """MOE as a fraction of the estimate. Above ~0.30 the estimate is weak."""
        if self.moe is None or self.value == 0:
            return None
        return self.moe / abs(self.value)

    def scaled(self, factor: float) -> "Estimate":
        """Multiply by an exact constant (e.g. an interpolation fraction).

        Treating the factor as exact understates true uncertainty, because an
        interpolation fraction is itself estimated. Documented rather than
        silently absorbed; see interpolate.py.
        """
        return Estimate(
            value=self.value * factor,
            moe=None if self.moe is None else abs(self.moe * factor),
        )

    def as_dict(self) -> dict:
        return {"value": self.value, "lo": self.lo, "hi": self.hi, "moe": self.moe}


def sum_estimates(estimates: list[Estimate]) -> Estimate:
    """Aggregate estimates. MOE of a sum is the root sum of squares.

    Census formula: MOE_total = sqrt(sum(MOE_i^2)). If any component MOE is
    missing the aggregate MOE is unknowable, so it propagates as None rather
    than pretending the missing component contributed nothing.
    """
    total = sum(e.value for e in estimates)
    moes = [e.moe for e in estimates]
    if any(m is None for m in moes):
        return Estimate(total, None)
    return Estimate(total, math.sqrt(sum(m * m for m in moes)))  # type: ignore[misc]


def proportion_moe(
    numerator: Estimate, denominator: Estimate
) -> float | None:
    """MOE of numerator/denominator where the numerator is a SUBSET of the denominator.

    Census proportion formula:
        MOE_p = (1/Y) * sqrt(MOE_X^2 - p^2 * MOE_Y^2)

    When the radicand goes negative the Bureau directs you to fall back to the
    ratio formula, which adds instead of subtracts. Both branches are
    implemented because the negative case is common at block-group level.
    """
    if numerator.moe is None or denominator.moe is None or denominator.value == 0:
        return None
    p = numerator.value / denominator.value
    radicand = numerator.moe**2 - (p**2) * (denominator.moe**2)
    if radicand < 0:
        radicand = numerator.moe**2 + (p**2) * (denominator.moe**2)
    return math.sqrt(radicand) / denominator.value


def product_moe(a: Estimate, b: Estimate) -> float | None:
    """MOE of a product of two independent estimates.

    MOE_ab = sqrt(a^2 * MOE_b^2 + b^2 * MOE_a^2)

    Independence is assumed. Where it does not hold (income x age, for instance)
    the caller applies an explicit independence penalty; see indices.py.
    """
    if a.moe is None or b.moe is None:
        return None
    return math.sqrt((a.value**2) * (b.moe**2) + (b.value**2) * (a.moe**2))


def widen(estimate: Estimate, factor: float) -> Estimate:
    """Inflate an MOE by a judgment-based factor, leaving the point estimate alone.

    Used for the independence penalty. This is a heuristic, not a derived
    statistic, and every place it is applied sets a flag on the scorecard so the
    memo can say so.
    """
    if estimate.moe is None:
        return estimate
    return Estimate(estimate.value, estimate.moe * factor)
