# Hand-written stub for protos/aaii_investor_sentiment.proto.
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AaiiInvestorSentimentRecord:
    """Schema stub matching AaiiInvestorSentimentRecord in aaii_investor_sentiment.proto."""

    date: str = ""
    bullish_pct: float = 0.0
    neutral_pct: float = 0.0
    bearish_pct: float = 0.0
    bull_bear_spread: float = 0.0
    bullish_average: float = 0.0
    bearish_average: float = 0.0
    source_url: str = ""
    fetch_time: str = ""
