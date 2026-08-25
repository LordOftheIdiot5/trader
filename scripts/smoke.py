"""End-to-end proof that the plumbing works, against live prices, risking nothing.

Pulls a real price from a public exchange endpoint (no API key, no account),
runs it through the paper venue, the risk gate, the journal and the publisher.
If this passes, the only untested link between here and live trading is the
venue adapter itself.

    .venv/Scripts/python scripts/smoke.py        # Windows
    .venv/bin/python scripts/smoke.py            # Linux VPS
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ccxt

from trader import config as config_module
from trader.adapters.paper import PaperVenue
from trader.engine import Engine
from trader.journal import Journal
from trader.risk import KillSwitch, OrderIntent, RiskGuard

SYMBOL = "BTC/USD"


def live_prices(exchange_id: str = "kraken"):
    """A price source backed by a real exchange's public ticker.

    Public market data needs no credentials, so this runs on a clean machine.
    Prices are cached per call rather than per symbol because a smoke test
    should see the same market the engine does.
    """
    exchange = getattr(ccxt, exchange_id)()

    def source(symbol: str) -> float:
        ticker = exchange.fetch_ticker(symbol)
        price = ticker.get("last") or ticker.get("close")
        if not price:
            raise ValueError(f"{exchange_id} returned no last price for {symbol}")
        return float(price)

    return source


def main() -> int:
    config = config_module.load()
    if config.is_live:
        print("Refusing to smoke test in live mode. Set mode: paper.")
        return 1

    print(f"mode        : {config.mode}")
    print(f"allowlist   : {', '.join(config.symbols)}")

    source = live_prices()
    price = source(SYMBOL)
    print(f"live {SYMBOL}: {price:,.2f}")

    venue = PaperVenue(
        price_source=source,
        starting_cash=config.paper_starting_cash,
        slippage_bps=config.paper_slippage_bps,
        fee_bps=config.paper_fee_bps,
        quote_currency=config.quote_currency,
    )
    journal = Journal(config.paths.journal)
    guard = RiskGuard(limits=config.limits, kill_switch=KillSwitch(config.paths.halt))
    engine = Engine(venues={"paper": venue}, guard=guard, journal=journal)

    if guard.kill_switch.engaged:
        print(f"\nHALT file present ({config.paths.halt}). Remove it to trade.")

    # 1. An order inside the caps should fill.
    size = round((config.limits.max_order_notional * 0.5) / price, 8)
    fill = engine.submit(
        OrderIntent(SYMBOL, "buy", size, price, venue="paper", strategy="smoke")
    )
    print(f"\nbuy {size} {SYMBOL} -> {'filled @ %.2f' % fill.price if fill else 'REFUSED'}")

    # 2. An order over the per-order cap must be refused, not filled.
    oversized = round((config.limits.max_order_notional * 5) / price, 8)
    refused = engine.submit(
        OrderIntent(SYMBOL, "buy", oversized, price, venue="paper", strategy="smoke")
    )
    print(f"buy {oversized} {SYMBOL} -> {'FILLED (BUG)' if refused else 'refused, as intended'}")

    # 3. A symbol outside the allowlist must be refused whatever its price.
    stray = engine.submit(
        OrderIntent("DOGE/USD", "buy", 1, 0.1, venue="paper", strategy="smoke")
    )
    print(f"buy 1 DOGE/USD -> {'FILLED (BUG)' if stray else 'refused, not on allowlist'}")

    snapshot = engine.snapshot()
    config.paths.publish.parent.mkdir(parents=True, exist_ok=True)
    config.paths.publish.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")

    print(f"\nequity      : {venue.equity():,.2f} {config.quote_currency}")
    print(f"journal     : {config.paths.journal}")
    print(f"published   : {config.paths.publish}")

    if refused is not None or stray is not None:
        print("\nFAILED: the risk gate let something through.")
        return 1
    if fill is None:
        print("\nFAILED: a within-limits order was refused.")
        return 1
    print("\nOK: fills work, refusals work, state published.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
