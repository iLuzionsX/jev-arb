# Phase 1 evidence report

The command below produces the measured report for a particular run:

```bash
python -m jev_arb.cli report --db data/live.db
```

## Local validation recorded on 2026-09-21

### Synthetic replay fixture

- 4 immutable candidate opportunities.
- 10 persisted order-book snapshots.
- 0.400 second replay window.
- $25,000 configured paper capital; $1,000 fixture trade notional.
- Strategy A realized simulated P&L: **$7.1393**.
- Strategy B fixture-Jev realized simulated P&L: **$7.1393**.
- Fixture-Jev incremental P&L: **$0.0000**.
- Paired observations: **4**.
- Approximate statistical conclusion: **insufficient observations**.
- This run uses `ReplayJevProvider`; it is a deterministic harness, not evidence about the live Jev service.

### Public live-feed attempt

- 25.002 seconds with Coinbase and Kraken enabled.
- 0 normalized books recorded and 0 candidate opportunities.
- Coinbase connection attempts returned HTTP 502 in this managed runtime.
- Kraken connection attempts timed out during the opening handshake.
- Therefore baseline and Jev live P&L are both **$0.00**, but this is a connectivity result, not a market result and not evidence that arbitrage is absent.

The runtime also exposes reconnect counts, last errors, book age, and sequence-gap state in the database/dashboard. A live session should be rerun from an environment with outbound WebSocket access before drawing any market conclusion.

## Required report fields

Every report must state:

- observation duration and number of candidates;
- simulated capital and quote-conversion assumptions;
- baseline and Jev realized P&L;
- Jev incremental P&L and measured latency;
- confidence buckets and paired statistical test;
- whether there is enough data to conclude anything.

The default synthetic fixture is not live-market evidence. A live run with no configured Jev key is also not a Jev comparison: it is only a baseline/connectivity run because Strategy B correctly fails closed.

