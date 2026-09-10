# Safety Architecture Audit (2026-09)

Scope: the request in [Safety Architecture Audit / Hardening 指示書] — audit
the existing single-portfolio trading pipeline (`services/pipeline.py` and
everything it depends on) for software-engineering safety before any real
capital is at risk. Per that instruction's own §0/§33, **this document is
the audit report only — no code has been changed yet.** P0 fixes begin
after this report is reviewed.

`services/capital_cells/` (the Capital Cell subsystem built in an earlier
session) is out of scope here: it is not wired into `services/pipeline.py`,
carries no capital, and cannot place an order. It is mentioned twice below
as prior art where relevant.

## 0. Headline finding

**This system cannot place a live order today.** `packages/broker_adapters/`
contains exactly two files: `base.py` (the interface) and `paper.py` (a
fully in-memory simulator with zero network calls — verified by grepping
`paper.py` for `requests`/`httpx`/`socket`/`http`: no matches). No live-
capable broker adapter exists anywhere in the repo. The entire class of
risk in §1 of the instruction ("誤ってProduction Brokerへ注文") is
therefore **structurally impossible in the current codebase**, not because
of a `LIVE_TRADING=false` flag, but because there is nothing to flip it to.

This does not make the audit moot — it changes what "P0" means. The real
P0 risk here is **that this fact stops being true without anyone deciding
it should**: the day someone adds a real broker adapter, every gap below
that assumes "nothing bad can happen because nothing real is connected"
stops being true at the same moment. §14 (Findings) is written with that
day in mind.

---

## 1. Current Architecture — actual trade flow

Traced file-to-file, not from the docs (docs/architecture.md's diagram is
larger than the wired reality — that gap itself is Finding F5):

```
Market Data (services/market_data/service.py, MockProvider)
  -> Data Integrity (services/data_validation/integrity.py) -- DataHealth gate, REAL
  -> Quant Scanner (services/quant/scanner.py)
  -> Decision AI (services/decision/*: Mock, or real Claude/OpenAI adapters)
  -> [SELL-on-unheld-symbol rejected here, services/pipeline.py:394-406]
  -> Skeptic AI (veto path, services/pipeline.py:415-424)
  -> Final Trade Thesis (services/decision/thesis.py)
  -> Loss Control / Stop Plan (services/loss_control/engine.py) -- BEFORE sizing
  -> Position Sizing (services/position_sizing/engine.py)
  -> Capital Allocation (services/capital_allocation/engine.py)
  -> OrderIntent constructed (services/pipeline.py:501-506)
  -> Independent Audit AI (services/decision/audit.py)
  -> Master Risk Controller (services/risk/master_controller.py) -- 16-check deterministic gate, HMAC-signs RiskApproval
  -> Immutable Approved Order Snapshot (packages/schemas/audit.py) -- hash-locked
  -> Execution Engine (services/execution/engine.py) -- verifies snapshot hash before building the broker request
  -> PaperBroker (packages/broker_adapters/paper.py) -- the only broker that exists
  -> Order State Machine (services/execution/state_machine.py)
  -> Reconciliation (services/reconciliation/engine.py) -- exists, NOT wired to run automatically (Finding F4)
  -> Ledger / Provenance / PDCA (packages/common/ledger.py, packages/common/provenance.py, services/pdca/*)
```

Everything above runs **synchronously, single-threaded, in one Python
process, per `run_session()` call.** Confirmed by grepping the entire repo
(`packages/`, `services/`, `apps/`, `scripts/`) for
`threading|asyncio|multiprocessing|async def|ThreadPoolExecutor`: zero
matches. `apps/api/main.py` is FastAPI but every route is a plain
(synchronous) `def`, and every route is `@app.get` — no route ever calls
`run_session()` or mutates pipeline state (verified: grep for
`@app\.(post|put|delete|patch)` in `apps/api/main.py` returns nothing).

This single fact reframes most of §17 ("Concurrency / Race Condition") in
the instruction: there is currently no concurrent access to shared mutable
state anywhere in this codebase, so races on Buying Power, position
sizing, etc. cannot happen today. It is an **implicit** property of the
architecture, not an **enforced** one — nothing stops a future change
(a scheduler, a second worker, a write endpoint) from breaking it silently.
See Finding F11.

---

## 2. Existing Safety Controls — already strong, do not rebuild

