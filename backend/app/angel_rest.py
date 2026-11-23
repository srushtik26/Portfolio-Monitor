"""Server-only Angel One SmartAPI REST quote adapter (backup market-data provider).

Quotes-only implementation:
- Authenticates strictly via server-only ANGEL_* environment variables.
- Fetches market LTP/quote data only.
- NEVER accesses holdings, positions, orders, or user profile endpoints.
- NEVER reuses Shoonya tokens, endpoints, units, or SHOONYA_* environment variables.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import logging
import os
import struct
from threading import RLock
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .prices import ProviderError, ProviderQuote, RateLimitError

logger = logging.getLogger("portfolio.angel_rest")

INDIA_TZ = ZoneInfo("Asia/Kolkata")
DEFAULT_ANGEL_BASE_URL = "https://apiconnect.angelone.in"
ANGEL_LOGIN_PATH = "/rest/auth/angelbroking/user/v1/loginByPassword"
ANGEL_QUOTE_PATH = "/rest/secure/angelbroking/market/v1/quote/"
ANGEL_PROVIDER_LABEL = "Angel One REST (backup)"

# Explicit Angel One SmartAPI symboltoken mappings (independent of any other broker).
# Additional mappings can be supplied via ANGEL_TOKEN_MAP_JSON in server environment.
ANGEL_SYMBOL_TOKENS: dict[tuple[str, str], str] = {
    ("NSE", "RELIANCE"): "2885",
    ("NSE", "TCS"): "11536",
    ("NSE", "INFY"): "1594",
    ("NSE", "HDFCBANK"): "1333",
    ("NSE", "ICICIBANK"): "4963",
    ("NSE", "SBIN"): "3045",
    ("NSE", "BHEL"): "438",
    ("NSE", "ITC"): "1660",
    ("NSE", "LT"): "11483",
    ("NSE", "WIPRO"): "3787",
    ("NSE", "TEGA"): "7570",
}


def generate_totp(secret: str, *, for_time: float | None = None, step: int = 30, digits: int = 6) -> str:
    """Generate a standard RFC 6238 TOTP code from a Base32 secret."""
    normalized = secret.strip().replace(" ", "").upper()
    if not normalized:
        raise ProviderError("ANGEL_TOTP_SECRET is empty.")
    padding = "=" * ((8 - len(normalized) % 8) % 8)
    try:
        key = base64.b32decode(normalized + padding, casefold=True)
    except Exception as exc:
        raise ProviderError("ANGEL_TOTP_SECRET is not valid Base32.") from exc
    counter = int((time.time() if for_time is None else for_time) // step)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code_int % (10**digits)).zfill(digits)


@dataclass(frozen=True)
class AngelRestConfig:
    api_key: str
    client_code: str
    pin: str
    totp_secret: str
    base_url: str = DEFAULT_ANGEL_BASE_URL
    stale_after_seconds: int = 300
    timeout_seconds: float = 10.0

    @classmethod
    def missing_env_vars(cls) -> list[str]:
        required = (
            "ANGEL_API_KEY",
            "ANGEL_CLIENT_CODE",
            "ANGEL_PIN",
            "ANGEL_TOTP_SECRET",
        )
        return [key for key in required if not os.environ.get(key, "").strip()]

    @classmethod
    def is_configured(cls) -> bool:
        return len(cls.missing_env_vars()) == 0

    @classmethod
    def from_env(cls) -> "AngelRestConfig | None":
        if not cls.is_configured():
            return None
        try:
            stale_after = max(
                30,
                int(
                    os.environ.get(
                        "ANGEL_STALE_AFTER_SECONDS",
                        os.environ.get("YFINANCE_STALE_AFTER_SECONDS", "300"),
                    )
                ),
            )
        except ValueError:
            stale_after = 300
        return cls(
            api_key=os.environ["ANGEL_API_KEY"].strip(),
            client_code=os.environ["ANGEL_CLIENT_CODE"].strip(),
            pin=os.environ["ANGEL_PIN"].strip(),
            totp_secret=os.environ["ANGEL_TOTP_SECRET"].strip(),
            base_url=os.environ.get("ANGEL_BASE_URL", DEFAULT_ANGEL_BASE_URL).strip().rstrip("/")
            or DEFAULT_ANGEL_BASE_URL,
            stale_after_seconds=stale_after,
        )


def resolve_angel_symbol_token(
    exchange: str,
    symbol: str,
    extra_tokens: dict[tuple[str, str], str] | None = None,
) -> str:
    """Resolve an Angel One SmartAPI symboltoken for (exchange, symbol)."""
    key = (exchange.strip().upper(), symbol.strip().upper())
    if extra_tokens and key in extra_tokens:
        return extra_tokens[key]

    raw_json = os.environ.get("ANGEL_TOKEN_MAP_JSON", "").strip()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                lookup_key = f"{key[0]}:{key[1]}"
                val = parsed.get(lookup_key) or parsed.get(key[1])
                if isinstance(val, (str, int)) and str(val).strip():
                    return str(val).strip()
        except Exception:
            logger.warning("Ignoring invalid ANGEL_TOKEN_MAP_JSON value.")

    token = ANGEL_SYMBOL_TOKENS.get(key)
    if not token:
        raise ProviderError(
            f"No Angel One symboltoken mapping configured for {key[0]}:{key[1]}."
        )
    return token


def _parse_angel_timestamp(raw: Any, now: datetime) -> datetime | None:
    """Parse Angel SmartAPI exchFeedTime / ltt timestamps into timezone-aware UTC."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.replace(tzinfo=INDIA_TZ).astimezone(timezone.utc) if raw.tzinfo is None else raw.astimezone(timezone.utc)
    text = str(raw).strip()
    if not text:
        return None
    # ISO-8601
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.replace(tzinfo=INDIA_TZ).astimezone(timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except ValueError:
        pass
    # Common Angel SmartAPI format: "23-Nov-2026 14:25:10" or "23-11-2026 14:25:10"
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(text, fmt).replace(tzinfo=INDIA_TZ)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _default_http_post(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = Request(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except HTTPError as exc:
        if exc.code == 429:
            raise RateLimitError(
                f"Angel One REST rate limited request ({exc.code}).",
                retry_after_seconds=30.0,
            ) from exc
        raise ProviderError(f"Angel One REST HTTP {exc.code} error.") from exc
    except URLError as exc:
        raise ProviderError(f"Angel One REST network error: {exc.reason}") from exc
    except Exception as exc:
        raise ProviderError(f"Angel One REST transport failure: {exc}") from exc

    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderError("Angel One REST returned non-JSON response.") from exc
    if not isinstance(decoded, dict):
        raise ProviderError("Angel One REST returned unexpected payload type.")
    return decoded


class AngelRestQuoteProvider:
    """Quotes-only Angel One SmartAPI REST adapter."""

    name = "angel_rest"
    is_simulated = False
    quote_type = "provider_quote"

    def __init__(
        self,
        config: AngelRestConfig | None = None,
        *,
        http_post: Callable[[str, dict[str, Any], dict[str, str], float], dict[str, Any]] | None = None,
        totp_generator: Callable[[str], str] | None = None,
        clock: Callable[[], datetime] | None = None,
        token_map: dict[tuple[str, str], str] | None = None,
    ) -> None:
        self._config = config if config is not None else AngelRestConfig.from_env()
        self._http_post = http_post or _default_http_post
        self._totp_generator = totp_generator or generate_totp
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._token_map = dict(token_map or {})
        self._auth_lock = RLock()
        self._jwt_token: str | None = None
        self._jwt_obtained_at: datetime | None = None

    @property
    def is_configured(self) -> bool:
        return self._config is not None

    @property
    def stale_after_seconds(self) -> int:
        return self._config.stale_after_seconds if self._config is not None else 300

    def _base_headers(self, jwt_token: str | None = None) -> dict[str, str]:
        assert self._config is not None
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": "127.0.0.1",
            "X-ClientPublicIP": "127.0.0.1",
            "X-MACAddress": "00:00:00:00:00:00",
            "X-PrivateKey": self._config.api_key,
        }
        if jwt_token:
            headers["Authorization"] = f"Bearer {jwt_token}"
        return headers

    def _ensure_session_token(self, *, force_refresh: bool = False) -> str:
        if self._config is None:
            raise ProviderError("Angel One REST backup is not configured.")
        with self._auth_lock:
            now = self._clock().astimezone(timezone.utc)
            if (
                not force_refresh
                and self._jwt_token
                and self._jwt_obtained_at is not None
                and (now - self._jwt_obtained_at) < timedelta(hours=6)
            ):
                return self._jwt_token

            totp_code = self._totp_generator(self._config.totp_secret)
            login_url = f"{self._config.base_url}{ANGEL_LOGIN_PATH}"
            response = self._http_post(
                login_url,
                {
                    "clientcode": self._config.client_code,
                    "password": self._config.pin,
                    "totp": totp_code,
                },
                self._base_headers(),
                self._config.timeout_seconds,
            )
            if not response.get("status"):
                msg = str(response.get("message") or "Angel One login rejected")
                raise ProviderError(f"Angel One REST login failed: {msg}")
            data = response.get("data")
            jwt_token = data.get("jwtToken") if isinstance(data, dict) else None
            if not isinstance(jwt_token, str) or not jwt_token.strip():
                raise ProviderError("Angel One REST login response missing jwtToken.")
            cleaned = jwt_token.strip()
            if cleaned.lower().startswith("bearer "):
                cleaned = cleaned[7:].strip()
            self._jwt_token = cleaned
            self._jwt_obtained_at = now
            return cleaned

    def fetch_quote(self, exchange: str, symbol: str) -> ProviderQuote:
        """Fetch a single quote via Angel One SmartAPI REST market quote endpoint (quotes only)."""
        if self._config is None:
            raise ProviderError("Angel One REST backup is not configured.")

        exch = exchange.strip().upper()
        sym = symbol.strip().upper()
        if exch not in {"NSE", "BSE"}:
            raise ProviderError(f"Unsupported exchange for Angel One REST: {exch}")

        token = resolve_angel_symbol_token(exch, sym, self._token_map)
        jwt_token = self._ensure_session_token()
        quote_url = f"{self._config.base_url}{ANGEL_QUOTE_PATH}"
        payload = {
            "mode": "FULL",
            "exchangeTokens": {exch: [token]},
        }

        response = self._http_post(
            quote_url,
            payload,
            self._base_headers(jwt_token),
            self._config.timeout_seconds,
        )
        # Retry once on expired JWT token
        if not response.get("status") and str(response.get("errorcode") or "").upper() in {"AG8001", "AG8002"}:
            jwt_token = self._ensure_session_token(force_refresh=True)
            response = self._http_post(
                quote_url,
                payload,
                self._base_headers(jwt_token),
                self._config.timeout_seconds,
            )

        if not response.get("status"):
            msg = str(response.get("message") or "Angel One quote request failed")
            raise ProviderError(f"Angel One REST quote failed for {exch}:{sym}: {msg}")

        data = response.get("data")
        fetched = data.get("fetched") if isinstance(data, dict) else None
        if not isinstance(fetched, list) or not fetched:
            raise ProviderError(f"Angel One REST returned no quote for {exch}:{sym}.")

        item = fetched[0]
        if not isinstance(item, dict):
            raise ProviderError(f"Angel One REST returned malformed quote item for {exch}:{sym}.")

        raw_ltp = item.get("ltp")
        if raw_ltp is None:
            raise ProviderError(f"Angel One REST returned no valid ltp for {exch}:{sym}.")
        try:
            price = Decimal(str(raw_ltp))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ProviderError(f"Angel One REST returned invalid price for {exch}:{sym}.") from exc
        if not price.is_finite() or price <= 0:
            raise ProviderError(f"Angel One REST returned non-positive price for {exch}:{sym}.")

        now = self._clock().astimezone(timezone.utc)
        quoted_at = _parse_angel_timestamp(
            item.get("exchFeedTime") or item.get("exchTradeTime") or item.get("ltt"),
            now,
        )
        if quoted_at is not None and quoted_at > now + timedelta(seconds=5):
            raise ProviderError(f"Angel One REST returned a future timestamp for {exch}:{sym}.")

        return ProviderQuote(
            exchange=exch,
            symbol=sym,
            price=price,
            currency="INR",
            provider=ANGEL_PROVIDER_LABEL,
            quoted_at=quoted_at,
            is_simulated=False,
            quote_type=self.quote_type,
            freshness_seconds=self._config.stale_after_seconds,
        )
