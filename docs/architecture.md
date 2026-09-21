# JEV-ARB Phase 1 architecture

## Experiment rules

1. A candidate is generated once from normalized order-book state and immutable thereafter.
2. Strategy A and Strategy B see the same candidate JSON.
3. The baseline uses deterministic configurable gates.
4. The Jev strategy applies the same non-negotiable safety checks and lets Jev gate the remaining opportunity.
5. A paper fill uses order-book state at its simulated arrival time, not detection-time ticker prices.
6. Every decision and counterfactual result is persisted.

## Data retention

The live recorder stores bounded top-of-book depth snapshots at `JEV_ARB_BOOK_RECORD_MS` intervals. It does not retain every raw depth message forever. Candidate, decision, trade, fill, health, configuration, and inventory records are retained so a run can be audited. SQLite is appropriate for the initial single-process prototype; PostgreSQL can replace the persistence adapter later.

## Safety boundary

The repository contains public market-data URLs only. There are no authenticated exchange URLs or order/withdrawal methods. The only credential accepted is `TYPESAFE_API_KEY` for the Jev decision service. `PAPER_ONLY` and the safety marker are asserted at runtime and persisted in every run.

## Future phase boundary

Live execution is intentionally not an extension of this phase's code. A future phase would require a separate reviewed package, explicit authenticated read-only/trading permissions, a new risk and reconciliation design, and independent safety review.


