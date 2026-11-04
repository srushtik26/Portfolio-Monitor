"""Fetch and persist one explicitly mapped Yahoo Finance demo candle."""

import argparse
import logging
import sys
from datetime import datetime, timezone

from .database import SessionLocal
from .prices import (
    ProviderError,
    YFinanceProvider,
    normalize_utc,
    record_quote,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch and persist a TEGA.NS candle without enabling alerts."
    )
    parser.add_argument("--exchange", choices=("NSE",), default="NSE")
    parser.add_argument("--symbol", choices=("TEGA",), default="TEGA")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    try:
        provider = YFinanceProvider()
        quote = provider.fetch_quote(args.exchange, args.symbol)
        with SessionLocal() as session:
            stored = record_quote(
                session,
                quote,
                now=datetime.now(timezone.utc),
                alerts_enabled=False,
                fixture_alerts_enabled=False,
            )
            session.commit()
        print(
            f"Saved {stored.exchange}:{stored.symbol} {stored.currency} {stored.price} "
            f"from {stored.provider}; 1-minute candle close "
            f"(Yahoo bar timestamp {normalize_utc(stored.quoted_at).isoformat()}, "
            f"status={stored.status}, "
            f"freshness threshold={provider.stale_after_seconds}s)."
        )
        print("No alert rules, notifications, or orders were activated.")
    except (ProviderError, ValueError) as error:
        print(f"Yahoo Finance smoke check failed; no success or fallback quote was recorded: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
