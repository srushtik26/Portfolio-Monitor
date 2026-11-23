"""Shared per-symbol market-data provider coordinator (yfinance PRIMARY, Angel One REST BACKUP).

Responsibilities:
- Enforces yfinance as primary (`PRICE_PROVIDER_PRIMARY=yfinance`) and Angel One REST as backup
  (`PRICE_PROVIDER_BACKUP=angel_rest`).
- Shares ONE in-memory cache and ONE rate-limited request queue between the background worker
  and manual API refreshes (`POST /api/price-feed/poll`).
- Namespaces Angel-backed cache entries separately from yfinance cache entries so non-owners
  never read owner-only Angel quotes from cache.
- Per-symbol fallback ONLY on primary failure, no valid price, or staleness.
- Returns to yfinance after 2 consecutive good primary fetches.
- Signals `source_changed=True` whenever a symbol switches source (yfinance <-> angel_rest)
  so callers reset crossing baselines and never infer crossings across a source switch or gap.
- Never falls back to fixture/simulated data for failed real quotes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import os
from threading import Lock, RLock
from time import monotonic, sleep as time_sleep
from typing import Any

from .angel_rest import ANGEL_PROVIDER_LABEL, AngelRestConfig, AngelRestQuoteProvider
from .prices import (
    PriceProvider,
    ProviderError,
    ProviderQuote,
    RateLimitError,
    YFinanceProvider,
    normalize_utc,
    register_verified_ticker,
)

logger = logging.getLogger("portfolio.provider_coordinator")

YFINANCE_SOURCE_KEY = "yfinance"
ANGEL_SOURCE_KEY = "angel_rest"
YFINANCE_DELAYED_LABEL = "Yahoo Finance (delayed candle data)"
RECOVERY_SUCCESS_THRESHOLD = 2


def configured_primary_provider() -> str:
    return (
        os.environ.get("PRICE_PROVIDER_PRIMARY", YFINANCE_SOURCE_KEY).strip().lower()
        or YFINANCE_SOURCE_KEY
    )


def configured_backup_provider() -> str:
    return (
        os.environ.get("PRICE_PROVIDER_BACKUP", ANGEL_SOURCE_KEY).strip().lower()
        or ANGEL_SOURCE_KEY
    )


def configured_stale_after_seconds() -> int:
    try:
        return max(30, int(os.environ.get("YFINANCE_STALE_AFTER_SECONDS", "300")))
    except ValueError:
        return 300


def configured_cache_ttl_seconds() -> int:
    try:
        return max(1, int(os.environ.get("YFINANCE_CACHE_TTL_SECONDS", "60")))
    except ValueError:
        return 60


def is_angel_provider_name(provider_name: str | None) -> bool:
    if not provider_name:
        return False
    lowered = provider_name.strip().lower()
    return "angel" in lowered


def is_yfinance_provider_name(provider_name: str | None) -> bool:
    if not provider_name:
        return False
    lowered = provider_name.strip().lower()
    return "yahoo" in lowered or "yfinance" in lowered


def is_angel_owner_user(user: Any | None) -> bool:
    """Owner-only gate for Angel One REST access.

    Signed-in workspace access (EMPLOYEE) is never enough.
    Requires ADMIN role, and if ANGEL_OWNER_NAME is configured in the server environment,
    the user's normalized name must match ANGEL_OWNER_NAME.
    """
    if user is None:
        return False
    role = getattr(user, "role", None)
    if role != "ADMIN":
        return False
    owner_name = os.environ.get("ANGEL_OWNER_NAME", "").strip().lower()
    if owner_name:
        normalized = str(getattr(user, "normalized_name", "") or "").strip().lower()
        display = str(getattr(user, "name", "") or "").strip().lower()
        return owner_name in {normalized, display}
    return True


def is_worker_angel_backup_enabled() -> bool:
    """Background worker only activates Angel backup when explicitly enabled by the owner."""
    return os.environ.get("ANGEL_OWNER_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@dataclass
class SymbolSourceState:
    exchange: str
    symbol: str
    active_source: str = YFINANCE_SOURCE_KEY
    source_label: str = YFINANCE_DELAYED_LABEL
    source_time: datetime | None = None
    fallback_active: bool = False
    fallback_reason: str | None = None
    consecutive_primary_successes: int = 0
    last_error: str | None = None
    last_updated_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "exchange": self.exchange,
            "symbol": self.symbol,
            "source": self.active_source,
            "source_label": self.source_label,
            "source_time": self.source_time.isoformat() if self.source_time else None,
            "fallback_active": self.fallback_active,
            "fallback_reason": self.fallback_reason,
            "consecutive_primary_successes": self.consecutive_primary_successes,
            "last_error": self.last_error,
            "last_updated_at": self.last_updated_at.isoformat() if self.last_updated_at else None,
        }


@dataclass(frozen=True)
class CoordinatedQuoteResult:
    quote: ProviderQuote
    source: str
    source_label: str
    fallback_active: bool
    fallback_reason: str | None
    source_changed: bool
    previous_source: str | None
    from_cache: bool = False


class SharedQuoteCoordinator:
    """Shared per-symbol coordinator with yfinance primary and Angel One REST backup."""

    name = YFINANCE_SOURCE_KEY
    is_simulated = False

    def __init__(
        self,
        *,
        primary: PriceProvider | None = None,
        backup: AngelRestQuoteProvider | PriceProvider | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
        cache_ttl_seconds: int | None = None,
        stale_after_seconds: int | None = None,
        min_request_interval_seconds: float = 0.05,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic_clock or monotonic
        self._sleep = sleeper or time_sleep
        self.cache_ttl_seconds = (
            configured_cache_ttl_seconds() if cache_ttl_seconds is None else max(0, cache_ttl_seconds)
        )
        self.stale_after_seconds = (
            configured_stale_after_seconds()
            if stale_after_seconds is None
            else max(1, stale_after_seconds)
        )
        self.primary: PriceProvider = primary or YFinanceProvider(
            cache_ttl_seconds=self.cache_ttl_seconds,
            stale_after_seconds=self.stale_after_seconds,
            clock=self._clock,
        )
        self._explicit_backup = backup
        self._min_request_interval_seconds = max(0.0, float(min_request_interval_seconds))

        # One shared rate-limited queue lock across worker + manual refresh
        self._queue_lock = Lock()
        self._last_upstream_call_mono = 0.0
        self._queued_requests_total = 0

        # Namespaced cache: (source_namespace, exchange, symbol) -> (monotonic_ts, CoordinatedQuoteResult)
        self._state_lock = RLock()
        self._cache: dict[tuple[str, str, str], tuple[float, CoordinatedQuoteResult]] = {}
        self._symbol_states: dict[tuple[str, str], SymbolSourceState] = {}

    def _get_backup_provider(self) -> PriceProvider | None:
        if self._explicit_backup is not None:
            if isinstance(self._explicit_backup, AngelRestQuoteProvider):
                return self._explicit_backup if self._explicit_backup.is_configured else None
            return self._explicit_backup
        if configured_backup_provider() != ANGEL_SOURCE_KEY:
            return None
        cfg = AngelRestConfig.from_env()
        if cfg is None:
            return None
        return AngelRestQuoteProvider(cfg, clock=self._clock)

    def backup_status(self, *, allow_backup: bool = True) -> str:
        if not allow_backup:
            return "restricted"
        if self._explicit_backup is not None:
            if isinstance(self._explicit_backup, AngelRestQuoteProvider):
                return "configured" if self._explicit_backup.is_configured else "not_configured"
            return "configured"
        if configured_backup_provider() != ANGEL_SOURCE_KEY:
            return "not_configured"
        return "configured" if AngelRestConfig.is_configured() else "not_configured"

    def _throttle_upstream(self) -> None:
        """Serialize and rate-limit upstream quote requests across worker and manual refresh."""
        if self._min_request_interval_seconds <= 0:
            return
        now_m = self._monotonic()
        elapsed = now_m - self._last_upstream_call_mono
        if self._last_upstream_call_mono > 0 and elapsed < self._min_request_interval_seconds:
            self._sleep(self._min_request_interval_seconds - elapsed)
        self._last_upstream_call_mono = self._monotonic()

    def _is_quote_valid_and_fresh(self, quote: ProviderQuote, now: datetime) -> tuple[bool, str | None]:
        if quote.is_simulated:
            return False, "primary_simulated_rejected"
        if quote.price is None or not quote.price.is_finite() or quote.price <= 0:
            return False, "primary_no_valid_price"
        if quote.quoted_at is None:
            return False, "primary_missing_timestamp"
        quoted_utc = normalize_utc(quote.quoted_at)
        threshold = max(1, min(self.stale_after_seconds, quote.freshness_seconds or self.stale_after_seconds))
        if now - quoted_utc > timedelta(seconds=threshold):
            return False, "primary_stale"
        return True, None

    def _get_cached(self, namespace: str, exchange: str, symbol: str) -> CoordinatedQuoteResult | None:
        if self.cache_ttl_seconds <= 0:
            return None
        key = (namespace, exchange, symbol)
        with self._state_lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            cached_mono, result = entry
            if self._monotonic() - cached_mono > self.cache_ttl_seconds:
                self._cache.pop(key, None)
                return None
            return CoordinatedQuoteResult(
                quote=result.quote,
                source=result.source,
                source_label=result.source_label,
                fallback_active=result.fallback_active,
                fallback_reason=result.fallback_reason,
                source_changed=False,
                previous_source=result.source,
                from_cache=True,
            )

    def _put_cached(self, namespace: str, exchange: str, symbol: str, result: CoordinatedQuoteResult) -> None:
        if self.cache_ttl_seconds <= 0:
            return
        with self._state_lock:
            self._cache[(namespace, exchange, symbol)] = (self._monotonic(), result)

    def fetch_coordinated_quote(
        self,
        exchange: str,
        symbol: str,
        *,
        allow_backup: bool = False,
        force_refresh: bool = False,
        now: datetime | None = None,
    ) -> CoordinatedQuoteResult:
        """Fetch a quote for (exchange, symbol) through the shared cache & rate-limited queue."""
        exch = exchange.strip().upper()
        sym = symbol.strip().upper()
        identity = (exch, sym)
        current_time = normalize_utc(now or self._clock())

        # Check shared cache first (namespaced so non-owners never see Angel-backed cache entries)
        if not force_refresh:
            if allow_backup:
                with self._state_lock:
                    sym_state = self._symbol_states.get(identity)
                    preferred_ns = (
                        ANGEL_SOURCE_KEY
                        if (sym_state is not None and sym_state.fallback_active)
                        else YFINANCE_SOURCE_KEY
                    )
                cached = self._get_cached(preferred_ns, exch, sym)
                if cached is not None:
                    return cached
            else:
                cached = self._get_cached(YFINANCE_SOURCE_KEY, exch, sym)
                if cached is not None:
                    return cached

        with self._queue_lock:
            self._queued_requests_total += 1
            register_verified_ticker(exch, sym)

            with self._state_lock:
                sym_state = self._symbol_states.setdefault(
                    identity,
                    SymbolSourceState(exchange=exch, symbol=sym),
                )
                prev_source = sym_state.active_source
                currently_in_fallback = sym_state.fallback_active

            # Step 1: Always attempt primary (yfinance) first
            primary_quote: ProviderQuote | None = None
            primary_issue: str | None = None
            try:
                if (force_refresh or self.cache_ttl_seconds == 0) and hasattr(self.primary, "_cache"):
                    primary_cache = getattr(self.primary, "_cache", None)
                    if isinstance(primary_cache, dict):
                        primary_cache.pop(identity, None)
                self._throttle_upstream()
                primary_quote = self.primary.fetch_quote(exch, sym)
                valid_fresh, reason = self._is_quote_valid_and_fresh(primary_quote, current_time)
                if not valid_fresh:
                    primary_issue = reason
            except RateLimitError:
                primary_issue = "primary_rate_limited"
            except (ProviderError, RuntimeError, ValueError) as exc:
                primary_issue = f"primary_failed: {exc}"

            # Step 2: Primary succeeded and is fresh
            if primary_quote is not None and primary_issue is None:
                if not currently_in_fallback or not allow_backup:
                    # Normal yfinance primary path
                    source_changed = prev_source != YFINANCE_SOURCE_KEY
                    result = CoordinatedQuoteResult(
                        quote=primary_quote,
                        source=YFINANCE_SOURCE_KEY,
                        source_label=YFINANCE_DELAYED_LABEL,
                        fallback_active=False,
                        fallback_reason=None,
                        source_changed=source_changed,
                        previous_source=prev_source,
                    )
                    with self._state_lock:
                        sym_state.active_source = YFINANCE_SOURCE_KEY
                        sym_state.source_label = YFINANCE_DELAYED_LABEL
                        sym_state.source_time = primary_quote.quoted_at
                        sym_state.fallback_active = False
                        sym_state.fallback_reason = None
                        sym_state.consecutive_primary_successes = min(
                            sym_state.consecutive_primary_successes + 1,
                            RECOVERY_SUCCESS_THRESHOLD,
                        )
                        sym_state.last_error = None
                        sym_state.last_updated_at = current_time
                    self._put_cached(YFINANCE_SOURCE_KEY, exch, sym, result)
                    return result

                # Currently in fallback: require 2 consecutive good primary fetches before returning
                with self._state_lock:
                    sym_state.consecutive_primary_successes += 1
                    good_count = sym_state.consecutive_primary_successes

                if good_count >= RECOVERY_SUCCESS_THRESHOLD:
                    source_changed = prev_source != YFINANCE_SOURCE_KEY
                    result = CoordinatedQuoteResult(
                        quote=primary_quote,
                        source=YFINANCE_SOURCE_KEY,
                        source_label=YFINANCE_DELAYED_LABEL,
                        fallback_active=False,
                        fallback_reason=None,
                        source_changed=source_changed,
                        previous_source=prev_source,
                    )
                    with self._state_lock:
                        sym_state.active_source = YFINANCE_SOURCE_KEY
                        sym_state.source_label = YFINANCE_DELAYED_LABEL
                        sym_state.source_time = primary_quote.quoted_at
                        sym_state.fallback_active = False
                        sym_state.fallback_reason = None
                        sym_state.last_error = None
                        sym_state.last_updated_at = current_time
                    self._put_cached(YFINANCE_SOURCE_KEY, exch, sym, result)
                    return result

                # Only 1 consecutive good primary fetch so far -> stay on backup for this cycle
                backup_provider = self._get_backup_provider()
                if backup_provider is not None:
                    try:
                        self._throttle_upstream()
                        backup_quote = backup_provider.fetch_quote(exch, sym)
                        if backup_quote.is_simulated:
                            raise ProviderError("Simulated backup quote rejected.")
                        source_changed = prev_source != ANGEL_SOURCE_KEY
                        result = CoordinatedQuoteResult(
                            quote=backup_quote,
                            source=ANGEL_SOURCE_KEY,
                            source_label=ANGEL_PROVIDER_LABEL,
                            fallback_active=True,
                            fallback_reason=sym_state.fallback_reason or "recovering_primary",
                            source_changed=source_changed,
                            previous_source=prev_source,
                        )
                        with self._state_lock:
                            sym_state.active_source = ANGEL_SOURCE_KEY
                            sym_state.source_label = ANGEL_PROVIDER_LABEL
                            sym_state.source_time = backup_quote.quoted_at
                            sym_state.last_updated_at = current_time
                        self._put_cached(ANGEL_SOURCE_KEY, exch, sym, result)
                        return result
                    except (ProviderError, RuntimeError, ValueError) as backup_exc:
                        logger.warning(
                            "Angel backup failed during recovery cycle for %s:%s (%s); using fresh yfinance quote.",
                            exch,
                            sym,
                            backup_exc,
                        )
                # If backup wasn't available during recovery probe, use the fresh primary quote
                source_changed = prev_source != YFINANCE_SOURCE_KEY
                result = CoordinatedQuoteResult(
                    quote=primary_quote,
                    source=YFINANCE_SOURCE_KEY,
                    source_label=YFINANCE_DELAYED_LABEL,
                    fallback_active=False,
                    fallback_reason=None,
                    source_changed=source_changed,
                    previous_source=prev_source,
                )
                with self._state_lock:
                    sym_state.active_source = YFINANCE_SOURCE_KEY
                    sym_state.source_label = YFINANCE_DELAYED_LABEL
                    sym_state.source_time = primary_quote.quoted_at
                    sym_state.fallback_active = False
                    sym_state.fallback_reason = None
                    sym_state.last_error = None
                    sym_state.last_updated_at = current_time
                self._put_cached(YFINANCE_SOURCE_KEY, exch, sym, result)
                return result

            # Step 3: Primary failed, had no valid price, or was stale -> reset consecutive primary count
            with self._state_lock:
                sym_state.consecutive_primary_successes = 0
                sym_state.last_error = primary_issue

            backup_provider = self._get_backup_provider() if allow_backup else None
            if backup_provider is not None:
                try:
                    self._throttle_upstream()
                    backup_quote = backup_provider.fetch_quote(exch, sym)
                    if backup_quote.is_simulated:
                        raise ProviderError("Simulated backup quote rejected.")
                    source_changed = prev_source != ANGEL_SOURCE_KEY
                    result = CoordinatedQuoteResult(
                        quote=backup_quote,
                        source=ANGEL_SOURCE_KEY,
                        source_label=ANGEL_PROVIDER_LABEL,
                        fallback_active=True,
                        fallback_reason=primary_issue,
                        source_changed=source_changed,
                        previous_source=prev_source,
                    )
                    with self._state_lock:
                        sym_state.active_source = ANGEL_SOURCE_KEY
                        sym_state.source_label = ANGEL_PROVIDER_LABEL
                        sym_state.source_time = backup_quote.quoted_at
                        sym_state.fallback_active = True
                        sym_state.fallback_reason = primary_issue
                        sym_state.last_error = None
                        sym_state.last_updated_at = current_time
                    self._put_cached(ANGEL_SOURCE_KEY, exch, sym, result)
                    return result
                except (ProviderError, RuntimeError, ValueError) as backup_err:
                    logger.warning(
                        "Angel One REST backup failed for %s:%s (%s).",
                        exch,
                        sym,
                        backup_err,
                    )
                    with self._state_lock:
                        sym_state.last_error = f"{primary_issue}; backup_failed: {backup_err}"

            # Step 4: Backup is not configured, restricted, or also failed.
            # If primary returned a stale (but valid-price) quote, return it so staleness is persisted
            # accurately (never falling back to fixture/simulated data).
            if primary_quote is not None and primary_issue == "primary_stale":
                source_changed = prev_source != YFINANCE_SOURCE_KEY
                result = CoordinatedQuoteResult(
                    quote=primary_quote,
                    source=YFINANCE_SOURCE_KEY,
                    source_label=YFINANCE_DELAYED_LABEL,
                    fallback_active=False,
                    fallback_reason="primary_stale_backup_unavailable",
                    source_changed=source_changed,
                    previous_source=prev_source,
                )
                with self._state_lock:
                    sym_state.active_source = YFINANCE_SOURCE_KEY
                    sym_state.source_label = YFINANCE_DELAYED_LABEL
                    sym_state.source_time = primary_quote.quoted_at
                    sym_state.fallback_active = False
                    sym_state.fallback_reason = (
                        "primary_stale (backup not configured)"
                        if self.backup_status(allow_backup=allow_backup) == "not_configured"
                        else primary_issue
                    )
                    sym_state.last_updated_at = current_time
                self._put_cached(YFINANCE_SOURCE_KEY, exch, sym, result)
                return result

            raise ProviderError(
                f"Quote unavailable for {exch}:{sym} ({primary_issue or 'primary_failed'})."
            )

    def fetch_quote(self, exchange: str, symbol: str) -> ProviderQuote:
        """PriceProvider-compatible wrapper (owner-gated backup via worker env flag)."""
        return self.fetch_coordinated_quote(
            exchange,
            symbol,
            allow_backup=is_worker_angel_backup_enabled(),
        ).quote

    def get_health_snapshot(self, *, include_angel_details: bool = False) -> dict[str, Any]:
        """Return coordinator health summary, redacting Angel details for non-owners."""
        with self._state_lock:
            symbols_out: list[dict[str, Any]] = []
            fallback_count = 0
            for st in self._symbol_states.values():
                if st.active_source == ANGEL_SOURCE_KEY or st.fallback_active:
                    if not include_angel_details:
                        continue
                    fallback_count += 1
                symbols_out.append(st.to_dict())

        return {
            "primary_provider": configured_primary_provider(),
            "primary_label": YFINANCE_DELAYED_LABEL,
            "data_delay_label": "delayed_candle_data",
            "backup_provider": configured_backup_provider(),
            "backup_status": self.backup_status(allow_backup=include_angel_details),
            "fallback_active_count": fallback_count if include_angel_details else 0,
            "queued_requests_total": self._queued_requests_total,
            "symbols": symbols_out,
        }


_SHARED_COORDINATOR_LOCK = Lock()
_SHARED_COORDINATOR: SharedQuoteCoordinator | None = None


def get_shared_coordinator() -> SharedQuoteCoordinator:
    global _SHARED_COORDINATOR
    with _SHARED_COORDINATOR_LOCK:
        if _SHARED_COORDINATOR is None:
            _SHARED_COORDINATOR = SharedQuoteCoordinator()
        return _SHARED_COORDINATOR


def reset_shared_coordinator(coordinator: SharedQuoteCoordinator | None = None) -> None:
    global _SHARED_COORDINATOR
    with _SHARED_COORDINATOR_LOCK:
        _SHARED_COORDINATOR = coordinator
