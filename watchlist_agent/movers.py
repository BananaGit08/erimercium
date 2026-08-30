"""Deciding which price moves are worth reporting.

The rule is per-ticker: a move is flagged when it is large relative to how much
that particular stock normally moves, not when it clears one global percentage.
Three guards keep that honest at the edges:

  * a z-score bar, so the move must be unusual for this name;
  * an absolute floor, so a statistically odd but trivial move in a very steady
    stock does not qualify;
  * an absolute ceiling, so a genuinely huge move is always reported even in a
    name volatile enough to make it unremarkable statistically.

Tickers with no volatility history fall back to the flat threshold.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Thresholds
from .prices import Quote


@dataclass
class Mover:
    quote: Quote
    sigma: float | None
    reason: str

    @property
    def ticker(self) -> str:
        return self.quote.ticker

    @property
    def change_pct(self) -> float:
        return self.quote.change_pct

    @property
    def z_score(self) -> float | None:
        if not self.sigma:
            return None
        return abs(self.change_pct) / self.sigma

    @property
    def rank_key(self) -> float:
        """Sort by statistical unusualness where known, magnitude otherwise.

        A ticker with no history is ranked by how far it cleared the flat
        threshold, which puts it on roughly the same scale as a z-score.
        """
        z = self.z_score
        if z is not None:
            return z
        return abs(self.change_pct) / 3.0


def select_movers(
    quotes: list[Quote], sigmas: dict[str, float], thresholds: Thresholds
) -> list[Mover]:
    """Flagged movers, most unusual first."""
    movers: list[Mover] = []

    for quote in quotes:
        pct = abs(quote.change_pct)
        sigma = sigmas.get(quote.ticker)

        if pct >= thresholds.always_flag_abs_pct:
            movers.append(
                Mover(quote, sigma, f"{pct:.1f}% move regardless of typical range")
            )
            continue

        if sigma is None:
            if pct > thresholds.fallback_pct:
                movers.append(
                    Mover(quote, None, f"above the {thresholds.fallback_pct:g}% fallback bar (no history)")
                )
            continue

        z = pct / sigma
        if z >= thresholds.z_score and pct >= thresholds.min_abs_pct:
            movers.append(
                Mover(quote, sigma, f"{z:.1f}x its typical daily move (±{sigma:.1f}%)")
            )

    return sorted(movers, key=lambda m: m.rank_key, reverse=True)


def split_for_email(
    movers: list[Mover], max_shown: int
) -> tuple[list[Mover], list[Mover]]:
    """Split into the movers to detail and the remainder to summarise."""
    if max_shown <= 0 or len(movers) <= max_shown:
        return movers, []
    return movers[:max_shown], movers[max_shown:]
