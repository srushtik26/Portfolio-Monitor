from decimal import Decimal
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class CompanyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    symbol: str
    exchange: Literal["NSE", "BSE"]
    sector: str | None
    cmp: float | None
    base_target: float | None
    bull_target: float | None
    version: int
    latest_quote: "PriceQuoteRead | None" = None


class PriceQuoteRead(BaseModel):
    id: int
    exchange: Literal["NSE", "BSE"]
    symbol: str
    price: float
    currency: str
    provider: str
    quote_type: Literal["last_trade", "candle_close", "fixture", "provider_quote"]
    quoted_at: datetime
    received_at: datetime
    is_simulated: bool
    freshness_seconds: int
    status: Literal["fresh", "stale", "out_of_order", "simulated"]
    is_fresh: bool


class PriceHistoryRead(BaseModel):
    exchange: Literal["NSE", "BSE"]
    symbol: str
    items: list[PriceQuoteRead]


class TargetUpdate(BaseModel):
    expected_version: int
    base_target: Decimal | None = None
    bull_target: Decimal | None = None


class ImportConfirm(BaseModel):
    review_cached_targets: bool = False
