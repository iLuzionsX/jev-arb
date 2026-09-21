# JEV-ARB

JEV-ARB is a Phase 1 research system for comparing a deterministic crypto-arbitrage baseline with a Jev-assisted gating strategy against the same public market-data opportunity stream.

**PAPER TRADING ONLY — NO REAL ORDERS**

The codebase has no authenticated exchange client, no exchange secret-key configuration, no submit-order function, no withdrawal function, and no live-trading mode. It consumes public WebSocket order books, creates immutable candidate records, and simulates fills against later book state.

## What is implemented

- Coinbase Advanced Trade and Kraken WebSocket L2 adapters; Binance Spot diff-depth adapter is available but disabled unless explicitly enabled.
- Local normalized order books with executable depth/VWAP calculations.
- Configurable USD/USDC/USDT conversion assumptions recorded in the run configuration.
- $25,000 default paper portfolio with per-venue balances and no automatic replenishment.
- Deterministic baseline and Jev strategy receiving the same candidate object.
- Realistic delayed paper fills, full/partial/missed/one-leg outcomes, and separate rebalancing-cost estimates.
- SQLite persistence, JSON/CSV export, replay, a synthetic fixture, and a responsive dashboard.
- Fail-closed Jev integration using TypeSafe's official typed System One HTTP endpoint.

## Quick start

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
cp .env.example .env

# Offline deterministic smoke run with synthetic order-book data.
python -m jev_arb.cli demo --db data/demo.db

# Replay the same fixture.
python -m jev_arb.cli replay --input fixtures/sample_session.jsonl --db data/replay.db

# Public read-only live session. The dashboard is at http://127.0.0.1:8000.
python -m jev_arb.cli live --duration-seconds 60 --db data/live.db

# Serve a completed database without starting exchange connections.
python -m jev_arb.cli serve --db data/live.db --port 8000
```

Jev is optional for local testing. Set `TYPESAFE_API_KEY` to call Jev. Without it, Strategy B records a fail-closed `SKIP` decision; this is intentionally not treated as evidence that Jev is better or worse.

## Architecture

```text
public WebSocket feeds
        ↓
venue adapters → normalized local books → immutable candidate detector
                                           ├─ Strategy A: deterministic gates
                                           └─ Strategy B: typed Jev gate
                                                        ↓
                               delayed paper execution simulator
                                                        ↓
                              SQLite events, inventory, analytics
                                                        ↓
                                      local research dashboard
```

Both strategies receive the same candidate JSON. Each strategy owns a separate simulated inventory ledger. A skipped opportunity still receives a counterfactual paper-fill record so the analysis can count profitable and losing opportunities that were rejected.

## Jev boundary

JEV-ARB uses the current official TypeSafe contract:

```text
POST https://api.typesafe.ai/v1/systemone
Authorization: Bearer $TYPESAFE_API_KEY
```

The state is structured candidate data. The questions are typed Choice/Score/Noul questions. Jev never calculates prices, fees, VWAP, balances, or P&L, and no arbitrary text is parsed into an execution decision.

## Evidence commands

```bash
python -m unittest discover -s tests -v
python -m jev_arb.cli report --db data/demo.db
python -m jev_arb.cli export --db data/demo.db --out data/exports
```

See `docs/architecture.md` and `docs/evidence-report.md` for the schema, assumptions, and the exact meaning of the validation results. Live output is not evidence of an edge until enough observations exist and the paired result is statistically meaningful.


