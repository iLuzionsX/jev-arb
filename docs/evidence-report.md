# Phase 1 evidence report

This file is a report template. The command below produces the measured report for a particular run:

```bash
python -m jev_arb.cli report --db data/live.db
```

The report must state:

- observation duration and number of candidates;
- simulated capital and quote-conversion assumptions;
- baseline and Jev realized P&L;
- Jev incremental P&L and measured latency;
- confidence buckets and paired statistical test;
- whether there is enough data to conclude anything.

The default synthetic fixture is not live-market evidence. A live run with no configured Jev key is also not a Jev comparison: it is only a baseline/connectivity run because Strategy B correctly fails closed.