The instruction (§0, §34) is explicit: do not re-implement what already
works. This is a substantial list, and it matters for calibrating how much
work remains.

| Control | Where | Evidence |
|---|---|---|
| HMAC-signed, hash-bound RiskApproval | `services/risk/master_controller.py:123-129` | `intent_hash` covers symbol/side/qty/order_type/limit/stop/TIF — changing any execution field post-approval invalidates the signature (INV-17) |
| Hash-locked Approved Order Snapshot | `packages/schemas/audit.py`, `services/execution/engine.py:80-93` | Execution refuses to mint its own snapshot; requires one from the approval step, matched by hash (A4, INV-17/18) |
| Explicit Order State Machine, UNKNOWN never == FAILED | `services/execution/state_machine.py`, `services/execution/engine.py:113-119` | Broker timeout -> `OrderState.UNKNOWN` + `HALT_NEW_ENTRIES`, never resubmitted blindly |
| RISK_INCREASING vs RISK_REDUCING correctly separated | `packages/schemas/core.py:308` (`is_protective_exit`), `services/risk/master_controller.py:107-120` | Only one production call site sets it True (`services/pipeline.py:274`, paired with `side=SELL`) — a BUY can never masquerade as protective |
| MASTER STOP state table preserves exits | `services/risk/master_controller.py:107-120`, `docs/invariants.md` §"MASTER STOP Semantics" | HALT_NEW_ENTRIES/SAFE_EXIT still allow protective stop / risk-reducing SELL; only FULL_BROKER_DISCONNECT blocks everything |
| Deterministic Master Risk Controller, 16 checks | `services/risk/master_controller.py:130-270` | No AI in this path; every check independently re-verified even when an upstream engine already checked it (defense in depth, §10) |
| AI cannot reach the broker | AST-enforced test `tests/unit/test_invariants.py::test_ai_cannot_reach_broker_imports` | Walks `services/decision/*.py`'s imports, fails the build if any import name contains "broker" |
| Structured AI output, strict schema | `packages/schemas/core.py` (`StrictModel`, `extra="forbid"`) | Malformed Output: Reject is real for unknown fields / wrong types — see Finding F1 for what it misses |
| Cash-account settlement correctness | `services/pipeline.py:240-247` | Uses `broker.get_cash().settled_cash`, not `ledger.cash` — a prior defect here rejected 116 orders against unsettled funds in a 200-session run before the fix (documented in the code comment) |
| No self-promotion to LIVE | `services/alpha_factory/factory.py` | `AlphaJudge` only emits `PROMOTE_RECOMMENDED`; nothing in `services/pdca` or `services/alpha_factory` touches `RiskConfig` or `MasterRiskController` (grepped, zero hits) |
| Immutable Risk Config | `packages/common/risk_config.py` | Frozen dataclass, no runtime mutation API, `max_leverage > 1.0` rejected at construction |
| Data staleness actually gates entries | `services/data_validation/integrity.py`, wired at `services/risk/master_controller.py:241-242` | This one (unlike Finding F2's signal-age check) is genuinely live: `data_health is HALT_ENTRIES` blocks new entries |
| Prior security review, 10 defects found & fixed | `docs/invariants.md` SEC-1..SEC-10 | Demonstrates an audit -> reproduce -> fix -> regression-test loop already exists and works in this project's history |
| Existing chaos/failure test suite | `tests/chaos/test_failures.py` | Already covers: order timeout -> UNKNOWN -> halt, broker disconnect, partial fill tracking, duplicate submission, reconciliation mismatch -> halt, disconnect during reconciliation |
| No secrets in source | Repo-wide grep for key-shaped literals | None found; `packages/common/llm_client.py` reads credentials from env vars only, never logs them |
| No CI path to a real broker | `.github/workflows/test.yml` | Installs `pip install -e '.[dev,api,llm]'` and runs pytest; no broker credentials, no LIVE env vars; LLM tests inject fake clients |

---

## 3. Failure Mode Analysis (instruction §3)

Worked through the instruction's own list against this codebase:

| Failure mode | Status here |
|---|---|
| Duplicate order via retry | Structurally blocked in-process (`ExecutionEngine._submitted`, `PaperBroker._orders`) — **not durable across a restart**, see F3 |
| Scheduler/Worker double-run | N/A today — no scheduler or worker process exists (§1 above). Becomes relevant the moment one is added |
| Broker success / client timeout | Handled correctly: `BrokerTimeoutError` -> `UNKNOWN` + `HALT_NEW_ENTRIES`, requires explicit reconciliation (`resolve_unknown`), never auto-resubmits |
| Broker reject read as internal success | No code path does this — `ack.accepted is False` -> `OrderState.REJECTED`, position never touched |
| Stale signal acted on | **Not actually prevented** — the check exists but is fed a hardcoded constant; see F2 |
| Symbol/side mix-up | Guarded structurally: Audit AI checks Decision-vs-Order direction match (A3, INV-16); a SELL on an unheld symbol is rejected before an order is even built (`services/pipeline.py:394-406`) |
| Malformed AI output (NaN/Inf/negative) | Negative and non-numeric: rejected. **NaN**: rejected (constraint math). **Infinity**: **not rejected** on several fields, including the Decision AI's own forecast range — see F1 |

---

## 4. Findings (P0/P1/P2/P3, per instruction §33)

Each finding: evidence, failure scenario, severity, proposed fix, scope,
regression risk.

### P0 — required before any Live Trading, and worth fixing now for Paper too

#### F1. Infinity is accepted where NaN is rejected, on AI-facing and execution schemas

**Evidence.** `packages/schemas/core.py` uses `Field(gt=0)`/`Field(ge=0)`
throughout (`Bar`, `Quote`, `OrderIntent.qty`, `SizedProposal.notional`,
`BrokerFill.price`, etc.). Pydantic v2's `gt`/`ge` constraints reject NaN
(`nan > 0` is `False`, so the constraint fails) but **accept positive
infinity** (`inf > 0` is `True`). Reproduced directly:

```python
OrderIntent(..., qty=float('inf'), ...)   # constructs successfully
```

Worse, `DecisionOutput.expected_return_range: tuple[float, float]`
(`packages/schemas/core.py:136`) — the Decision AI's own forecast, straight
from LLM structured output — has **no Field constraint at all**. Reproduced:

```python
DecisionOutput(..., expected_return_range=(float('nan'), float('inf')), ...)
# constructs successfully; the `_range_ordered` model_validator's
# `lo > hi` check also silently passes NaN, since any comparison with
# NaN is False.
```

This directly contradicts the module's own docstring claim ("Malformed
Output: Reject", §28) and the instruction's explicit §9/§25 requirement.
No test anywhere in the suite exercises this (repo-wide grep for
`nan|NaN|inf'` in `tests/` turns up only unrelated substring matches like
"proveNANce" and "fiNAnce" — zero real NaN/Inf tests exist).

**Failure scenario.** A real LLM (or a malformed/adversarial response) emits
`expected_return_range: [NaN, Infinity]`, or an upstream calculation
(division by a near-zero volatility, a bad corporate-action adjustment)
produces an infinite `qty`/`notional`/`price` that reaches a schema
boundary. It passes validation and reaches Position Sizing / the Master
Risk Controller's notional-based checks, where arithmetic against
Infinity/NaN produces undefined comparisons that could pass a `<=` check
that should have failed (e.g. `inf <= cfg.max_position_pct * equity` is
`False`, so this particular check is likely safe — but the reverse-direction
consequences of an infinite number entering `equity`, `total_exposure_notional`, or a `%` calc have not been audited field-by-field, and should not need to be, given a cheap boundary fix exists).

**Severity: P0.** Named explicitly in the instruction's own required test
list (§25 "Invalid AI Output ... NaN, Infinity ... → Reject").

**Proposed fix.** Add `allow_inf_nan=False` to every relevant `Field(...)`
(pydantic v2 supports this per-field), and add an explicit
`Field(allow_inf_nan=False)` to `DecisionOutput.expected_return_range`'s
tuple elements (requires a small `model_validator` since pydantic's
`allow_inf_nan` doesn't apply inside a bare `tuple[float, float]` — needs
an explicit check: `if not (math.isfinite(lo) and math.isfinite(hi))`).
Mechanical, no behavior change for any currently-valid input.

**Scope.** `packages/schemas/core.py`, `packages/schemas/audit.py`,
`packages/schemas/monitor.py` (all inherit `StrictModel`); a few fields in
`packages/schemas/capital_cell.py` for consistency, though that module is
unwired.

**Regression risk.** Low. Purely additive validation; no existing test
constructs these schemas with NaN/Infinity (confirmed above), so nothing
currently-passing should break.

---

#### F2. `signal_age_sec` is a hardcoded constant — the "stale signal" risk check can never fire

**Evidence.** `services/risk/master_controller.py:257-259`:
```python
check("stale_order", is_exit or view.signal_age_sec <= cfg.stale_order_after_sec, ...)
```
`PortfolioRiskView.signal_age_sec` is fed `0.0` at **both** production
call sites: `services/pipeline.py:259` and `services/pipeline.py:660`
(`signal_age_sec=0.0`, literal). `0.0 <= cfg.stale_order_after_sec` (default
300.0) is always `True`. This check is fully unit-tested in isolation
(`tests/unit/test_security_review_regressions.py:158,179` — it tests the
`MasterRiskController.review()` function directly with a crafted
`PortfolioRiskView`) but **is never exercised end-to-end**, because nothing
in `services/pipeline.py` ever constructs a `PortfolioRiskView` with a
non-zero value. A classic "unit-tested, not integration-wired" gap.

**Nuance that matters for the fix.** `run_session(self, as_of: datetime)`
(`services/pipeline.py:344`) takes one `as_of` timestamp and threads it
through the entire candidate loop as `now` — Decision AI's `created_at`
and the resulting `OrderIntent.created_at` are stamped with the *same*
`now` (`services/pipeline.py:428`, `506`). In the current paper/replay
harness this means naively computing `now - proposal.created_at` would
still be `0.0` — there is no simulated processing latency to measure. This
check is only meaningful once the pipeline runs against a **real** clock
with real per-step wall-clock delay (LLM latency, network calls, retries)
between decision generation and order submission — i.e. once a live-mode
execution path exists. **The correct P0-scope fix is not "make the number
up" — it's to stop the check from silently no-op'ing, and to make its
current vacuity impossible to miss.**

**Failure scenario.** In a future live-mode deployment, a Decision AI
signal generated at T is, for whatever reason (LLM retry storm, a stuck
queue, a slow News Engine call), acted on at T+40 minutes, after the
market context has materially changed — and the system has zero
protection against this, despite `docs/invariants.md`, the risk-check
table, and this exact audit's own checklist all asserting the protection
exists.

**Severity: P0** (as a "must not silently ship broken to Live" issue,
even though it costs nothing in Paper today).

**Proposed fix, scoped for now (no live-mode work required):**
1. Stamp `OrderIntent`/decision provenance with the **real** wall-clock
   time (`packages/common/clock.utcnow()`, independent of the session's
   simulated `as_of`) at the moment each is created, so the data needed
   for a real computation exists.
2. Compute `signal_age_sec` from that real timestamp at both
   `_risk_view`/`_exit_risk_view` call sites instead of the literal `0.0`.
3. Add a regression test that freezes real time forward between decision
   and order-intent creation and asserts `stale_order` actually rejects
   past the configured threshold — the missing case in the existing
   `test_security_review_regressions.py` coverage.
4. Add a code comment (already partially present via `§10`'s "defense in
   depth" note) stating explicitly that this value is real-wall-clock,
   not simulation-clock, so nobody re-introduces `0.0` "to make tests
   deterministic."

**Scope.** `services/pipeline.py` (two call sites + wherever
`OrderIntent`/proposal creation happens), one new test.

**Regression risk.** Low-medium. Existing paper/replay tests use
`FrozenClock`, so real-wall-clock timestamps captured via
`packages.common.clock.utcnow()` would introduce non-determinism into
`signal_age_sec` specifically (though not into any other simulated value)
— needs a `Clock`-injectable timestamp source, not a bare `utcnow()` call,
to keep tests deterministic. This is the one fix in this report that
touches pipeline control flow rather than pure validation, so it should
land with its own focused review.

---

#### F3. Idempotency has no durability across a process crash/restart

**Evidence.** `ExecutionEngine._submitted: dict[str, RiskApprovedOrder]`
(`services/execution/engine.py:57`) and `PaperBroker._orders`
(`packages/broker_adapters/paper.py:65`) are both plain in-memory dicts.
`DuplicateClientOrderIdError` (`packages/broker_adapters/base.py:23-24`)
is a pure structural guard with no storage backend. There is no database
anywhere in the repo (`docs/database.md` describes a Postgres/SQLite design
the code admits, in its own words, is not implemented — "本実装では...
インメモリ/SQLite互換のリポジトリ層を提供" — and grepping for
`sqlite3|sqlalchemy|psycopg|CREATE TABLE` across the repo returns nothing).

**Failure scenario.** Process submits an order, crashes before the fill
confirmation is processed, restarts. `_submitted` and `_orders` are both
empty again. If retry logic (this system's own, or a human's) resubmits
the same logical decision with a regenerated `client_order_id` (which
`make_client_order_id` — `services/execution/engine.py:45-46` — does
produce fresh each call, including a random suffix), **nothing in this
codebase would recognize it as a duplicate.** The only backstop would be
whatever a real broker's own dedup does — exactly the single point of
failure the instruction's §5/§6 explicitly warns against relying on
alone. (For the current in-process `PaperBroker`, a restart also clears
the simulated broker's own state, so there is no backstop at all in
Paper mode today.)

**Severity: P0** (a Live Trading blocker; currently low-consequence in
Paper since `PaperBroker` state resetting on restart doesn't cost real
money, but the absence of any durable idempotency layer is exactly what
§6/§21 of the instruction require to exist before Live).

**Proposed fix.** Requires a durable store for at least: submitted
`client_order_id`s, the `RiskApproval`/`ApprovedOrderSnapshot` they're
bound to, and last-known order state. This is the one finding in this
report that cannot be "minimally" fixed without first deciding on a
persistence layer (SQLite is the natural low-risk choice given
`docs/database.md` already gestures at it and it needs zero new
infrastructure) — flagged as P0 but scoped as its own follow-up design,
not a same-day patch. **Recommend resolving F4 (below) first**, since a
persistence decision naturally covers both.

**Scope.** New module (e.g. `packages/common/durable_store.py`), touches
`ExecutionEngine.submit()` and `PaperBroker.submit_order()`.

**Regression risk.** Medium — this is the one change in the report large
enough to warrant its own P0 sub-plan (per instruction §28, "no large
refactor at once": persistence should land as multiple small commits —
schema, write path, read/recovery path, tests — not one change).

---

#### F4. `ReconciliationEngine.reconcile()` is never called automatically — "起動時＋定期＋異常時" is documented, not implemented

**Evidence.** `docs/architecture.md:44` states reconciliation runs
"起動時＋定期＋異常時" (at startup, periodically, and on anomaly). Grepping
every call site of `.reconcile()`: `scripts/run_paper_demo.py` and
`scripts/run_llm_paper_demo.py` call it **once, after all `--days` sessions
have already completed**, as a final summary step — not before trading
resumes. `scripts/run_dashboard.py` never calls it. No scheduler exists
(§1 above) to call it "periodically." The class itself
(`services/reconciliation/engine.py`) is correct and well-tested in
isolation (`tests/chaos/test_failures.py:80-97`) — this is a wiring gap,
not a logic gap.

**Failure scenario.** Process restarts after a crash mid-session (see F3).
Per instruction §21 ("Crash Recovery"), the correct sequence is: fetch
broker state -> fetch internal state -> reconcile -> confirm safe -> only
then resume normal trading. Today, nothing forces that sequence — a
restarted process would go straight into `run_session()` against
whatever internal state survived (likely none, given F3), with no
automatic check against the broker's actual position/cash/orders first.

**Severity: P0** for the same reason as F3 — required before Live, and
the fix is comparatively cheap since the reconciliation logic already
exists and is already tested.

**Proposed fix.** Add an explicit "startup reconciliation" step as the
*first* action of any long-running entry point (`scripts/run_dashboard.py`,
and any future live-mode runner), before the first `run_session()` call —
mirroring the "safety state確定後に通常運転へ戻す" sequence in §21. This is
a small, additive change: call the already-correct `reconcile()`, and if
`not report.consistent`, do not proceed past startup (the engine already
sets `HALT_NEW_ENTRIES` on mismatch — startup wiring just needs to respect
that state before calling `run_session()` at all, which it currently
would, since `MasterRiskController.review()` already checks `self.state`).

**Scope.** `scripts/run_dashboard.py` (and equivalent future entry
points); no changes needed to `ReconciliationEngine` itself.

**Regression risk.** Low. Purely additive at startup; existing scripts
that already call `reconcile()` post-session are unaffected.

---

### P1 — needed before Live, not urgent for continued Paper development

#### F5. No operator-facing Kill Switch

**Evidence.** `apps/api/main.py` is 100% `@app.get` — zero write endpoints
(grepped `@app\.(post|put|delete|patch)`: no matches; the file's own
docstring says "書き込み系エンドポイントは存在しない"). `MasterRiskController.
set_state()` (`services/risk/master_controller.py:104-105`) is only called,
in production code, from automatic fault paths: `services/execution/
engine.py:117,123,163` (timeout/disconnect) and `services/reconciliation/
engine.py:46,72` (mismatch/unreachable). Every other caller is a test.
There is no human-initiated "stop all new trading now" control anywhere
in the running system.

**Failure scenario.** An operator observes something wrong (bad news,
a suspected model regression, anything not caught by an automatic
check) and has no way to halt new entries without either editing code or
killing the whole process outright — which, per F3/F4, is itself an
unsafe operation without durable idempotency and startup reconciliation.

**Severity: P1** (P0 before Live; the current system has no write surface
at all, consistent with the "read-only V1" design, so this is a planned
gap rather than a regression — but it must close before Live per
instruction §12).

**Proposed fix.** One authenticated write endpoint or CLI command that
calls `risk_controller.set_state(RiskState.HALT_NEW_ENTRIES or SAFE_EXIT,
reason=...)`, logged with who/when/why, requiring a named operator
(mirroring the existing pattern in `Ledger.rebase_high_water_mark`, which
already requires a named human approver). No automatic re-enable (per
instruction §12 — "Kill Switch解除は明示的操作を基本とし、自動解除しない",
which the existing `rebase_high_water_mark` pattern already satisfies for
the *resume* side).

**Scope.** New endpoint/CLI in `apps/api/main.py` or a new
`scripts/kill_switch.py`; no changes to `MasterRiskController` itself
(the mechanism already exists — only the trigger surface is missing).

**Regression risk.** Low — purely additive; must be carefully scoped to
require auth once any real write surface is added, since this is also the
first write endpoint the system would ever have.

---

#### F6. No Circuit Breaker for consecutive failures — trips on the first occurrence, never on a rate

**Evidence.** Grepped `services/risk`, `services/supervisor`,
`services/execution` for `consecutive|circuit_breaker|fail_count`: no
matches outside unrelated hits. `BrokerTimeoutError`/`BrokerDisconnectedError`
correctly halt on the *first* occurrence (a good, conservative default —
see Existing Controls) — but there is no accumulator for repeated
non-fatal anomalies: rejection rate, abnormal slippage, repeated
(recovered) API errors. Instruction §22 asks for this explicitly.

**Severity: P1.**

**Proposed fix.** A small, config-driven counter (`RiskConfig` already
centralizes thresholds — this fits the same pattern) that trips
`HALT_NEW_ENTRIES` after N rejections or M abnormal-slippage fills within
a rolling window; reuses the existing `RiskState` machinery, so no new
state model is needed.

**Scope.** New small module, likely `services/risk/circuit_breaker.py`,
consulted from `services/execution/engine.py`'s fill/reject handling.

**Regression risk.** Low if implemented as an opt-in additional check
alongside the existing single-event triggers, not a replacement for them.

---

#### F7. No structured logging or durable audit trail — everything lives in memory and vanishes on exit

**Evidence.** Repo-wide grep for `logging.getLogger|structlog|import logging`:
zero matches. All output is `print()` in demo scripts. The system's audit
trail (`ProvenanceStore`, `PreTradeAuditLog`, the `Ledger`'s own entry log)
is real and thorough *while the process is running* — but it is pure
in-memory Python state (confirmed: no DB, see F3/F4), so a crash loses it
entirely, defeating the instruction's §23 "後から追跡可能に" requirement
the moment a process doesn't exit cleanly.

**Severity: P1** (compounds F3/F4 — the same persistence gap is the root
cause of all three).

**Proposed fix.** Once F3's persistence layer exists, the same store
should durably capture the fields §23 lists (decision_id, order ids,
symbol/side/qty, risk checks, rejection reasons, etc.) — this is largely
"persist what's already being computed," not new computation.

**Scope.** Depends on F3's persistence decision.

**Regression risk.** Low, contingent on F3.

---

#### F8. `broker_connected` RiskCheck is permanently green regardless of actual connectivity

**Evidence.** `services/pipeline.py:256,657`: `broker_connected=True` is a
literal at both `PortfolioRiskView` construction sites.
`services/risk/master_controller.py:230`: `check("broker_connected",
view.broker_connected)` therefore always passes. Real broker-disconnect
protection *does* exist — via `risk_controller.state ==
FULL_BROKER_DISCONNECT`, set by `services/execution/engine.py:123` and
checked separately in `_entry_allowed_by_state`/`_exit_allowed_by_state`
— so **orders are still correctly blocked during a real disconnect**. This
finding is about the audit trail being misleading, not about a live safety
hole: `decisions_log` would show `broker_connected: PASS` even during an
actual disconnect (the order would still be rejected, just for a
different, correct reason — `market_open`/state-gate failures, not this
check).

**Severity: P1** (audit-trail correctness, not an active safety gap —
downgraded from what it would be if this were the *only* protection).

**Proposed fix.** Either wire `broker_connected` to a real live query
(cheap: `risk_controller.state is not RiskState.FULL_BROKER_DISCONNECT`)
or remove the field and rely solely on the state-gate, whichever the team
prefers — recommend the former since it costs one line and keeps the
`decisions_log` accurate for post-hoc audit.

**Scope.** `services/pipeline.py`, 2 call sites.

**Regression risk.** Minimal.

---

### P2 — improvement, not blocking

#### F9. No persistence layer at all (root cause of F3/F4/F7)

Already covered above as the shared root cause; listed separately here
because it is itself the single highest-leverage fix in this report.
**Recommend this be the first P0 implementation task**, since F3, F4, and
F7 are all partially or fully resolved by the same underlying decision.

#### F10. No external alerting sink

**Evidence.** `services/supervisor/heartbeat.py`'s `HeartbeatRegistry` is
real and correctly computes `ServiceStatus` (including "silent service =
failed service" staleness logic) — but only exposes a queryable snapshot.
Grepped for `smtp|slack|webhook|pagerduty|alert`: no matches anywhere in
`services/`/`apps/`. Nothing pushes a notification to a human; someone
must be actively watching `/health`.

**Severity: P2** (P1 before Live — flagged P2 here because it requires an
external integration decision outside this codebase's control, unlike the
other findings).

**Proposed fix.** Add a pluggable alert sink interface (a single method,
`send(severity, message)`) with a no-op default, wired to fire on
`HeartbeatRegistry.overall() == ERROR` and on `RiskState` transitions into
`HALT_NEW_ENTRIES`/`SAFE_EXIT`/`FULL_BROKER_DISCONNECT`. Actual
integration (Slack, email, PagerDuty) is a deployment-time config choice,
not a code change.

**Scope.** New small interface in `services/supervisor/`.

**Regression risk.** None if additive with a no-op default.

---

### P3 — forward-looking, document now rather than fix now

#### F11. Single-threaded safety is implicit, not enforced or documented as an invariant

Covered in §1 above. Not an active gap (nothing concurrent exists to
race), but nothing currently documents this as a *load-bearing*
architectural assumption. Recommend adding one line to
`docs/invariants.md` or a new `CLAUDE.md` (see F13) stating explicitly:
"this pipeline assumes single-threaded, single-process execution; adding
concurrency (a scheduler, multiple workers, or write-capable API routes)
requires re-auditing Buying Power reservation and Position Sizing for
races before merging." Costs nothing, prevents a future silent regression.

Note: `services/capital_cells/reservation.py` (unwired) already
implements exactly the atomic-reservation pattern this would need if
concurrency is ever introduced — worth reusing rather than re-designing
from scratch when that day comes.

#### F12. RiskConfig doesn't reject NaN/Infinity in its own fields

Lower severity than F1: `RiskConfig` loads from a human-edited file, not
network/AI input. Worth the same one-line-per-field fix as F1 for
consistency, but not urgent.

#### F13. No `CLAUDE.md`/`AGENTS.md` exists yet — RESOLVED

Confirmed via `find . -maxdepth 2 -iname CLAUDE.md -o -iname AGENTS.md`:
no matches at audit time (`docs/agents.md` exists but describes AI agent
*interfaces*, not development conventions). Instruction §36 asked for
common Safety Rules to persist beyond this one audit.

A separate "Architecture / Code Quality Rules" instruction was supplied
after this audit and has been persisted as `CLAUDE.md` at the repo root
(layering, dependency direction, domain boundaries mapped onto this
project's actual `services/*`/`packages/*` structure, naming, and a
pre-/post-implementation reporting checklist for large changes). It
cross-references `docs/architecture.md`'s Service Boundary table as the
canonical domain list rather than duplicating it.

Still open, and left as forward-looking guidance inside `CLAUDE.md` itself
rather than a separate action item: the LIVE_TRADING gate design (once a
real broker adapter is planned) and the single-threaded assumption (F11)
should get their own explicit entries once those are actually acted on —
`CLAUDE.md` as written covers general architecture discipline, not yet
those two safety-specific conventions.

---

## 5. Test Gaps (instruction §25 checklist, cross-referenced)

| Required test | Status |
|---|---|
| Duplicate signal/order intent -> single Broker order | Covered (`test_duplicate_submission_structurally_blocked`) |
| Timeout -> no blind resubmit -> Broker query | Covered (`test_order_timeout_goes_unknown_and_halts`) |
| Buying Power exceeded -> Reject | Covered (`test_insufficient_settled_cash_is_rejected`) |
| Concurrent Buying Power -> no double-spend | N/A today (F11 — no concurrency exists); should gain a test the moment any concurrency is introduced |
| Partial Fill -> correct remaining qty | Covered (`test_partial_fill_tracked`) |
| Invalid AI output (NaN/Inf/negative) -> Reject | **Missing — and would fail today for Infinity (F1)** |
| Stale market data -> reject risk-increasing | Covered for market data (`DataHealth`); **missing/dead for signal age specifically (F2)** |
| Reconciliation mismatch -> halt | Covered (`test_reconciliation_mismatch_halts_entries`) |
| Kill Switch ON -> reject risk-increasing | Covered at the state-machine level (`test_master_stop_allows_protective_exit` and friends); **no test exists for a human-triggered kill switch, because no trigger surface exists (F5)** |
| Reduce-Only still processes under Kill Switch | Covered (same MASTER STOP test suite) |
| Crash recovery -> no duplicate on restart | **Missing — and would fail today (F3)** |
| Duplicate webhook -> no double-update | N/A — no webhook receiver exists in this architecture (broker interaction is poll-based, not push-based); not a gap, a different design |
| Broker Reject -> no position increase | Covered structurally (`OrderState.REJECTED` path never touches the ledger) |
| Unknown order state -> halt new risk | Covered (`test_order_timeout_goes_unknown_and_halts`) |

---

## 6. Recommended order of work

Per instruction §28 (no large refactor at once) and §0 (small units,
P0 first):

1. **F1** (NaN/Infinity schema hardening) — smallest, purely additive,
   zero design decisions, do first.
2. **F9/F3/F4/F7** (persistence layer, then durable idempotency, then
   startup reconciliation, then durable audit trail) — the one real
   design decision in this report; land as separate small commits per
   the instruction's own process (§27: reproduce -> RCA -> regression
   test -> minimal fix -> ... for each).
3. **F2** (signal age) — do after F1 lands (shares the "reject bad
   numbers" mindset) and ideally after the persistence layer exists
   (decision timestamps should probably be durable too).
4. **F5** (Kill Switch), **F8** (broker_connected audit-trail fix) —
   independent, small, can land in either order relative to the above.
5. **F6** (circuit breaker), **F10** (alerting) — P1/P2, no urgency
   relative to continued Paper development.
6. **F11/F12/F13** — documentation-only; cheap, do whenever convenient,
   ideally before F9 so the persistence design is written against a
   documented single-threaded assumption rather than an implicit one.

No P0 finding in this report requires touching
`services/risk/master_controller.py`'s core check logic, the Order State
Machine, the HMAC signing, or the Reconciliation comparison logic itself
— all of that is already correct. Every P0 fix here is either a schema
boundary (F1), a wiring gap (F2, F4, F8), or a missing persistence layer
(F3/F7/F9). This matches the instruction's own expectation (§0: "既に安全
に実装されている部分は作り直さないでください") — the core safety design is
sound; what's missing is durability and a few dead/unwired checks.

---

**Live Trading readiness (instruction §35): not ready.** F1, F2, F3, F4
are unresolved P0s. This audit report itself does not change that
determination — no code has been modified.
