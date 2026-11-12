"""Shoonya (Finvasia) NorenRestApiPy live WebSocket & REST price-monitoring service.

STRICT SAFETY BOUNDARY:
This module is strictly a read-only market-data and target-alert feed.
It never exposes or executes buy/sell orders.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import struct
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import SessionLocal
from .models import Company, WorkerHeartbeat
from .notifications import MultiChannelNotifier, drain_outbox
from .prices import (
    ExchangeCalendar,
    NseBseExchangeCalendar,
    ProviderError,
    ProviderQuote,
    RateLimitError,
    normalize_utc,
    record_quote,
    reset_symbol_cross_baselines,
)

logger = logging.getLogger("portfolio.shoonya_feed")

UNVERIFIED_AUTH_ERROR_MESSAGE = "Shoonya auth flow unverified against current official docs"

# UNVERIFIED: Default Shoonya REST and WebSocket endpoints from legacy NorenRestApiPy examples.
# Must be verified against current official Finvasia Shoonya API documentation before production use.
DEFAULT_SHOONYA_HOST = "https://api.shoonya.com/NorenWClientTP/"
DEFAULT_SHOONYA_WS = "wss://api.shoonya.com/NorenWSTP/"

# UNVERIFIED: Static NSE/BSE scrip token mapping for simulated/offline tests only.
# Must be replaced from the real login / official scrip-master response after the auth flow is confirmed.
DEFAULT_SHOONYA_TOKENS: dict[tuple[str, str], str] = {
    ("NSE", "BHEL"): "438",
    ("NSE", "TEGA"): "7529",
    ("NSE", "RELIANCE"): "2885",
    ("NSE", "HDFCBANK"): "1333",
    ("NSE", "TCS"): "11536",
    ("NSE", "INFY"): "1594",
    ("NSE", "ICICIBANK"): "4963",
    ("NSE", "LT"): "11483",
    ("NSE", "BHARTIARTL"): "10604",
    ("NSE", "SUNPHARMA"): "3351",
    ("BSE", "AXISBANK"): "532215",
    ("NSE", "AXISBANK"): "5900",
    ("NSE", "TITAN"): "3506",
    ("NSE", "MARUTI"): "10999",
    ("BSE", "BAJFINANCE"): "500034",
    ("NSE", "BAJFINANCE"): "317",
    ("NSE", "NTPC"): "11630",
    ("NSE", "POWERGRID"): "14977",
    ("NSE", "ONGC"): "2475",
    ("NSE", "COALINDIA"): "20374",
    ("NSE", "SBIN"): "3045",
    ("NSE", "KOTAKBANK"): "1922",
    ("NSE", "ITC"): "1660",
    ("NSE", "HINDUNILVR"): "1394",
    ("NSE", "ULTRACEMCO"): "11532",
    ("NSE", "TATASTEEL"): "3499",
    ("NSE", "JSWSTEEL"): "11723",
    ("NSE", "HINDALCO"): "1363",
    ("NSE", "GRASIM"): "1232",
    ("NSE", "ADANIPORTS"): "15083",
    ("NSE", "M&M"): "2031",
    ("NSE", "TATAMOTORS"): "3456",
    ("NSE", "BAJAJ-AUTO"): "16669",
    ("NSE", "EICHERMOT"): "910",
    ("NSE", "HEROMOTOCO"): "1348",
    ("NSE", "CIPLA"): "694",
    ("NSE", "DRREDDY"): "881",
    ("NSE", "APOLLOHOSP"): "157",
    ("NSE", "WIPRO"): "3787",
    ("NSE", "HCLTECH"): "7229",
    ("NSE", "TECHM"): "13538",
    ("NSE", "HAL"): "2303",
}


class ShoonyaAuthError(ProviderError):
    """Raised when Shoonya authentication or 2FA TOTP generation fails."""


class ShoonyaSubscriptionError(ProviderError):
    """Raised when instrument token lookup or WebSocket subscription fails."""


class NorenSdkProtocol(Protocol):
    """Subset of NorenRestApiPy.NorenApi used strictly for market data."""

    def login(
        self,
        userid: str,
        password: str,
        twoFA: str,
        vendor_code: str,
        api_secret: str,
        imei: str,
    ) -> dict[str, Any] | None: ...

    def searchscrip(self, exchange: str, searchtext: str) -> dict[str, Any] | None: ...

    def get_quotes(self, exchange: str, token: str) -> dict[str, Any] | None: ...

    def start_websocket(
        self,
        subscribe_callback: Callable[[dict[str, Any]], None] | None = None,
        order_update_callback: Callable[[dict[str, Any]], None] | None = None,
        socket_open_callback: Callable[[], None] | None = None,
        socket_close_callback: Callable[[], None] | None = None,
        socket_error_callback: Callable[[Any], None] | None = None,
    ) -> Any: ...

    def subscribe(self, instrument: str | list[str], feed_type: Any = None) -> Any: ...

    def unsubscribe(self, instrument: str | list[str], feed_type: Any = None) -> Any: ...

    def close_websocket(self) -> Any: ...


def generate_totp(secret: str, *, for_time: int | None = None, interval: int = 30) -> str:
    """Generate a 6-digit RFC 6238 TOTP code from a Base32 secret.

    Uses pyotp if installed, otherwise uses standard-library HMAC-SHA1 so 2FA
    works reliably even without extra C/third-party dependencies.
    """
    cleaned = secret.strip().replace(" ", "").upper()
    if not cleaned:
        raise ShoonyaAuthError("TOTP secret must not be empty.")
    try:
        import pyotp  # type: ignore[import-not-found]

        if for_time is None:
            return str(pyotp.TOTP(cleaned).now())
        return str(pyotp.TOTP(cleaned).at(for_time))
    except ImportError:
        pass

    padding = (8 - len(cleaned) % 8) % 8
    try:
        key = base64.b32decode(cleaned + ("=" * padding), casefold=True)
    except Exception as exc:
        raise ShoonyaAuthError("Invalid Base32 SHOONYA_TOTP_SECRET.") from exc

    timestamp = int(time.time()) if for_time is None else int(for_time)
    counter = timestamp // interval
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


@dataclass(frozen=True)
class ShoonyaConfig:
    """Configuration and credential mapping for Finvasia Shoonya API.

    NOTE (UNVERIFIED AUTH FLOW):
    The direct `NorenRestApiPy` login flow (`userid` + `password` + `twoFA` + `vendor_code`
    + `api_secret` + `imei`) and the `NorenWClientTP` / `NorenWSTP` endpoints are UNVERIFIED
    against current official Shoonya documentation.
    The Shoonya provider is shipped disabled by default (`allow_unverified_auth=False`)
    and fails fast unless explicitly opted in.
    """

    user_id: str
    password: str
    vendor_code: str
    api_secret: str
    totp_secret: str | None = None
    factor2_override: str | None = None
    imei: str = "abc1234"
    host: str = DEFAULT_SHOONYA_HOST
    ws_endpoint: str = DEFAULT_SHOONYA_WS
    quote_freshness_seconds: int = 300
    reconnect_base_seconds: float = 2.0
    reconnect_max_seconds: float = 60.0
    max_reconnect_attempts: int = 0
    subscription_batch_size: int = 50
    tick_queue_maxsize: int = 256
    allow_unverified_auth: bool = False
    custom_token_map: dict[tuple[str, str], str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> ShoonyaConfig:
        """Load Shoonya credentials and connection settings from environment variables."""
        allow_unverified = (
            os.environ.get("SHOONYA_ALLOW_UNVERIFIED_AUTH", "false").strip().lower()
            in {"1", "true", "yes"}
        )
        if not allow_unverified:
            raise ShoonyaAuthError(UNVERIFIED_AUTH_ERROR_MESSAGE)

        user_id = (
            os.environ.get("SHOONYA_USER_ID")
            or os.environ.get("SHOONYA_UID")
            or ""
        ).strip()
        password = (
            os.environ.get("SHOONYA_PASSWORD")
            or os.environ.get("SHOONYA_PWD")
            or ""
        ).strip()
        vendor_code = (
            os.environ.get("SHOONYA_VENDOR_CODE")
            or os.environ.get("SHOONYA_VC")
            or (f"{user_id}_U" if user_id else "")
        ).strip()
        api_secret = (
            os.environ.get("SHOONYA_API_SECRET")
            or os.environ.get("SHOONYA_APP_KEY")
            or os.environ.get("SHOONYA_API_KEY")
            or ""
        ).strip()
        totp_secret = (
            os.environ.get("SHOONYA_TOTP_SECRET")
            or os.environ.get("SHOONYA_TOTP_KEY")
            or ""
        ).strip() or None
        factor2_override = (
            os.environ.get("SHOONYA_FACTOR2")
            or os.environ.get("SHOONYA_TWO_FA")
            or ""
        ).strip() or None
        imei = (os.environ.get("SHOONYA_IMEI") or "abc1234").strip()
        host = (os.environ.get("SHOONYA_HOST") or DEFAULT_SHOONYA_HOST).strip()
        ws_endpoint = (os.environ.get("SHOONYA_WS_ENDPOINT") or DEFAULT_SHOONYA_WS).strip()

        freshness = int(os.environ.get("SHOONYA_QUOTE_FRESHNESS_SECONDS", "300"))
        reconnect_base = float(os.environ.get("SHOONYA_RECONNECT_BASE_SECONDS", "2.0"))
        reconnect_max = float(os.environ.get("SHOONYA_RECONNECT_MAX_SECONDS", "60.0"))
        max_attempts = int(os.environ.get("SHOONYA_MAX_RECONNECT_ATTEMPTS", "0"))
        queue_maxsize = int(os.environ.get("SHOONYA_TICK_QUEUE_MAXSIZE", "256"))

        custom_tokens: dict[tuple[str, str], str] = {}
        raw_map = (os.environ.get("SHOONYA_TOKEN_MAP_JSON") or "").strip()
        if raw_map:
            try:
                parsed = json.loads(raw_map)
                if isinstance(parsed, dict):
                    for key, token_val in parsed.items():
                        if ":" in str(key):
                            exch, sym = str(key).split(":", 1)
                            custom_tokens[(exch.strip().upper(), sym.strip().upper())] = str(token_val).strip()
            except json.JSONDecodeError as exc:
                raise ValueError("SHOONYA_TOKEN_MAP_JSON must be valid JSON.") from exc

        missing: list[str] = []
        if not user_id:
            missing.append("SHOONYA_USER_ID")
        if not password:
            missing.append("SHOONYA_PASSWORD")
        if not vendor_code:
            missing.append("SHOONYA_VENDOR_CODE")
        if not api_secret:
            missing.append("SHOONYA_API_SECRET")
        if not totp_secret and not factor2_override:
            missing.append("SHOONYA_TOTP_SECRET (or SHOONYA_FACTOR2)")
        if missing:
            raise ShoonyaAuthError(
                f"Missing required Shoonya environment variables: {', '.join(missing)}"
            )

        return cls(
            user_id=user_id,
            password=password,
            vendor_code=vendor_code,
            api_secret=api_secret,
            totp_secret=totp_secret,
            factor2_override=factor2_override,
            imei=imei,
            host=host,
            ws_endpoint=ws_endpoint,
            quote_freshness_seconds=max(60, min(86400, freshness)),
            reconnect_base_seconds=max(0.5, reconnect_base),
            reconnect_max_seconds=max(reconnect_base, reconnect_max),
            max_reconnect_attempts=max(0, max_attempts),
            tick_queue_maxsize=max(1, queue_maxsize),
            allow_unverified_auth=allow_unverified,
            custom_token_map=custom_tokens,
        )

    def current_two_fa(self) -> str:
        """Return the current 2FA OTP token (generated from TOTP secret or static factor2)."""
        if self.totp_secret:
            return generate_totp(self.totp_secret)
        if self.factor2_override:
            return self.factor2_override
        raise ShoonyaAuthError("Neither SHOONYA_TOTP_SECRET nor SHOONYA_FACTOR2 is configured.")


def _create_noren_sdk_client(host: str, ws_endpoint: str) -> NorenSdkProtocol:
    """Instantiate the NorenRestApiPy NorenApi client with order execution disabled."""
    try:
        from NorenRestApiPy.NorenApi import NorenApi  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ProviderError(
            "NorenRestApiPy is not installed. Install it via `pip install NorenRestApiPy pyotp websocket-client`."
        ) from exc

    class ReadOnlyShoonyaApi(NorenApi):  # type: ignore[misc]
        def __init__(self) -> None:
            super().__init__(host=host, websocket=ws_endpoint)

        def place_order(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("Order execution is strictly prohibited in this price-monitoring system.")

        def modify_order(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("Order modification is strictly prohibited in this price-monitoring system.")

        def cancel_order(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("Order cancellation is strictly prohibited in this price-monitoring system.")

    return ReadOnlyShoonyaApi()


class ShoonyaTokenResolver:
    """Resolves (exchange, symbol) identities to Shoonya numeric instrument tokens and back."""

    def __init__(
        self,
        *,
        custom_tokens: dict[tuple[str, str], str] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._symbol_to_token: dict[tuple[str, str], str] = dict(DEFAULT_SHOONYA_TOKENS)
        if custom_tokens:
            for (exch, sym), tok in custom_tokens.items():
                self._symbol_to_token[(exch.upper(), sym.upper())] = str(tok).strip()
        self._token_to_symbol: dict[tuple[str, str], str] = {
            (exch, tok): sym for (exch, sym), tok in self._symbol_to_token.items()
        }

    def register(self, exchange: str, symbol: str, token: str) -> None:
        norm_exch = exchange.strip().upper()
        norm_sym = symbol.strip().upper()
        norm_tok = str(token).strip()
        if not norm_exch or not norm_sym or not norm_tok:
            raise ValueError("Exchange, symbol, and token must be non-empty.")
        with self._lock:
            self._symbol_to_token[(norm_exch, norm_sym)] = norm_tok
            self._token_to_symbol[(norm_exch, norm_tok)] = norm_sym

    def resolve_token(
        self,
        exchange: str,
        symbol: str,
        sdk: NorenSdkProtocol | None = None,
    ) -> str:
        norm_exch = exchange.strip().upper()
        norm_sym = symbol.strip().upper()
        if norm_exch not in {"NSE", "BSE"}:
            raise ShoonyaSubscriptionError(f"Unsupported exchange for Shoonya: {norm_exch}")
        with self._lock:
            cached = self._symbol_to_token.get((norm_exch, norm_sym))
            if cached:
                return cached

        if sdk is None:
            raise ShoonyaSubscriptionError(
                f"No Shoonya token mapped for {norm_exch}:{norm_sym} and no SDK client provided for searchscrip."
            )

        try:
            response = sdk.searchscrip(exchange=norm_exch, searchtext=norm_sym)
        except Exception as exc:
            raise ShoonyaSubscriptionError(
                f"Shoonya searchscrip failed for {norm_exch}:{norm_sym}: {exc}"
            ) from exc

        if not isinstance(response, dict) or response.get("stat") != "Ok":
            emsg = response.get("emsg", "Unknown error") if isinstance(response, dict) else "Empty response"
            raise ShoonyaSubscriptionError(
                f"Shoonya searchscrip could not resolve {norm_exch}:{norm_sym}: {emsg}"
            )

        values = response.get("values") or []
        if not isinstance(values, list) or not values:
            raise ShoonyaSubscriptionError(
                f"Shoonya searchscrip returned no instruments for {norm_exch}:{norm_sym}."
            )

        # Prefer exact symbol or("-EQ") match on NSE/BSE
        chosen_token: str | None = None
        for item in values:
            if not isinstance(item, dict):
                continue
            tsym = str(item.get("tsym") or "").upper()
            symname = str(item.get("symname") or item.get("cname") or "").upper()
            tok = str(item.get("token") or "").strip()
            if not tok:
                continue
            if tsym in {norm_sym, f"{norm_sym}-EQ", f"{norm_sym}-BE"} or symname == norm_sym:
                chosen_token = tok
                break
        if chosen_token is None:
            first_tok = str(values[0].get("token") or "").strip() if isinstance(values[0], dict) else ""
            if first_tok:
                chosen_token = first_tok

        if not chosen_token:
            raise ShoonyaSubscriptionError(
                f"Shoonya searchscrip result for {norm_exch}:{norm_sym} lacked a valid token."
            )

        self.register(norm_exch, norm_sym, chosen_token)
        return chosen_token

    def resolve_symbol(self, exchange: str, token: str) -> str | None:
        with self._lock:
            return self._token_to_symbol.get((exchange.strip().upper(), str(token).strip()))

    def format_subscription_key(
        self,
        exchange: str,
        symbol: str,
        sdk: NorenSdkProtocol | None = None,
    ) -> str:
        token = self.resolve_token(exchange, symbol, sdk=sdk)
        return f"{exchange.strip().upper()}|{token}"


_LATEST_FEED_HEALTH_LOCK = threading.RLock()
_LATEST_FEED_HEALTH: dict[str, Any] = {
    "connected": False,
    "feed_ready": False,
    "awaiting_snapshot": True,
    "backlog_state": "normal",
    "lag_detected": False,
    "queue_maxsize": 256,
    "queue_depth": 0,
    "queue_overflow_drops": 0,
    "protocol_gap_detection": "disabled",
    "protocol_gap_reason": (
        "Shoonya touchline messages do not provide a protocol sequence field; "
        "protocol sequence gap detection stays disabled unless sequence fields are present."
    ),
    "protocol_sequence_gaps": 0,
    "reconnect_count": 0,
    "reconnect_gaps_count": 0,
    "gap_history": [],
}


def get_latest_feed_health() -> dict[str, Any]:
    with _LATEST_FEED_HEALTH_LOCK:
        return {
            ** _LATEST_FEED_HEALTH,
            "gap_history": list(_LATEST_FEED_HEALTH.get("gap_history", [])),
        }


def _extract_protocol_sequence(payload: dict[str, Any]) -> int | None:
    """Extract a protocol sequence number ONLY if the source message explicitly carries one."""
    for key in ("seq", "seqno", "sequence", "sq"):
        raw_val = payload.get(key)
        if raw_val is not None and str(raw_val).strip() != "":
            try:
                return int(str(raw_val).strip())
            except ValueError:
                return None
    return None


def _parse_source_timestamp(payload: dict[str, Any], now_utc: datetime) -> datetime | None:
    """Parse tick timestamp strictly from source message timestamp fields ('ft', 'lut', 'ltt').

    Never fabricates or falls back to local receive time if the source timestamp is missing
    or unparseable.
    """
    for key in ("ft", "lut", "ltt"):
        raw_val = payload.get(key)
        if raw_val is None:
            continue
        text = str(raw_val).strip()
        if not text:
            continue
        try:
            epoch_val = int(float(text))
            if epoch_val > 0:
                candidate = datetime.fromtimestamp(epoch_val, tz=timezone.utc)
                return min(candidate, now_utc)
        except (ValueError, OSError, OverflowError):
            pass
    return None


def parse_shoonya_tick(
    payload: dict[str, Any],
    resolver: ShoonyaTokenResolver,
    *,
    freshness_seconds: int = 300,
    fallback_now: datetime | None = None,
) -> ProviderQuote | None:
    """Parse a Shoonya WebSocket touchline ('tk' or 'tf') or REST quote payload into a ProviderQuote.

    Returns None for heartbeat/connection messages or ticks that do not carry a last traded price ('lp').
    UNVERIFIED: Shoonya field mapping ('t', 'e', 'tk', 'lp', 'ft', 'lut', 'ltt') is unverified against
    current official docs and used only in gated or simulated test flows.
    """
    if not isinstance(payload, dict):
        return None

    msg_type = payload.get("t")
    # Shoonya sends 'ck' on connection ack, 'tk' on subscription ack, 'tf' on touchline feed update.
    if msg_type in {"ck", "dk", "om"}:
        return None

    raw_lp = payload.get("lp")
    if raw_lp is None or str(raw_lp).strip() == "":
        return None

    exchange = str(payload.get("e") or payload.get("exch") or "").strip().upper()
    token = str(payload.get("tk") or payload.get("token") or "").strip()
    if exchange not in {"NSE", "BSE"} or not token:
        return None

    symbol = resolver.resolve_symbol(exchange, token)
    if not symbol:
        raw_tsym = str(payload.get("ts") or payload.get("tsym") or "").strip().upper()
        if raw_tsym.endswith("-EQ"):
            raw_tsym = raw_tsym[:-3]
        symbol = raw_tsym or None
    if not symbol:
        logger.debug("Ignoring Shoonya tick for unmapped token %s|%s.", exchange, token)
        return None

    try:
        price = Decimal(str(raw_lp).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ProviderError(f"Shoonya returned non-numeric price {raw_lp!r} for {exchange}:{symbol}.") from exc

    if not price.is_finite() or price <= 0:
        raise ProviderError(f"Shoonya returned non-positive price {price} for {exchange}:{symbol}.")

    now_utc = normalize_utc(fallback_now or datetime.now(timezone.utc))
    quoted_at = _parse_source_timestamp(payload, now_utc)
    freshness_unknown = quoted_at is None
    sequence_number = _extract_protocol_sequence(payload)

    return ProviderQuote(
        exchange=exchange,
        symbol=symbol,
        price=price,
        currency="INR",
        provider="shoonya",
        quoted_at=quoted_at,
        is_simulated=False,
        quote_type="last_trade",
        freshness_seconds=freshness_seconds,
        freshness_unknown=freshness_unknown,
        sequence_number=sequence_number,
    )


class ShoonyaPriceProvider:
    """Synchronous Shoonya REST provider implementing PriceProvider for poll_once and on-demand polls."""

    name = "shoonya"
    is_simulated = False

    def __init__(
        self,
        config: ShoonyaConfig | None = None,
        *,
        sdk: NorenSdkProtocol | None = None,
        resolver: ShoonyaTokenResolver | None = None,
    ) -> None:
        self.config = config or ShoonyaConfig.from_env()
        if not self.config.allow_unverified_auth:
            raise ShoonyaAuthError(UNVERIFIED_AUTH_ERROR_MESSAGE)
        self.sdk = sdk or _create_noren_sdk_client(self.config.host, self.config.ws_endpoint)
        self.resolver = resolver or ShoonyaTokenResolver(custom_tokens=self.config.custom_token_map)
        self._authenticated = False
        self._lock = threading.Lock()

    def ensure_authenticated(self) -> None:
        if not self.config.allow_unverified_auth:
            raise ShoonyaAuthError(UNVERIFIED_AUTH_ERROR_MESSAGE)
        with self._lock:
            if self._authenticated:
                return
            self._login_locked()

    def _login_locked(self) -> None:
        # UNVERIFIED: Direct NorenRestApiPy SDK login parameters and response structure.
        two_fa = self.config.current_two_fa()
        try:
            res = self.sdk.login(
                userid=self.config.user_id,
                password=self.config.password,
                twoFA=two_fa,
                vendor_code=self.config.vendor_code,
                api_secret=self.config.api_secret,
                imei=self.config.imei,
            )
        except Exception as exc:
            raise ShoonyaAuthError(f"Shoonya login request failed: {exc}") from exc

        if not isinstance(res, dict) or res.get("stat") != "Ok":
            emsg = res.get("emsg", "Invalid credentials or 2FA") if isinstance(res, dict) else "Empty response"
            raise ShoonyaAuthError(f"Shoonya authentication rejected: {emsg}")
        self._authenticated = True
        logger.info("Authenticated with Shoonya REST API as user %s.", self.config.user_id)

    def fetch_quote(self, exchange: str, symbol: str) -> ProviderQuote:
        self.ensure_authenticated()
        token = self.resolver.resolve_token(exchange, symbol, sdk=self.sdk)
        try:
            response = self.sdk.get_quotes(exchange=exchange.upper(), token=token)
        except Exception as exc:
            msg = str(exc).lower()
            if "rate" in msg or "429" in msg:
                raise RateLimitError(15.0) from exc
            raise ProviderError(f"Shoonya get_quotes failed for {exchange}:{symbol}: {exc}") from exc

        if not isinstance(response, dict) or response.get("stat") != "Ok":
            emsg = str(response.get("emsg") or "Unknown error") if isinstance(response, dict) else "Empty response"
            if "session" in emsg.lower() or "invalid" in emsg.lower():
                with self._lock:
                    self._authenticated = False
                    self._login_locked()
                response = self.sdk.get_quotes(exchange=exchange.upper(), token=token)
            if not isinstance(response, dict) or response.get("stat") != "Ok":
                raise ProviderError(f"Shoonya get_quotes error for {exchange}:{symbol}: {emsg}")

        quote = parse_shoonya_tick(
            response,
            self.resolver,
            freshness_seconds=self.config.quote_freshness_seconds,
        )
        if quote is None:
            raise ProviderError(f"Shoonya get_quotes response for {exchange}:{symbol} did not contain a valid price.")
        return quote


class ShoonyaWebSocketService:
    """Persistent Shoonya WebSocket streaming service with bounded queue, gap detection,
    reconnect cross-baseline resets, and target-alert evaluation.
    """

    def __init__(
        self,
        config: ShoonyaConfig | None = None,
        *,
        sdk_factory: Callable[[str, str], NorenSdkProtocol] = _create_noren_sdk_client,
        resolver: ShoonyaTokenResolver | None = None,
        calendar: ExchangeCalendar | None = None,
        session_factory: Callable[[], Session] = SessionLocal,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config or ShoonyaConfig.from_env()
        if not self.config.allow_unverified_auth:
            raise ShoonyaAuthError(UNVERIFIED_AUTH_ERROR_MESSAGE)
        self._sdk_factory = sdk_factory
        self.sdk: NorenSdkProtocol = self._sdk_factory(self.config.host, self.config.ws_endpoint)
        self.resolver = resolver or ShoonyaTokenResolver(custom_tokens=self.config.custom_token_map)
        self.calendar = calendar or NseBseExchangeCalendar()
        self.session_factory = session_factory
        self._sleep = sleep_fn
        self._notifier = MultiChannelNotifier()

        self._state_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._ws_connected = threading.Event()
        self._authenticated = False
        self._started_at = datetime.now(timezone.utc)
        self._subscribed_keys: set[str] = set()
        self._reconnect_count = 0
        self._reconnect_gaps_count = 0
        self._failed_count = 0
        self._ticks_recorded = 0
        self._last_tick_at: datetime | None = None
        self._last_error: str | None = None
        self._feed_ready = False
        self._awaiting_snapshot = True
        self._had_prior_session = False

        # Bounded queue & gap telemetry
        self._queue_overflow_drops = 0
        self._backlog_state = "normal"
        self._lag_detected = False
        self._protocol_gap_detection = "disabled"
        self._protocol_gap_reason = (
            "Shoonya touchline messages do not provide a protocol sequence field; "
            "protocol sequence gap detection stays disabled unless sequence fields are present."
        )
        self._protocol_sequence_gaps = 0
        self._last_seq_by_symbol: dict[tuple[str, str], int] = {}
        self._gap_history: list[dict[str, Any]] = []

        self._async_loop: asyncio.AbstractEventLoop | None = None
        self._async_queue: asyncio.Queue[ProviderQuote | None] | None = None
        self._publish_health()

    def attach_async_queue(
        self,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[ProviderQuote | None],
    ) -> None:
        """Forward parsed ProviderQuote ticks directly to a bounded asyncio.Queue for async worker consumption."""
        with self._state_lock:
            self._async_loop = loop
            self._async_queue = queue
        self._publish_health()

    def create_bounded_queue(self) -> asyncio.Queue[ProviderQuote | None]:
        """Create a bounded asyncio.Queue configured with tick_queue_maxsize."""
        return asyncio.Queue(maxsize=max(1, self.config.tick_queue_maxsize))

    def _enqueue_tick_nonblocking(self, quote: ProviderQuote | None) -> bool:
        """Attempt to enqueue a tick into the bounded queue; on overflow, drop with explicit counter and lag state."""
        with self._state_lock:
            queue = self._async_queue
        if queue is None:
            return False
        if quote is None:
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass
            return True

        try:
            queue.put_nowait(quote)
            with self._state_lock:
                if self._queue_overflow_drops == 0:
                    self._backlog_state = "normal"
            self._publish_health()
            return True
        except asyncio.QueueFull:
            now = datetime.now(timezone.utc)
            with self._state_lock:
                self._queue_overflow_drops += 1
                self._backlog_state = "overflow"
                self._lag_detected = True
                drops = self._queue_overflow_drops
                self._gap_history.append(
                    {
                        "kind": "local_queue_overflow",
                        "exchange": quote.exchange,
                        "symbol": quote.symbol,
                        "at": now.isoformat(),
                        "queue_overflow_drops": drops,
                        "queue_maxsize": queue.maxsize,
                    }
                )
            logger.warning(
                "Local tick queue overflow (maxsize=%d): dropped tick for %s:%s (total_overflow_drops=%d).",
                queue.maxsize,
                quote.exchange,
                quote.symbol,
                drops,
            )
            self._publish_health()
            return False

    def get_health_snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            queue_depth = self._async_queue.qsize() if self._async_queue is not None else 0
            queue_maxsize = (
                self._async_queue.maxsize
                if self._async_queue is not None and self._async_queue.maxsize > 0
                else self.config.tick_queue_maxsize
            )
            return {
                "connected": self._ws_connected.is_set(),
                "feed_ready": self._ws_connected.is_set() and self._feed_ready and not self._awaiting_snapshot,
                "awaiting_snapshot": self._awaiting_snapshot,
                "backlog_state": self._backlog_state,
                "lag_detected": self._lag_detected,
                "queue_maxsize": queue_maxsize,
                "queue_depth": queue_depth,
                "queue_overflow_drops": self._queue_overflow_drops,
                "protocol_gap_detection": self._protocol_gap_detection,
                "protocol_gap_reason": self._protocol_gap_reason,
                "protocol_sequence_gaps": self._protocol_sequence_gaps,
                "reconnect_count": self._reconnect_count,
                "reconnect_gaps_count": self._reconnect_gaps_count,
                "gap_history": list(self._gap_history),
                "last_tick_at": self._last_tick_at.isoformat() if self._last_tick_at else None,
                "last_error": self._last_error,
            }

    def _publish_health(self) -> None:
        snap = self.get_health_snapshot()
        with _LATEST_FEED_HEALTH_LOCK:
            _LATEST_FEED_HEALTH.clear()
            _LATEST_FEED_HEALTH.update(snap)

    @property
    def is_connected(self) -> bool:
        return self._ws_connected.is_set()

    @property
    def subscribed_instruments(self) -> set[str]:
        with self._state_lock:
            return set(self._subscribed_keys)

    def authenticate(self) -> dict[str, Any]:
        """Authenticate with Shoonya using user_id, password, dynamic TOTP 2FA, vendor_code, and api_secret."""
        if not self.config.allow_unverified_auth:
            raise ShoonyaAuthError(UNVERIFIED_AUTH_ERROR_MESSAGE)
        # UNVERIFIED: Direct NorenRestApiPy SDK login call.
        two_fa = self.config.current_two_fa()
        try:
            response = self.sdk.login(
                userid=self.config.user_id,
                password=self.config.password,
                twoFA=two_fa,
                vendor_code=self.config.vendor_code,
                api_secret=self.config.api_secret,
                imei=self.config.imei,
            )
        except Exception as exc:
            self._record_failure(f"Shoonya login exception: {exc}")
            raise ShoonyaAuthError(f"Shoonya login failed: {exc}") from exc

        if not isinstance(response, dict) or response.get("stat") != "Ok":
            emsg = response.get("emsg", "Authentication rejected") if isinstance(response, dict) else "Empty login response"
            self._record_failure(f"Shoonya login rejected: {emsg}")
            raise ShoonyaAuthError(f"Shoonya login rejected: {emsg}")

        with self._state_lock:
            self._authenticated = True
            self._last_error = None
        logger.info(
            "Shoonya session authenticated for user_id=%s vendor_code=%s.",
            self.config.user_id,
            self.config.vendor_code,
        )
        return response

    def load_monitored_companies(self) -> list[tuple[str, str]]:
        """Query all approved and active (exchange, symbol) pairs from the database."""
        with self.session_factory() as session:
            companies = list(
                session.scalars(
                    select(Company)
                    .where(Company.is_approved.is_(True), Company.monitoring_active.is_(True))
                    .order_by(Company.exchange, Company.symbol)
                )
            )
            return [(c.exchange, c.symbol) for c in companies]

    def build_subscription_keys(self, instruments: list[tuple[str, str]] | None = None) -> list[str]:
        """Resolve (exchange, symbol) pairs into Shoonya 'NSE|token' subscription keys."""
        targets = instruments if instruments is not None else self.load_monitored_companies()
        keys: list[str] = []
        for exchange, symbol in targets:
            try:
                key = self.resolver.format_subscription_key(exchange, symbol, sdk=self.sdk)
                keys.append(key)
            except ShoonyaSubscriptionError as exc:
                self._record_failure(str(exc))
                logger.warning("Skipping subscription for %s:%s: %s", exchange, symbol, exc)
        return keys

    def subscribe_all(self, instruments: list[tuple[str, str]] | None = None) -> list[str]:
        """Subscribe to NSE/BSE instrument tokens in batches over the active WebSocket."""
        keys = self.build_subscription_keys(instruments)
        if not keys:
            logger.info("No active NSE/BSE instruments to subscribe on Shoonya WebSocket.")
            return []

        batch_size = max(1, self.config.subscription_batch_size)
        subscribed: list[str] = []
        for idx in range(0, len(keys), batch_size):
            batch = keys[idx : idx + batch_size]
            # NorenRestApiPy accepts either a list of 'NSE|token' strings or '#'-joined string
            payload = batch if len(batch) > 1 else batch[0]
            try:
                self.sdk.subscribe(payload)
                subscribed.extend(batch)
            except Exception as exc:
                self._record_failure(f"WebSocket subscribe failed: {exc}")
                raise ShoonyaSubscriptionError(f"Failed to subscribe batch on Shoonya WebSocket: {exc}") from exc

        with self._state_lock:
            self._subscribed_keys.update(subscribed)
        logger.info("Subscribed to %d Shoonya instrument token(s).", len(subscribed))
        return subscribed

    def record_reconnect_gap(
        self,
        *,
        reason: str = "websocket_reconnect",
        symbols: list[tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Record a feed disconnect/reconnect gap, mark feed as awaiting a fresh snapshot,
        and reset each affected symbol's cross baseline so no crossing is inferred across the gap.
        """
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            affected_count = reset_symbol_cross_baselines(
                session,
                symbols,
                reason=reason,
                now=now,
            )
            session.commit()

        with self._state_lock:
            self._reconnect_count += 1
            self._reconnect_gaps_count += 1
            self._feed_ready = False
            self._awaiting_snapshot = True
            gap_entry = {
                "kind": "reconnect_gap",
                "reason": reason,
                "at": now.isoformat(),
                "reconnect_gaps_count": self._reconnect_gaps_count,
                "affected_symbols_count": affected_count,
            }
            self._gap_history.append(gap_entry)
        logger.warning(
            "Reconnect gap recorded (%s): awaiting fresh snapshot and reset cross baselines for %d symbol(s).",
            reason,
            affected_count,
        )
        self._publish_health()
        return gap_entry

    def _on_socket_open(self) -> None:
        logger.info("Shoonya WebSocket connection opened.")
        self._ws_connected.set()
        with self._state_lock:
            was_reconnecting = self._had_prior_session
            self._had_prior_session = True
            self._last_error = None
            if was_reconnecting:
                self._feed_ready = False
                self._awaiting_snapshot = True
        try:
            self.subscribe_all()
        except Exception:
            logger.exception("Error subscribing instruments on WebSocket open.")
        self._publish_health()
        self._sync_heartbeat(connection_status="connected", websocket_status="connected")

    def _on_socket_close(self) -> None:
        logger.warning("Shoonya WebSocket connection closed.")
        self._ws_connected.clear()
        with self._state_lock:
            self._subscribed_keys.clear()
            self._feed_ready = False
            self._awaiting_snapshot = True
        if not self._stop_event.is_set():
            self.record_reconnect_gap(reason="websocket_closed")
            self._sync_heartbeat(
                connection_status="reconnecting",
                websocket_status="reconnecting",
                last_error=self._last_error or "Shoonya WebSocket closed unexpectedly; reconnecting.",
            )
        else:
            self._publish_health()

    def _on_socket_error(self, error: Any) -> None:
        err_msg = str(error)[:1000] if error is not None else "Unknown Shoonya WebSocket error"
        logger.error("Shoonya WebSocket error: %s", err_msg)
        self._ws_connected.clear()
        with self._state_lock:
            self._feed_ready = False
            self._awaiting_snapshot = True
        self._record_failure(err_msg)
        self._publish_health()
        self._sync_heartbeat(
            connection_status="error",
            websocket_status="reconnecting",
            last_error=err_msg,
        )

    def _check_protocol_sequence(self, quote: ProviderQuote, now: datetime) -> None:
        """Detect protocol sequence gaps ONLY when the source message carries a sequence field."""
        if quote.sequence_number is None:
            return
        identity = (quote.exchange.upper(), quote.symbol.upper())
        gap_detected = False
        expected_seq = 0
        with self._state_lock:
            self._protocol_gap_detection = "enabled"
            self._protocol_gap_reason = (
                "Source messages carry a protocol sequence field; sequence gap detection is active."
            )
            prev_seq = self._last_seq_by_symbol.get(identity)
            self._last_seq_by_symbol[identity] = quote.sequence_number
            if prev_seq is not None and quote.sequence_number > prev_seq + 1:
                gap_detected = True
                expected_seq = prev_seq + 1
                self._protocol_sequence_gaps += 1
                self._feed_ready = False
                self._awaiting_snapshot = True
                self._gap_history.append(
                    {
                        "kind": "protocol_sequence_gap",
                        "exchange": identity[0],
                        "symbol": identity[1],
                        "expected_seq": expected_seq,
                        "actual_seq": quote.sequence_number,
                        "at": now.isoformat(),
                    }
                )
        if gap_detected:
            logger.warning(
                "Protocol sequence gap detected for %s:%s (expected=%d, actual=%d); resetting cross baseline.",
                identity[0],
                identity[1],
                expected_seq,
                quote.sequence_number,
            )
            with self.session_factory() as session:
                reset_symbol_cross_baselines(
                    session,
                    [identity],
                    reason=f"protocol_sequence_gap({expected_seq}->{quote.sequence_number})",
                    now=now,
                )
                session.commit()
            self._publish_health()

    def _on_tick(self, message: dict[str, Any]) -> None:
        """Handle incoming Shoonya WebSocket tick ('tk' or 'tf').

        If an asyncio queue is attached (used by the async worker in app/worker.py),
        enqueues the normalized ProviderQuote into the bounded queue without blocking the socket thread.
        Otherwise persists and evaluates target alerts inline.
        """
        try:
            now = datetime.now(timezone.utc)
            quote = parse_shoonya_tick(
                message,
                self.resolver,
                freshness_seconds=self.config.quote_freshness_seconds,
                fallback_now=now,
            )
            if quote is None:
                return

            self._check_protocol_sequence(quote, now)

            quote_is_fresh = (
                not quote.freshness_unknown
                and quote.quoted_at is not None
                and (now - normalize_utc(quote.quoted_at)).total_seconds() <= quote.freshness_seconds
            )

            with self._state_lock:
                self._ticks_recorded += 1
                self._last_tick_at = now
                self._last_error = None
                if quote_is_fresh:
                    self._feed_ready = True
                    self._awaiting_snapshot = False
                async_loop = self._async_loop
                async_queue = self._async_queue

            if async_loop is not None and async_queue is not None:
                if async_loop.is_running():
                    async_loop.call_soon_threadsafe(self._enqueue_tick_nonblocking, quote)
                else:
                    self._enqueue_tick_nonblocking(quote)
                return

            alerts_enabled = os.environ.get("ALERT_RULES_ENABLED", "true").lower() == "true"

            with self.session_factory() as session:
                stored = record_quote(
                    session,
                    quote,
                    now=now,
                    alerts_enabled=alerts_enabled,
                    fixture_alerts_enabled=False,
                )

            with self._state_lock:
                if stored.status == "fresh":
                    self._feed_ready = True
                    self._awaiting_snapshot = False

            drain_outbox(self._notifier, session_factory=self.session_factory, now=now)

            self._publish_health()
            self._sync_heartbeat(
                connection_status="connected",
                websocket_status="connected",
                last_quote_at=now if stored.status == "fresh" else None,
            )
            logger.debug(
                "Processed Shoonya WebSocket tick %s:%s price=%s status=%s.",
                stored.exchange,
                stored.symbol,
                stored.price,
                stored.status,
            )
        except Exception as exc:
            err_msg = f"Tick processing error: {exc}"[:1000]
            logger.exception("Failed to process Shoonya tick: %s", message)
            self._record_failure(err_msg)
            self._publish_health()

    async def stream_quotes(
        self,
        *,
        health_check_interval: float = 5.0,
    ) -> AsyncIterator[ProviderQuote]:
        """Start the Shoonya WebSocket connection manager in a background daemon thread
        and yield normalized ProviderQuote ticks asynchronously from a bounded queue.
        """
        loop = asyncio.get_running_loop()
        queue = self.create_bounded_queue()
        self.attach_async_queue(loop, queue)

        runner_thread = threading.Thread(
            target=self._run_forever_and_signal_end,
            kwargs={"health_check_interval": health_check_interval},
            name="shoonya-ws-runner",
            daemon=True,
        )
        runner_thread.start()
        try:
            while not self._stop_event.is_set():
                quote = await queue.get()
                if quote is None:
                    break
                yield quote
        finally:
            self.stop()

    def _run_forever_and_signal_end(self, *, health_check_interval: float = 5.0) -> None:
        try:
            self.run_forever(health_check_interval=health_check_interval)
        finally:
            with self._state_lock:
                loop = self._async_loop
            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(self._enqueue_tick_nonblocking, None)

    def connect_once(self) -> None:
        """Authenticate (if needed) and open the Shoonya WebSocket stream."""
        if not self._authenticated:
            self.authenticate()
        self._ws_connected.clear()
        self.sdk.start_websocket(
            subscribe_callback=self._on_tick,
            order_update_callback=None,
            socket_open_callback=self._on_socket_open,
            socket_close_callback=self._on_socket_close,
            socket_error_callback=self._on_socket_error,
        )

    def run_forever(self, *, health_check_interval: float = 5.0) -> None:
        """Maintain the Shoonya WebSocket feed with automatic exponential-backoff reconnection."""
        self._stop_event.clear()
        self._started_at = datetime.now(timezone.utc)
        attempt = 0

        while not self._stop_event.is_set():
            try:
                if attempt > 0:
                    self.record_reconnect_gap(reason=f"reconnect_attempt_{attempt}")
                    with self._state_lock:
                        self._authenticated = False
                    self.sdk = self._sdk_factory(self.config.host, self.config.ws_endpoint)

                self.connect_once()

                # Wait briefly for socket_open_callback to fire
                opened = self._ws_connected.wait(timeout=15.0)
                if not opened and not self._stop_event.is_set():
                    raise ShoonyaSubscriptionError("Timed out waiting for Shoonya WebSocket open handshake.")

                attempt = 0
                # Monitor active connection and dynamically subscribe newly added companies
                while self._ws_connected.is_set() and not self._stop_event.is_set():
                    self.refresh_subscriptions_if_needed()
                    self._sleep(health_check_interval)

            except Exception as exc:
                attempt += 1
                err_msg = str(exc)[:1000]
                self._record_failure(err_msg)
                self._publish_health()
                self._sync_heartbeat(
                    connection_status="reconnecting",
                    websocket_status="reconnecting",
                    last_error=err_msg,
                )
                max_attempts = self.config.max_reconnect_attempts
                if max_attempts > 0 and attempt >= max_attempts:
                    logger.error(
                        "Shoonya WebSocket exhausted maximum reconnect attempts (%d).",
                        max_attempts,
                    )
                    self._sync_heartbeat(
                        connection_status="error",
                        websocket_status="disconnected",
                        last_error=f"Exhausted {max_attempts} reconnect attempts: {err_msg}",
                    )
                    raise

                delay = min(
                    self.config.reconnect_max_seconds,
                    self.config.reconnect_base_seconds * (2 ** max(0, attempt - 1)),
                )
                logger.warning(
                    "Shoonya WebSocket disconnected (attempt %d); reconnecting in %.1f seconds.",
                    attempt,
                    delay,
                )
                self._sleep(delay)

    def refresh_subscriptions_if_needed(self) -> list[str]:
        """Subscribe any newly approved/activated companies without dropping the socket."""
        if not self._ws_connected.is_set():
            return []
        desired_keys = set(self.build_subscription_keys())
        with self._state_lock:
            missing = sorted(desired_keys - self._subscribed_keys)
        if not missing:
            return []
        payload = missing if len(missing) > 1 else missing[0]
        self.sdk.subscribe(payload)
        with self._state_lock:
            self._subscribed_keys.update(missing)
        logger.info("Dynamically subscribed %d new Shoonya token(s).", len(missing))
        return missing

    def stop(self) -> None:
        """Stop the WebSocket loop and close the underlying connection cleanly."""
        self._stop_event.set()
        self._ws_connected.clear()
        try:
            self.sdk.close_websocket()
        except Exception:
            logger.debug("Ignored exception while closing Shoonya WebSocket.", exc_info=True)
        self._sync_heartbeat(connection_status="disconnected", websocket_status="disconnected")

    def _record_failure(self, message: str) -> None:
        with self._state_lock:
            self._failed_count += 1
            self._last_error = message[:1000]

    def _sync_heartbeat(
        self,
        *,
        connection_status: str,
        websocket_status: str,
        last_quote_at: datetime | None = None,
        last_error: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            monitored_count = len(
                list(
                    session.scalars(
                        select(Company).where(
                            Company.is_approved.is_(True),
                            Company.monitoring_active.is_(True),
                        )
                    )
                )
            )
            heartbeat = session.get(WorkerHeartbeat, "price")
            effective_quote_at = last_quote_at or self._last_tick_at
            if heartbeat is None:
                heartbeat = WorkerHeartbeat(
                    name="price",
                    provider="shoonya",
                    exchange_calendar=self.calendar.name,
                    started_at=self._started_at,
                    last_poll_at=effective_quote_at or now,
                    last_error=last_error,
                    connection_status=connection_status,
                    websocket_status=websocket_status,
                    last_successful_quote_at=effective_quote_at,
                    last_failed_at=now if last_error else None,
                    failed_requests_count=self._failed_count,
                    reconnect_count=self._reconnect_count,
                    monitored_symbols_count=monitored_count,
                    stale_symbols_count=0,
                )
                session.add(heartbeat)
            else:
                heartbeat.provider = "shoonya"
                heartbeat.exchange_calendar = self.calendar.name
                heartbeat.started_at = self._started_at
                heartbeat.last_poll_at = effective_quote_at or now
                heartbeat.last_error = last_error
                heartbeat.connection_status = connection_status
                heartbeat.websocket_status = websocket_status
                if effective_quote_at is not None:
                    heartbeat.last_successful_quote_at = effective_quote_at
                if last_error:
                    heartbeat.last_failed_at = now
                heartbeat.failed_requests_count = self._failed_count
                heartbeat.reconnect_count = self._reconnect_count
                heartbeat.monitored_symbols_count = monitored_count
            session.commit()


def main() -> None:
    """CLI entrypoint to run the Shoonya live WebSocket price-monitoring service."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    service = ShoonyaWebSocketService()
    try:
        service.run_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down Shoonya WebSocket service.")
        service.stop()


if __name__ == "__main__":
    main()

