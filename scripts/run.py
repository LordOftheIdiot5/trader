"""The process that runs on the VPS.

Builds the venues, publishes state for the dashboard, and — once you add one —
gives a strategy somewhere to run. There is no strategy in this repo, so as it
stands this is a heartbeat: it proves the engine is alive, the venues are
reachable, and the dashboard has fresh data.

    .venv/bin/python scripts/run.py --once            # one tick, then exit
    .venv/bin/python scripts/run.py --interval 300    # every 5 minutes
    .venv/bin/python scripts/run.py --interval 300 --push

--push commits the published state back to the repo so GitHub Pages serves it.
Without it the state is written locally only.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from trader import config as config_module
from trader import venues as venues_module
from trader.engine import Engine
from trader.journal import Journal
from trader.risk import KillSwitch, RiskGuard


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


def tick(engine: Engine, config, push: bool) -> None:
    if engine.guard.kill_switch.engaged:
        print(f"HALTED: {engine.guard.kill_switch.reason()}")
    else:
        # A strategy goes here. It builds OrderIntents and calls
        # engine.submit(); it must never touch a venue directly, because the
        # risk gate lives on this path and nowhere else.
        pass
    publish(engine, config.paths.publish, push)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="one tick, then exit")
    parser.add_argument("--interval", type=int, default=300, help="seconds between ticks")
    parser.add_argument("--push", action="store_true", help="git push published state")
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

    if args.once:
        tick(engine, config, args.push)
        return 0

    while True:
        try:
            tick(engine, config, args.push)
        except Exception as error:
            # One bad tick must not take the process down; the next one may
            # succeed, and a dead process publishes nothing at all.
            print(f"tick failed: {error}", file=sys.stderr)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
