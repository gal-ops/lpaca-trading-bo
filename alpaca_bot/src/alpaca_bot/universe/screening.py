"""Universe screening pipeline (spec section 6): the staged process that
takes `tradable_universe` down to `eligible_now` -- the third universe
layer, built from live bars/quotes, not just static asset metadata. These
are trade-eligibility filters for a given cycle, never permanent discovery
exclusions (spec section 2): an asset that fails here keeps its
AssetRecord and simply gets an exclusion_reason recorded for that cycle.

Ranking here covers what's computable from cheap daily-bar features alone
(relative volume, liquidity/notional). Catalyst quality and live
spread/execution quality need real-time quotes and the news feed, which
are wired in by the strategy modules (phase 6) and regime engines
(phase 5) -- this stage's ranking is the cheap prescreen the spec
describes in section 6 steps 3-5, not the final strategy-specific score.
"""

from __future__ import annotations

from dataclasses import dataclass

from alpaca_bot.data.bars import Bar
from alpaca_bot.features.liquidity import atr_pct, average_volume, median_daily_dollar_volume
from alpaca_bot.universe.discovery import AssetRecord


@dataclass
class EligibilityResult:
    symbol: str
    eligible: bool
    reason: str | None
    features: dict[str, float]


def screen_equity_universe(
    assets: list[AssetRecord],
    bars_by_symbol: dict[str, list[Bar]],
    cfg: dict,
) -> list[EligibilityResult]:
    """Spec section 6's initial equity eligibility guidelines: price,
    median/average dollar volume, average share volume, ATR% band. Assets
    with no bar data at all are excluded with a clear reason rather than
    silently skipped."""
    eq_cfg = cfg["universe"]["equity"]
    results = []
    for asset in assets:
        bars = bars_by_symbol.get(asset.symbol)
        if not bars:
            results.append(EligibilityResult(asset.symbol, False, "no bar data available", {}))
            continue

        last_price = bars[-1].close
        med_dollar_vol = median_daily_dollar_volume(bars)
        avg_vol = average_volume(bars)
        atr_pct_value = atr_pct(bars)
        features = {
            "last_price": last_price, "median_dollar_volume": med_dollar_vol,
            "average_volume": avg_vol, "atr_pct": atr_pct_value,
        }

        if last_price < eq_cfg["min_price"]:
            results.append(EligibilityResult(asset.symbol, False,
                            f"price ${last_price:.2f} below minimum ${eq_cfg['min_price']}", features))
            continue
        if med_dollar_vol < eq_cfg["min_median_dollar_volume"]:
            results.append(EligibilityResult(asset.symbol, False,
                            f"median dollar volume ${med_dollar_vol:,.0f} below minimum "
                            f"${eq_cfg['min_median_dollar_volume']:,.0f}", features))
            continue
        if avg_vol < eq_cfg["min_avg_volume_shares"]:
            results.append(EligibilityResult(asset.symbol, False,
                            f"average volume {avg_vol:,.0f} shares below minimum "
                            f"{eq_cfg['min_avg_volume_shares']:,.0f}", features))
            continue
        if not (eq_cfg["min_atr_pct"] <= atr_pct_value <= eq_cfg["max_atr_pct"]):
            results.append(EligibilityResult(asset.symbol, False,
                            f"ATR% {atr_pct_value*100:.2f}% outside "
                            f"[{eq_cfg['min_atr_pct']*100:.1f}%, {eq_cfg['max_atr_pct']*100:.1f}%]",
                            features))
            continue

        results.append(EligibilityResult(asset.symbol, True, None, features))
    return results


def screen_crypto_universe(
    assets: list[AssetRecord],
    bars_by_symbol: dict[str, list[Bar]],
    min_median_dollar_volume: float = 1_000_000,
) -> list[EligibilityResult]:
    """Crypto guidelines are looser per spec section 6 (no price floor,
    no ATR band -- crypto is naturally more volatile); the binding
    constraint at this cheap-feature stage is having sufficient rolling
    volume at all. Spread/displayed-liquidity checks need live quotes and
    belong to the pre-trade validator (phase 9), not this bars-only pass."""
    results = []
    for asset in assets:
        bars = bars_by_symbol.get(asset.symbol)
        if not bars:
            results.append(EligibilityResult(asset.symbol, False, "no bar data available", {}))
            continue
        med_dollar_vol = median_daily_dollar_volume(bars)
        features = {"median_dollar_volume": med_dollar_vol, "last_price": bars[-1].close}
        if med_dollar_vol < min_median_dollar_volume:
            results.append(EligibilityResult(asset.symbol, False,
                            f"median dollar volume ${med_dollar_vol:,.0f} below minimum "
                            f"${min_median_dollar_volume:,.0f}", features))
            continue
        results.append(EligibilityResult(asset.symbol, True, None, features))
    return results


def rank_candidates(eligible: list[EligibilityResult], top_n: int | None = None) -> list[EligibilityResult]:
    """Cheap prescreen ranking (spec section 6 step 5): relative-volume
    percentile plus liquidity/notional, the two components computable
    from bars alone. Returns only the eligible=True results, ranked
    descending, optionally truncated to top_n before the expensive
    per-symbol work (streaming subscriptions, strategy signals, models)
    that follows in later phases."""
    passing = [r for r in eligible if r.eligible]
    if not passing:
        return []

    volumes = sorted(r.features.get("average_volume", r.features.get("median_dollar_volume", 0))
                      for r in passing)

    def percentile_rank(value: float) -> float:
        if len(volumes) <= 1:
            return 1.0
        below = sum(1 for v in volumes if v <= value)
        return below / len(volumes)

    def score(r: EligibilityResult) -> float:
        vol_metric = r.features.get("average_volume", r.features.get("median_dollar_volume", 0))
        rel_volume_pct = percentile_rank(vol_metric)
        liquidity_pct = percentile_rank(r.features.get("median_dollar_volume", 0))
        # Catalyst/spread components are 0 here -- not computable from bars
        # alone; see module docstring. Weights re-normalized over the two
        # components this stage can actually produce.
        return 0.55 * rel_volume_pct + 0.45 * liquidity_pct

    ranked = sorted(passing, key=score, reverse=True)
    return ranked[:top_n] if top_n else ranked
