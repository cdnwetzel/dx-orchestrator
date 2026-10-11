"""Small, dependency-free Bayesian helpers.

Every rate the learner reasons about (first-pass yield, gate pass rate,
signature hit rate) is a Beta-Binomial posterior. Observations can carry a
weight in (0, 1] so old evidence fades with an exponential half-life instead
of being cut off at a hard window. Monte Carlo draws use a fixed seed, so the
same inputs always produce the same recommendations.
"""
from __future__ import annotations

import math
import random
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

PRIOR = (1.0, 1.0)  # uniform


def decay_weight(observed_at: datetime, now: datetime, half_life_days: float) -> float:
    if half_life_days <= 0:
        raise ValueError("half_life_days must be > 0")
    age_days = max((now - observed_at).total_seconds() / 86400.0, 0.0)
    return float(0.5 ** (age_days / half_life_days))


@dataclass(frozen=True)
class BetaPost:
    alpha: float
    beta: float

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def n_eff(self) -> float:
        """Effective (weighted) observation count, prior excluded."""
        return self.alpha + self.beta - sum(PRIOR)

    def interval(self, mass: float = 0.9, draws: int = 20000, seed: int = 0) -> tuple[float, float]:
        rng = random.Random(seed)
        xs = sorted(rng.betavariate(self.alpha, self.beta) for _ in range(draws))
        lo = xs[int((1 - mass) / 2 * draws)]
        hi = xs[min(int((1 + mass) / 2 * draws), draws - 1)]
        return lo, hi


def beta_from(observations: Iterable[tuple[bool, float]], prior: tuple[float, float] = PRIOR) -> BetaPost:
    a, b = prior
    for success, weight in observations:
        if not (0.0 <= weight <= 1.0) or math.isnan(weight):
            raise ValueError(f"weight out of range: {weight}")
        if success:
            a += weight
        else:
            b += weight
    return BetaPost(a, b)


def prob_greater(x: BetaPost, y: BetaPost, draws: int = 20000, seed: int = 0) -> float:
    """P(rate_x > rate_y) under independent Beta posteriors."""
    rng = random.Random(seed)
    wins = sum(rng.betavariate(x.alpha, x.beta) > rng.betavariate(y.alpha, y.beta)
               for _ in range(draws))
    return wins / draws


def prob_diff_below(x: BetaPost, y: BetaPost, delta: float, draws: int = 20000,
                    seed: int = 0) -> float:
    """P(rate_x - rate_y < delta) under independent Beta posteriors."""
    rng = random.Random(seed)
    hits = sum(rng.betavariate(x.alpha, x.beta) - rng.betavariate(y.alpha, y.beta) < delta
               for _ in range(draws))
    return hits / draws


def total_variation(a: dict[str, int], b: dict[str, int]) -> float:
    """Total-variation distance between two count distributions (0 = same mix, 1 = disjoint)."""
    na, nb = sum(a.values()), sum(b.values())
    if not na or not nb:
        return 0.0
    return 0.5 * sum(abs(a.get(k, 0) / na - b.get(k, 0) / nb) for k in set(a) | set(b))
