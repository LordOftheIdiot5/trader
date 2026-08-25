"""The process that runs on the VPS.

Builds the venues, runs whichever strategy was asked for, and publishes state
for the dashboard. With --strategy none it is a heartbeat: it proves the engine
is alive, the venues are reachable, and the dashboard has fresh data.

    .venv/bin/python scripts/run.py --once                     # one tick
    .venv/bin/python scripts/run.py --interval 300             # every 5 min
    .venv/bin/python scripts/run.py --interval 300 --strategy crossover --push

--push commits the published state back to the repo so GitHub Pages serves it.
Without it the state is written locally only.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from trader import config as config_module
from trader import venues as venues_module
from trader.engine import Engine
from trader.journal import Journal
from trader.risk import KillSwitch, RiskGuard
from trader.strategies.crossover import Crossover
from trader.strategy import Context, PriceHistory


def publish(engine: Engine, destination: Path, push: bool) -> None:
    snapshot = engine.snapshot()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")

    if not push:
        return
    root = destination.parent.parent.parent
    try:
        subprocess.run(["git", "add", str(destination)], cwd=root, check=True,
                       capture_output=True)
        # Nothing to commit is the normal case on a quiet tick, not a failure.
        staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=root)
        if staged.returncode != 0:
            subprocess.run(
                ["git", "commit", "-m", "Publish trader state"],
                cwd=root, check=True, capture_output=True,
            )
            subprocess.run(["git", "push"], cwd=root, check=True, capture_output=True)
    except subprocess.CalledProcessError as error:
        # A failed publish must never stop trading, and must never be silent.
        detail = (error.stderr or b"").decode(errors="replace").strip()
        print(f"publish failed (continuing): {detail}", file=sys.stderr)


def collect_prices(venue, symbols, history) -> dict[str, float]:
    """Quote every symbol, tolerating individual failures.

    One unlisted or temporarily unquotable symbol must not blind the strategy
    to the rest of the book, so failures are skipped rather than raised.
    """
    prices: dict[str, float] = {}
    for symbol in symbols:
        try:
            price = venue.last_price(symbol)
        except Exception as error:
            print(f"no price for {symbol}: {error}", file=sys.stderr)
            continue
        prices[symbol] = price
        history.record(symbol, price)
    return prices


def tick(engine: Engine, config, strategy, history, push: bool) -> None:
    if engine.guard.kill_switch.engaged:
        # Still publish while halted: a dashboard that goes stale during a
        # halt is exactly when you most want to see the reason.
        print(f"HALTED: {engine.guard.kill_switch.reason()}")
        publish(engine, config.paths.publish, push)
        return

    if strategy is not None:
        venue_name = "paper" if not config.is_live else "crypto"
        venue = engine.venues[venue_name]
        prices = collect_prices(venue, config.symbols, history)
        context = Context(
            now=datetime.now(timezone.utc),
            prices=prices,
            positions=venue.positions(),
            cash=venue.cash(),
            venue=venue_name,
            history=history,
        )
        for intent in strategy.decide(context):
            # Every intent still goes through the gate. A strategy asking for
            # something oversized gets refused and journalled, not obeyed.
            engine.submit(intent)

    publish(engine, config.paths.publish, push)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="one tick, then exit")
    parser.add_argument("--interval", type=int, default=300, help="seconds between ticks")
    parser.add_argument("--push", action="store_true", help="git push published state")
    parser.add_argument(
        "--strategy",
        default="none",
        choices=("none", "crossover"),
        help="none (default) just publishes state; crossover runs the worked example",
    )
    args = parser.parse_args()

    load_dotenv()
    config = config_module.load()
    engine = Engine(
        venues=venues_module.build(config),
        guard=RiskGuard(config.limits, KillSwitch(config.paths.halt)),
        journal=Journal(config.paths.journal),
    )

    banner = "LIVE - real money" if config.is_live else "paper"
    print(f"trader up in {banner} mode, venues: {', '.join(sorted(engine.venues))}")
    print(f"halt with: touch {config.paths.halt}")

    strategy = None
    if args.strategy == "crossover":
        strategy = Crossover(config.symbols)
        print(f"strategy: {strategy.name} (a worked example, not a recommendation)")
    else:
        print("strategy: none - publishing state only")
    history = PriceHistory()

    if args.once:
        tick(engine, config, strategy, history, args.push)
        return 0

    while True:
        try:
            tick(engine, config, strategy, history, args.push)
        except Exception as error:
            # One bad tick must not take the process down; the next one may
            # succeed, and a dead process publishes nothing at all.
            print(f"tick failed: {error}", file=sys.stderr)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
