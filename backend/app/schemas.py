from decimal import Decimal
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


TargetDirection = Literal["below", "above", "pending"]
TriggerMode = Literal["at_or_beyond", "crosses"]
RearmPolicy = Literal["once_per_revision", "rearm_on_reset", "rearm_after_recovery", "cooldown"]
AlertStateStatus = Literal["armed", "triggered", "acknowledged", "resolved", "pending_configuration"]


class PriceQuoteRead(BaseModel):
    id: int
    exchange: Literal["NSE", "BSE"]
    symbol: str
    price: float
    currency: str
    provider: str
    quote_type: Literal["last_trade", "candle_close", "fixture", "provider_quote", "websocket_tick"]
    quoted_at: datetime
    received_at: datetime
    is_simulated: bool
    freshness_seconds: int
    status: Literal["fresh", "stale", "out_of_order", "simulated"]
    is_fresh: bool


class TargetAlertStateRead(BaseModel):
    target_kind: Literal["base", "bull"]
    state: AlertStateStatus
    comparison_direction: TargetDirection
    trigger_mode: TriggerMode
    rearm_policy: RearmPolicy
    last_evaluated_price: float | None = None
    last_evaluated_at: datetime | None = None
    last_condition_met: bool = False
    last_triggered_at: datetime | None = None
    last_triggered_price: float | None = None
    last_alert_id: int | None = None
    trigger_cycle: int = 0
    acknowledged_at: datetime | None = None
    resolved_at: datetime | None = None
    reset_at: datetime | None = None


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
    latest_quote: PriceQuoteRead | None = None

    is_approved: bool = True
    monitoring_active: bool = True
    base_direction: TargetDirection = "above"
    bull_direction: TargetDirection = "above"
    base_trigger_mode: TriggerMode = "at_or_beyond"
    bull_trigger_mode: TriggerMode = "at_or_beyond"
    rearm_policy: RearmPolicy = "once_per_revision"
    cooldown_minutes: int = 60
    source_metadata: dict[str, Any] | None = None

    distance_to_base_abs: float | None = None
    distance_to_base_pct: float | None = None
    distance_to_bull_abs: float | None = None
    distance_to_bull_pct: float | None = None
    nearest_target_pct: float | None = None
    alert_status: str = "monitoring"
    base_alert_state: TargetAlertStateRead | None = None
    bull_alert_state: TargetAlertStateRead | None = None


class PriceHistoryRead(BaseModel):
    exchange: Literal["NSE", "BSE"]
    symbol: str
    items: list[PriceQuoteRead]


class TargetUpdate(BaseModel):
    expected_version: int
    base_target: Decimal | None = None
    bull_target: Decimal | None = None
    base_direction: TargetDirection | None = None
    bull_direction: TargetDirection | None = None
    base_trigger_mode: TriggerMode | None = None
    bull_trigger_mode: TriggerMode | None = None
    rearm_policy: RearmPolicy | None = None
    cooldown_minutes: int | None = Field(default=None, ge=1, le=10080)
    monitoring_active: bool | None = None
    is_approved: bool | None = None
    actor: str = "Srushti Kadam"


class TargetConfigUpdate(BaseModel):
    base_direction: TargetDirection | None = None
    bull_direction: TargetDirection | None = None
    base_trigger_mode: TriggerMode | None = None
    bull_trigger_mode: TriggerMode | None = None
    rearm_policy: RearmPolicy | None = None
    cooldown_minutes: int | None = Field(default=None, ge=1, le=10080)
    monitoring_active: bool | None = None
    is_approved: bool | None = None
    actor: str = "Srushti Kadam"


class AlertActionRequest(BaseModel):
    action: Literal["acknowledge", "resolve", "reset"]
    actor: str = "Srushti Kadam"


class ImportRowUpdate(BaseModel):
    exchange: str | None = None
    symbol: str | None = None
    name: str | None = None
    sector: str | None = None
    cmp: Decimal | None = None
    base_target: Decimal | None = None
    bull_target: Decimal | None = None
    base_direction: TargetDirection | None = None
    bull_direction: TargetDirection | None = None
    approval_status: Literal["approved", "rejected", "pending"] | None = None


class ImportConfirm(BaseModel):
    review_cached_targets: bool = False
    uploaded_by: str = "Srushti Kadam"
    approved_by: str = "Srushti Kadam"
    default_base_direction: TargetDirection | None = None
    default_bull_direction: TargetDirection | None = None


class NotificationPreferenceUpdate(BaseModel):
    dashboard_enabled: bool | None = None
    email_enabled: bool | None = None
    email_recipients: list[str] | str | None = None
    notify_on_base: bool | None = None
    notify_on_bull: bool | None = None
    notify_on_stale: bool | None = None
    actor: str = "Srushti Kadam"
