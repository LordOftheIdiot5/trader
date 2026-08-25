# trader.nordl.dev

Automated trading across stocks (Alpaca) and crypto (any ccxt exchange), with
a public dashboard and credentials that cannot move money off a venue.

Runs in paper mode by default. Nothing below places a real order until
`mode: live` is set in `config.yaml`, deliberately.

## Why it is split in two

The other `nordl.dev` projects are a single repo on GitHub Pages with an
Actions cron. That pattern cannot work here. `stockwatch` only reads; a trader
holds credentials, and Pages publishes everything in the repo. An API key in
the page or in `data/` is world-readable the moment it deploys.

So:

| Part | Where | Holds secrets |
|---|---|---|
| Engine, venue adapters, strategies | your VPS | yes, in `.env` |
| Dashboard at `trader.nordl.dev` | GitHub Pages | no, ever |

The VPS publishes a scrubbed `state.json`. The browser never sees a key.

## Trade but not withdraw

Three separate locks, in order of how much they actually protect you:

**1. The exchange key scope.** The strongest one, and it lives outside this
repo. Create the API key with trading enabled and withdrawals disabled, then
bind it to the VPS's static IP. Worst case if the key leaks: someone trades
your balance badly. They cannot move it out. See `.env.example` for the exact
per-exchange settings.

**2. The interface has no withdraw method.** `trader/adapters/base.py` defines
what a venue can be asked to do: quote, read positions, place an order. There
is no `withdraw`, no `transfer`. No strategy and no bug in this codebase can
express moving funds off a venue, because the vocabulary does not contain it.

**3. The risk gate.** Every order goes through `RiskGuard.check` before it can
reach a venue. Caps on order size, daily notional, open positions, plus a
symbol allowlist and a kill switch.

### If you later want on-chain DEX trading

A raw private key cannot be made trade-only. If a key can sign a Uniswap swap
it can sign `transfer(attacker, everything)` — there is no permission flag on
an EOA. The real answer is to put the funds where the bot key does not own
them: a **Safe** with a **Zodiac Roles Modifier**, where the bot key is a role
member permitted to call only specific selectors on specific contracts. Not
built here yet.

## The kill switch

```bash
touch var/HALT
```

The next order is refused, and every one after it. It is a file rather than a
flag because a process that has lost its mind cannot be trusted to honour its
own in-memory state, and because a human can trip it from any shell while the
bot is running. Nothing in the bot removes it — clearing it is a deliberate
act.

It also trips itself when the day's realised loss passes `daily_loss_limit`,
and that halt survives both the daily counter reset and a process restart.

## Setup

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # Windows
# .venv/bin/python -m pip install -r requirements.txt      # Linux VPS
cp .env.example .env    # then fill it in, read the comments first
```

Run the tests:

```bash
.venv/Scripts/python -m pytest tests/ -q
```

## Going from paper to live

Deliberately more than one switch, so it cannot happen by accident:

1. `mode: live` in `config.yaml`
2. `ALPACA_BASE_URL` to `https://api.alpaca.markets` in `.env`
3. Real exchange keys in `.env`, scoped as above

Do 1 without 2 and 3 and it will fail loudly rather than trade something
unexpected.

## What this is not

It has no strategy in it. The engine routes and constrains orders; deciding
*what* to trade is yours. It also does not model partial fills, queue
position, or market impact — a good paper result says the plumbing works, not
that the strategy does.
