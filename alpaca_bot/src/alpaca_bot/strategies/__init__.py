from .base import CandidateSignal, Strategy, StrategyContext, TradePlan, ValidationResult
from .mean_reversion import MeanReversionStrategy
from .news_momentum import NewsMomentumStrategy
from .opening_range_breakout import OpeningRangeBreakoutStrategy
from .relative_strength import RelativeStrengthStrategy
from .vwap_pullback import VwapPullbackStrategy

ALL_STRATEGIES: list[Strategy] = [
    OpeningRangeBreakoutStrategy(),
    VwapPullbackStrategy(),
    MeanReversionStrategy(),
    NewsMomentumStrategy(),
    RelativeStrengthStrategy(),
]

__all__ = [
    "ALL_STRATEGIES",
    "CandidateSignal",
    "MeanReversionStrategy",
    "NewsMomentumStrategy",
    "OpeningRangeBreakoutStrategy",
    "RelativeStrengthStrategy",
    "Strategy",
    "StrategyContext",
    "TradePlan",
    "ValidationResult",
    "VwapPullbackStrategy",
]
