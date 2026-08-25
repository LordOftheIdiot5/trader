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
from trader.state import material_fingerprint
from trader.strategy import Context, PriceHistory


# Push at least this often even when nothing changed, so a stale dashboard
# means "the engine is down" rather than "the market was quiet".
HEARTBEAT_SECONDS = 3600

_last = {"fingerprint": None, "pushed_at": 0.0}


def publish(engine: Engine, destination: Path, push: bool) -> None:
    snapshot = engine.snapshot()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")

    if not push:
        return

    fingerprint = material_fingerprint(snapshot)
    stale = time.time() - _last["pushed_at"] > HEARTBEAT_SECONDS
    if fingerprint == _last["fingerprint"] and not stale:
        return

    root = destination.parent.parent.parent
    try:
        subprocess.run(["git", "add", str(destination)], cwd=root, check=True,
                       capture_output=True)
        staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=root)
        if staged.returncode != 0:
            subprocess.run(
                ["git", "commit", "-m", "Publish trader state"],
                cwd=root, check=True, capture_output=True,
            )
            subprocess.run(["git", "push"], cwd=root, check=True, capture_output=True)
        _last["fingerprint"] = fingerprint
        _last["pushed_at"] = time.time()
    except subprocess.CalledProcessError as error:
        # A failed publish must never stop trading, and must never be silent.
        # The fingerprint is deliberately not updated, so the next tick retries.
        detail = (error.stderr or b"").decode(errors="replace").strip()
        print(f"publish failed (continuing): {detail}", file=sys.stderr)


def build_desk(config, journal):
    """Assemble the multi-agent desk from config. Imported lazily so the rest
    of the engine does not depend on the anthropic SDK being installed."""
    import anthropic

    from trader.desk.desk import DeskBudget, TradingDesk
    from trader.strategies.desk import DeskStrategy

    settings = config.desk
    desk = TradingDesk(
        client=anthropic.Anthropic(),
        model=settings.get("model", "claude-opus-5"),
        budget=DeskBudget(
            min_seconds_between_runs=float(settings.get("min_minutes_between_runs", 60)) * 60,
            max_runs_per_day=int(settings.get("max_runs_per_day", 24)),
        ),
        analyst_effort=settings.get("analyst_effort", "low"),
        chair_effort=settings.get("chair_effort", "high"),
    )
    return DeskStrategy(desk, symbols=config.symbols, journal=journal)


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


def tick(engine: Engine, config, strategy, history, history_path, push: bool) -> None:
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
        history.save(history_path)
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
        choices=("none", "crossover", "desk"),
        help="none (default) publishes state only; crossover is the worked "
             "example; desk convenes the multi-agent desk (needs ANTHROPIC_API_KEY)",
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
    elif args.strategy == "desk":
        strategy = build_desk(config, Journal(config.paths.journal))
        print(f"strategy: desk, model {config.desk.get('model', 'claude-opus-5')}, "
              f"at most {config.desk.get('max_runs_per_day', 24)} runs/day")
    else:
        print("strategy: none - publishing state only")
    history_path = config.paths.journal.parent / "history.json"
    history = PriceHistory.load(history_path)
    if len(history):
        print(f"history: restored {len(history)} symbols from {history_path.name}")

    if args.once:
        tick(engine, config, strategy, history, history_path, args.push)
        return 0

    while True:
        try:
            tick(engine, config, strategy, history, history_path, args.push)
        except Exception as error:
            # One bad tick must not take the process down; the next one may
            # succeed, and a dead process publishes nothing at all.
            print(f"tick failed: {error}", file=sys.stderr)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
