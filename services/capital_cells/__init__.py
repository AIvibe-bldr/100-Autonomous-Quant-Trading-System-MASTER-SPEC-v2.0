"""Capital Cell foundation (docs/capital_cell_architecture.md, §41 priority 1-7).

Proposed, additive extension — NOT wired into services.pipeline.
TradingPipeline / the single Master Ledger / Master Risk Controller are
unchanged; this package only adds the pieces the spec's own priority order
puts first: Cell Schema, per-cell virtual ledgers (with cell-level no-short),
Master↔Cell reconciliation, Capital Reservation, Internal Netting (+ Internal
Transfer Price, gross-intent preservation, virtual/actual P&L separation),
and deterministic Fill Allocation. See docs/capital_cell_architecture.md for
the full 42-section spec and the consistency review against the current
single-portfolio architecture.
"""
