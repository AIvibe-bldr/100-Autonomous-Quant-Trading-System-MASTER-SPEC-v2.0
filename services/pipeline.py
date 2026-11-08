"""End-to-end trading pipeline (MASTER SPEC §6, §7, §107).

Wires: Universe → Scanner → Decision AI → Skeptic → Loss Control →
Position Sizing → Capital Allocation → Master Risk Controller →
Execution → Paper Broker → Ledger/Provenance.

The pipeline itself is deterministic glue.  Its result object always explains
NO TRADE outcomes with the funnel numbers (§93: 故障ではなく理由表示).
"""
from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from packages.common.clock import Clock
from packages.common.durable_store import DurableAuditStore
from packages.common.environment import Environment, require_same_environment
from packages.common.llm_client import LLMCallError
from packages.common.ledger import Ledger
from packages.common.provenance import ProvenanceStore
from packages.schemas.core import (
    Action,
    BrokerFill,
    DecisionAction,
    FinalTradeThesis,
    OrderIntent,
    OrderState,
    OrderType,
    ProposalSource,
    Quote,
    RiskApproval,
    RiskApprovedOrder,
    RiskRejection,
    SizedProposal,
    StopPlan,
    TimeInForce,
    TradeProposal,
)
from packages.schemas.audit import ApprovedOrderSnapshot, AuditVerdict
from services.supervisor.heartbeat_alerts import HeartbeatAlerter
from services.capital_allocation.engine import CapitalAllocationEngine
from services.cost_manager.engine import OperatingCostEngine
from services.data_validation.integrity import DataIntegrityEngine
from services.decision.audit import (
    AuditContext,
    AuditUnavailableError,
    IndependentAuditor,
)
from services.decision.models import (
    CalibrationTracker,
    DecisionContext,
    MalformedDecisionError,
    MockDecisionModel,
    MockSkepticModel,
    validate_decision,
    UntrustedText,
)
from services.decision.thesis import build_final_trade_thesis
from packages.broker_adapters.base import BrokerDisconnectedError
from services.execution.engine import ExecutionEngine, make_client_order_id
from services.pdca.audit_log import NearMissKind, PreTradeAuditLog, Stage
from services.decision.disagreement import (
    ModelDisagreementEngine,
    decision_lean,
    news_lean,
    quant_lean,
    regime_lean,
    skeptic_lean,
)
from services.institutional.engine import InstitutionalFlowEngine
from services.institutional.sources import (
    FlowUnavailable,
    OwnershipFilingsSource,
    describe_flow,
)
from services.pdca.observer import DecisionEvent, FillEvent, SessionEndEvent, SessionObserver
from services.pdca.post_trade import PostTradeTracker
from services.pdca.decision_quality import (
    DecisionKind,
    DecisionQualityEngine,
    DecisionSnapshot,
)
from services.loss_control.engine import LossControlEngine, NoStopPlanError
from services.market_data.service import MarketDataService, MarketDataUnavailable
from services.market_data.universe import UniverseManager
from services.market_data.event_calendar import EventCalendar, RiskEvent
from services.market_data.event_source import EventSource, EventsUnavailable
from services.news.engine import NewsEngine, NewsSignal
from services.news.selection import select_for_symbol
from services.news.source import NewsSource, NewsUnavailable
from services.position_sizing.correlation import max_abs_correlation_to_book
from services.position_sizing.engine import (
    PortfolioContext,
    PositionSizingEngine,
    SizingRejected,
)
from services.quant.scanner import QuantScanner
from services.regime.engine import (RegimeEngine, describe_universe_regime,
                                    universe_composite_bars)
from services.reconciliation.engine import ReconciliationEngine
from services.risk.gap_risk import gap_risk_score
from services.risk.master_controller import (
    MasterRiskController,
    PortfolioRiskView,
    RiskState,
    throttle_factor,
    throttle_level,
)


log = logging.getLogger("quant.pipeline")


@dataclass
class PipelineResult:
    """Session outcome incl. NO TRADE reasons (§93)."""

    scanned: int = 0
    candidates: int = 0
    decision_candidates: int = 0
    skeptic_vetoes: int = 0
    stop_planned: int = 0
    protective_stops_placed: int = 0
    stops_triggered: int = 0
    # §30: symbol -> size multiplier applied for model disagreement (1.0 = unanimous)
    disagreement_multipliers: dict[str, float] = field(default_factory=dict)
    sized: int = 0
    allocated: int = 0
    audit_passed: int = 0
    audit_rejected: int = 0
    audit_review: int = 0        # §8: queued for human review, never auto-sent
    risk_passed: int = 0
    risk_rejected: int = 0
    orders_filled: int = 0
    no_trade_reasons: list[str] = field(default_factory=list)
    # non-fatal data problems worth a human's attention (e.g. news fetch failed)
    data_warnings: list[str] = field(default_factory=list)
    fills: list[BrokerFill] = field(default_factory=list)

    @property
    def traded(self) -> bool:
        return self.orders_filled > 0


def _daily_returns(closes: list[float]) -> list[float]:
    return [b / a - 1.0 for a, b in zip(closes, closes[1:]) if a > 0]


@dataclass
class TradingPipeline:
    environment: Environment
    clock: Clock
    market_data: MarketDataService
    universe: UniverseManager
    scanner: QuantScanner
    integrity: DataIntegrityEngine
    decision_model: MockDecisionModel
    skeptic: MockSkepticModel
    calibration: CalibrationTracker
    loss_control: LossControlEngine
    sizing: PositionSizingEngine
    allocation: CapitalAllocationEngine
    risk_controller: MasterRiskController
    execution: ExecutionEngine
    ledger: Ledger
    provenance: ProvenanceStore
    # Required, and never defaulted: the previous default_factory produced
    # `IndependentAuditor(model=None, audit_all=False)` — the single most
    # permissive configuration possible — so any caller that forgot to pass an
    # auditor silently got "every order passes, audited by nobody".
    auditor: IndependentAuditor
    audit_log: PreTradeAuditLog = field(default_factory=PreTradeAuditLog)
    decision_quality: DecisionQualityEngine = field(default_factory=DecisionQualityEngine)
    post_trade: PostTradeTracker = field(default_factory=PostTradeTracker)
    # Optional (§80-83): when set, every fill's fee is also recorded here for
    # the cost breakdown/Data ROI picture. The fee already reduced trading_pnl
    # via the ledger the moment the fill landed — this is visibility, not a
    # second charge (see OperatingCostEngine.total() / trading_fees_total()).
    cost_engine: Optional[OperatingCostEngine] = None
    symbol_themes: dict[str, list[str]] = field(default_factory=dict)
    max_new_positions: int = 5
    # docs/SAFETY_AUDIT.md F2: a REAL wall clock, independent of `clock`
    # (which is the session's simulated `as_of` — FrozenClock in every
    # paper/replay run today, so it cannot measure real processing latency).
    # This is what `stale_order` (check 15, §47) measures signal age against.
    # Deliberately NOT `packages.common.clock.utcnow()` called inline: tests
    # need to inject a fake one (see test_security_review_regressions.py) to
    # assert staleness deterministically without a real sleep.
    wall_clock: Clock = field(default_factory=Clock)
    # A live quote is fetched some seconds AFTER the session's `as_of` was fixed,
    # so its own timestamp is slightly "later" than as_of. Up to this much is our
    # sampling lag, not a data anomaly; anything beyond it is still flagged.
    quote_sampling_lag: timedelta = timedelta(seconds=60)
    # News → Decision AI (MASTER SPEC §17-19). None = not connected: Decision is told
    # "news not available". The engine does dedup / reliability / injection screening.
    news_source: Optional[NewsSource] = None
    news_engine: NewsEngine = field(default_factory=NewsEngine)
    news_lookback: timedelta = timedelta(days=3)
    # Event Calendar → Decision AI and gap risk (§16, §40). None = not connected.
    event_source: Optional[EventSource] = None
    event_horizon_days: int = 7          # gap-risk window for scheduled events
    event_display_days: int = 14         # how far ahead Decision is shown events
    # F10: when set, checked at every session start (None = no heartbeat producers wired)
    heartbeat_alerter: Optional[HeartbeatAlerter] = None
    # Institutional flow (§20): a volume anomaly from our own bars is always computed; SEC 13D/13G
    # ownership filings need a source (None = not connected, stated to the model as unavailable).
    flow_filings_source: Optional[OwnershipFilingsSource] = None
    flow_engine: InstitutionalFlowEngine = field(default_factory=InstitutionalFlowEngine)
    disagreement: ModelDisagreementEngine = field(default_factory=ModelDisagreementEngine)
    # Read-only listeners (services/pdca/observer.py): they record and grade, never decide.
    observers: list[SessionObserver] = field(default_factory=list)
    # docs/SAFETY_AUDIT.md F7: when set, the pre-trade audit trail for every
    # order that reaches execution.submit() is durably persisted (default
    # None — fully additive, no behavior change for the many tests that
    # construct TradingPipeline without one).
    audit_store: Optional[DurableAuditStore] = None
    # §26: market regime fed to Decision AI. Read from the universe composite
    # (an estimate — see services/regime/engine.universe_composite_bars).
    regime_engine: RegimeEngine = field(default_factory=RegimeEngine)
    # short label (e.g. "BULL") stamped on decision snapshots for this session
    _session_regime: str = field(default="UNKNOWN", repr=False)
    _order_seq: int = 0
    # symbol -> (protective stop client_order_id, stop plan, entry price, risk amount)
    open_stops: dict[str, tuple[str, StopPlan, float, float]] = field(default_factory=dict)
    # decision_id -> the Final Trade Thesis built for that decision this session
    # (§27); looked back up at order-placement time to carry skeptic_id into
    # the Approved Order Snapshot without widening SizedProposal.
    _final_theses: dict[str, FinalTradeThesis] = field(default_factory=dict, repr=False)
    # proposal_id -> real wall-clock time the signal (decision+skeptic-passed
    # proposal) was captured, for F2's signal_age_sec (services.pipeline
    # is the only writer/reader; not a durable audit trail — see F7).
    _signal_captured_at: dict[str, datetime] = field(default_factory=dict, repr=False)
    # Result of the latest ledger-vs-broker reconciliation (§48). False until the
    # first reconcile of a session has run: "not yet verified" is not "consistent".
    _reconciliation_ok: bool = field(default=False, repr=False)
    # symbol -> max |corr| against holdings, computed at the sizing step this session
    _book_correlation: dict[str, float] = field(default_factory=dict, repr=False)
    reconciliation: ReconciliationEngine = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """§73: reject a mixed-environment component graph at construction.

        `require_same_environment` existed but had no production caller, so
        nothing noticed that `IndependentAuditor` carried its own environment.
        A LIVE pipeline holding a PAPER auditor is exactly the configuration
        that turns the LIVE fail-closed audit into a fail-open one, so it must
        be impossible to build rather than merely discouraged.
        """
        require_same_environment(self.environment, self.execution.environment,
                                 self.auditor.environment)
        self.reconciliation = ReconciliationEngine(
            broker=self.execution.broker, ledger=self.ledger,
            risk_controller=self.risk_controller)

    def final_theses(self) -> dict[str, FinalTradeThesis]:
        """Read-only view for callers outside the pipeline (the status API's
        Final Trade Thesis panel, §27) — a copy, so nothing external can
        mutate pipeline state through it."""
        return dict(self._final_theses)

    def _persist_audit_record(self, log_rec, at: datetime) -> None:
        """F7: durably snapshot a PreTradeRecord once its order has reached
        execution.submit() — matches PreTradeAuditLog.is_fully_traceable's
        own definition of what must be traceable (A5). `default=str` covers
        the raw `datetime` fields on PreTradeRecord itself; every nested
        dict (audit_result, risk_result, ...) is already JSON-safe, built
        via pydantic's `model_dump(mode="json")`."""
        if self.audit_store is None:
            return
        record_json = json.dumps(dataclasses.asdict(log_rec), default=str)
        self.audit_store.upsert(log_rec.client_order_id, log_rec.decision_id,
                                record_json, at)

    def _record_fill(self, symbol: str, side_qty: float, price: float, fees: float,
                     ts: datetime, note: str = "") -> None:
        """Book a fill on the ledger and, if a cost engine is attached, record
        its fee for the §80-83 cost breakdown. One call site so every fill —
        entry, resting-order sync, or stop-out — reports its fee the same way;
        previously `ledger.record_fill` was called directly from three places
        and none of them made the fee visible anywhere but trading_pnl."""
        self.ledger.record_fill(symbol, side_qty, price, fees, ts, note=note)
        if self.cost_engine is not None:
            self.cost_engine.record_transaction_fee(
                at=ts, amount=fees, note=note or symbol)

    def _read_regime(self, symbols: list[str], now: datetime) -> str:
        """Classify the market from the universe composite. Any shortfall
        (too few symbols/bars) degrades to "UNKNOWN" — never a guess."""
        try:
            bars = {sym: [b.bar for b in self.market_data.bars(sym, now, days=40, received_at=now)]
                    for sym in symbols}
            reading = self.regime_engine.classify(universe_composite_bars(bars))
        except (ValueError, MarketDataUnavailable):
            self._session_regime = "UNKNOWN"
            return "UNKNOWN"
        self._session_regime = reading.primary.value
        return describe_universe_regime(reading, len(bars))

    def _record_decision(self, decision_id: str, symbol: str, kind: DecisionKind,
                         now, reference_price: float, decision=None,
                         had_stop_plan: bool = False, skeptic_consulted: bool = False,
                         regime: Optional[str] = None) -> None:
        """A1-1: immutable decision snapshot for EVERY decision kind."""
        from services.data_validation.integrity import DataHealth

        snap = DecisionSnapshot(
            decision_id=f"{decision_id}:{kind.value}",
            symbol=symbol, ts=now, reference_price=reference_price, decision=kind,
            confidence=decision.confidence if decision else 0.5,
            expected_horizon=decision.expected_horizon if decision else "1w",
            expected_return_range=(tuple(decision.expected_return_range)
                                   if decision else (-0.05, 0.05)),
            scenarios=({"bull": decision.bull_case.model_dump(mode="json"),
                        "base": decision.base_case.model_dump(mode="json"),
                        "bear": decision.bear_case.model_dump(mode="json")}
                       if decision else {}),
            thesis=decision.base_case.description if decision else "",
            invalidation_conditions=(tuple(decision.invalidation_conditions)
                                     if decision else ()),
            regime=regime or self._session_regime,
            model=self.decision_model.name, model_version=self.decision_model.version,
            rule_compliant=True, had_stop_plan=had_stop_plan,
            skeptic_consulted=skeptic_consulted,
            data_health_ok=self.integrity.health is not DataHealth.HALT_ENTRIES)
        self.decision_quality.record(snap)

    # -- protective exits (§33-34, §40, §43, INV-15) -------------------------
    def _adv_shares(self, symbol: str, now: datetime, days: int = 20) -> float:
        """Real 20-day average daily volume (§41).

        The risk views used to pass a literal 1_000_000 here, which made the
        Master Risk Controller's liquidity cap a flat 10,000 shares for every
        symbol — looser than the sizing engine it is supposed to backstop,
        which already uses the real ADV. A final barrier that is weaker than
        the layer above it is not a barrier.
        """
        try:
            stamped = self.market_data.bars(symbol, now, days, received_at=now)
        except Exception:
            return 0.0   # unknown liquidity must not read as unlimited
        if not stamped:
            return 0.0
        window = stamped[-days:]
        return sum(s.bar.volume for s in window) / len(window)

    def _settled_cash(self) -> float:
        """§14: only settled cash is spendable.

        `ledger.cash` is NOT it. The ledger books sale proceeds as cash the
        moment a fill lands, while the broker correctly holds them unsettled
        until T+1 — so passing ledger.cash here let the risk controller approve
        purchases against money that had not settled. The only thing catching
        those was the broker's own guard, which rejected 116 such orders in a
        200-session run. The broker is the authority on settlement (§49).
        """
        try:
            return self.execution.broker.get_cash().settled_cash
        except BrokerDisconnectedError:
            # Unknown settlement state must not read as "plenty available".
            return 0.0

    def _broker_connected(self) -> bool:
        """F8: real connectivity, not a literal `True`. The `broker_connected`
        RiskCheck itself was never the only protection — `risk_controller.
        state is FULL_BROKER_DISCONNECT` already independently blocks entries
        (services/execution/engine.py sets it on disconnect) — this fixes the
        audit trail being misleading (`decisions_log` showing `PASS` during
        an actual disconnect, correctly rejected for a different reason),
        not an active safety hole."""
        return self.risk_controller.state is not RiskState.FULL_BROKER_DISCONNECT

    def _exit_risk_view(self, symbol: str, qty: float, now: datetime) -> PortfolioRiskView:
        prices = self.mark_prices({}, now)
        pos_notional = self._position_notional({}, now)
        snapshot = self.ledger.snapshot(prices)
        return PortfolioRiskView(
            equity=snapshot.equity, settled_cash=self._settled_cash(),
            total_exposure_notional=sum(pos_notional.values()),
            position_notional=pos_notional,
            position_qty={s: l.qty for s, l in self.ledger.positions.items()},
            theme_exposure=self._theme_exposure({}, now),
            symbol_themes=self.symbol_themes, drawdown=snapshot.drawdown,
            stop_plan_exists=True, gap_risk_score=0.0,
            adv_shares=self._adv_shares(symbol, now),
            correlation_to_book=0.0,       # exit view: the correlation check is exempt for exits
            reconciliation_ok=self._reconciliation_ok,
            data_health=self.integrity.health, broker_connected=self._broker_connected(),
            spread_pct=self._spread_pct(symbol, now),
            known_client_order_ids=frozenset(self.execution._submitted),  # noqa: SLF001
            # F2: a real value isn't needed here — check 15 (`stale_order`)
            # is unconditionally exempted for exits (`is_exit or ...` in
            # MasterRiskController.review), so this is structurally inert,
            # not another instance of the same gap.
            signal_age_sec=0.0, margin_requirement=0.0)

    def place_protective_stop(self, symbol: str, qty: float, stop: StopPlan,
                              now: datetime, decision_id: str = "") -> bool:
        """Submit a resting protective STOP SELL so the planned stop actually
        exists at the broker (§33-34).  Risk-reducing exits stay permitted even
        under MASTER STOP (§43)."""
        if qty <= 0:
            return False
        self._order_seq += 1
        intent = OrderIntent(
            client_order_id=make_client_order_id(self.environment, f"stop{symbol}",
                                                 self._order_seq),
            proposal_id=f"protective-{symbol}", symbol=symbol, side=Action.SELL,
            qty=qty, order_type=OrderType.STOP, stop_price=stop.stop_price,
            time_in_force=TimeInForce.GTC,           # must outlive the session (INV-15)
            environment=self.environment, is_protective_exit=True, created_at=now)
        verdict = self.risk_controller.review(intent, self._exit_risk_view(symbol, qty, now))
        if isinstance(verdict, RiskRejection):
            self.audit_log.record_near_miss(Stage.RISK, NearMissKind.NO_STOP, now,
                                            decision_id, intent.client_order_id,
                                            detail="; ".join(verdict.reasons))
            return False
        approved = RiskApprovedOrder(intent=intent, approval=verdict)
        snapshot = ApprovedOrderSnapshot.from_approved(approved, decision_id=decision_id)
        log_rec = self.audit_log.open(intent.client_order_id, decision_id, now,
                                      {"symbol": symbol, "side": "SELL",
                                       "qty": qty, "protective": True})
        log_rec.protective_exit = True   # semantic audit N/A: no decision to compare
        log_rec.risk_result = {"passed": True, "approval_id": verdict.approval_id}
        log_rec.approved_snapshot_hash = snapshot.hash
        log_rec.broker_submitted = True
        state = self.execution.submit(approved, snapshot=snapshot)
        log_rec.final_state = state.value
        self._persist_audit_record(log_rec, now)
        # Adding to a held position places a SECOND resting stop for the new
        # shares; the first one stays live at the broker. Accumulate the risk
        # so the anti-martingale guard sees the position's total risk rather
        # than only the most recent lot's.
        prior = self.open_stops.get(symbol)
        prior_risk = prior[3] if prior else 0.0
        self.open_stops[symbol] = (intent.client_order_id, stop, stop.entry_price,
                                   prior_risk + qty * stop.stop_distance)
        return True

    def manage_open_positions(self, now: datetime,
                              result: Optional["PipelineResult"] = None) -> tuple[int, int]:
        """Session-start exit management: let resting stops trigger, book the
        fills, and feed the learning loops.  Returns (stops_triggered, synced)."""
        fills = self.execution.sync_open_orders()
        triggered = 0
        result = result if result is not None else PipelineResult()
        for f in fills:
            stop_entry = self.open_stops.get(f.symbol)
            self._emit("on_fill", FillEvent(
                symbol=f.symbol, side=f.side, qty=f.qty, price=f.price, fees=f.fees, ts=f.ts,
                client_order_id=f.client_order_id,
                reference_price=(stop_entry[1].stop_price
                                 if stop_entry is not None and f.side is Action.SELL else None)),
                result)
            if f.side is not Action.SELL:
                # a resting BUY (e.g. a limit entry) that fills later must still
                # reach the ledger, or ledger and broker drift apart (§48)
                self._record_fill(f.symbol, f.qty, f.price, f.fees, f.ts,
                                  note=f.client_order_id)
                continue
            # Realized P&L must come from the ledger's weighted average cost,
            # NOT from the shadow entry price in open_stops. open_stops is
            # keyed by symbol, so buying more of a held name overwrote the
            # first stop's entry price, and the whole exit was then priced off
            # the later entry — feeding the anti-martingale guard (§37/INV-10)
            # a loss figure that diverged from the ledger by 13% over 200
            # sessions. The ledger already holds the correct basis.
            lot_before = self.ledger.positions.get(f.symbol)
            avg_cost = lot_before.avg_cost if lot_before else f.price
            self._record_fill(f.symbol, -f.qty, f.price, f.fees, f.ts,
                              note=f.client_order_id)
            entry = self.open_stops.get(f.symbol)
            realized = 0.0
            if entry is not None:
                _, stop, _entry_px, risk_amount = entry
                realized = (f.price - avg_cost) * f.qty - f.fees
                # §37/INV-10: feed the anti-martingale guard with the outcome
                self.sizing.record_trade_result(realized_pnl=realized,
                                                risk_amount=risk_amount)
                # §50: grade the stop afterwards
                self.post_trade.track(f.symbol, f.price, f.ts, kind="STOP")
                if self.ledger.position_qty(f.symbol) <= 1e-9:
                    self.open_stops.pop(f.symbol, None)
            # A1-4: the SELL is a decision and is graded too
            self._record_decision(f"exit-{f.symbol}-{f.ts.date()}", f.symbol,
                                  DecisionKind.SELL, now, f.price,
                                  had_stop_plan=True, skeptic_consulted=False)
            triggered += 1
        return triggered, len(fills)

    def run_session(self, as_of: datetime) -> PipelineResult:
        result = self._run_session(as_of)
        equity = self.ledger.snapshot(self.mark_prices({}, as_of)).equity
        self._emit("on_session_end", SessionEndEvent(as_of=as_of, result=result,
                                                     price_on=self._close_on_or_before,
                                                     equity=equity), result)
        return result

    def _emit(self, hook: str, event, result: PipelineResult) -> None:
        """Hand a fact to every observer. One that raises is reported, never allowed to
        interrupt trading or hide the others."""
        for obs in self.observers:
            try:
                getattr(obs, hook)(event)
            except Exception as e:  # noqa: BLE001 — observers are best-effort by design
                log.warning("observer %s failed in %s: %s", obs.name, hook, e)
                result.data_warnings.append(f"observer {obs.name} failed: {type(e).__name__}")

    def _close_on_or_before(self, symbol: str, at: datetime) -> Optional[float]:
        """Last daily close at/before `at`, or None when it cannot be had (used to grade
        forecasts/exits after the fact; never to open risk)."""
        try:
            bars = self.market_data.bars(symbol, at, days=5, received_at=at)
        except MarketDataUnavailable:
            return None
        usable = [b.bar for b in bars if b.bar.ts.date() <= at.date()]
        return usable[-1].close if usable else None

    def _run_session(self, as_of: datetime) -> PipelineResult:
        result = PipelineResult()
        now = as_of
        # Per-session working state. Theses are only ever read back within the
        # same run_session (order placement looks up the key written above it),
        # so keeping earlier sessions' entries leaked ~10 objects/session
        # forever AND made the §27 dashboard panel show every session ever run
        # while claiming to show "this session".
        self._final_theses.clear()
        self._signal_captured_at.clear()
        self._book_correlation.clear()

        if self.heartbeat_alerter is not None:
            self.heartbeat_alerter.check()

        # 0. Exit management first: resting protective stops may have triggered
        #    since the last session (§33-34, §43)
        result.stops_triggered, _ = self.manage_open_positions(now, result)

        # 0a. Operator kill switch (F5): exits above already ran; stop before any
        #     LLM spend. Mid-session engagement is caught again before each entry.
        blocked = self.execution.entries_blocked()
        if blocked:
            result.no_trade_reasons.append(blocked)
            return result

        # 0b. Reconcile ledger vs broker every session (§48): a mismatch halts new
        #     entries (the engine sets HALT_NEW_ENTRIES), so stop before spending
        #     LLM calls on decisions that cannot become orders.
        report = self.reconciliation.reconcile()
        self._reconciliation_ok = report.consistent
        if not report.consistent:
            result.no_trade_reasons.append(
                "reconciliation mismatch — new entries halted: "
                + "; ".join(f"{m.kind} ({m.detail})" for m in report.mismatches[:3]))
            return result

        # 1. Universe + Scanner (§12, §21)
        symbols = self.universe.symbols_as_of(now.date())
        scans = self.scanner.scan(symbols, now)
        result.scanned = self.scanner.last_funnel.scanned
        result.candidates = self.scanner.last_funnel.passed_advanced
        if not scans:
            result.no_trade_reasons.append("scanner produced no candidates")
            return result

        # data integrity gate (§11)
        gated = []
        quotes: dict[str, Quote] = {}
        decision_times: dict[str, datetime] = {}
        for s in scans:
            self.integrity.validate_bars(s.symbol, list(s.bars))
            try:
                quote = self.market_data.quote(s.symbol, now)
            except MarketDataUnavailable:
                result.no_trade_reasons.append(f"{s.symbol}: no valid quote — skipped")
                continue
            decided_at = self._decision_time(now, quote)
            self.integrity.validate_quote(quote, decided_at)
            # Cost/quality pre-filter using the SAME cap the Master Risk Controller
            # enforces (single source: RiskConfig.max_spread_pct) — a quote already
            # too wide to trade must not buy an LLM call. The controller still
            # decides for everything that passes.
            spread_pct = quote.spread / quote.mid
            cap = self.risk_controller.config.max_spread_pct
            if spread_pct > cap:
                result.no_trade_reasons.append(
                    f"{s.symbol}: quote spread {spread_pct:.2%} exceeds cap {cap:.2%} — "
                    f"no Decision call")
                continue
            quotes[s.symbol] = quote
            decision_times[s.symbol] = decided_at
            gated.append(s)
        scans = gated
        if not scans:
            result.no_trade_reasons.append("no candidate has a valid quote")
            return result

        # 2. Decision AI + Skeptic (§27-29) — proposals only, no broker access
        sized_candidates: list[SizedProposal] = []
        prices: dict[str, float] = {}
        regime_text = self._read_regime(symbols, now)
        news_signals, news_fetched = self._load_news([s.symbol for s in scans], now, result)
        calendar, events_fetched = self._load_events([s.symbol for s in scans], now, result)
        filings_by_symbol, filings_fetched = self._load_flow_filings(
            [s.symbol for s in scans], now, result)
        for scan in scans[: self.max_new_positions * 3]:
            chosen = select_for_symbol(news_signals, scan.symbol) if news_fetched else []
            flow_lines, flow_obs = describe_flow(
                scan.symbol, scan.bars,
                filings_by_symbol.get(scan.symbol, []) if filings_fetched else None)
            if flow_obs is not None:
                self.flow_engine.ingest(flow_obs)
            ctx = DecisionContext(scan=scan, regime=regime_text,
                                  flow_lines=flow_lines, flow_filings_fetched=filings_fetched,
                                  as_of=decision_times.get(scan.symbol, now),
                                  news=[self._untrusted(s) for s in chosen],
                                  news_fetched=news_fetched,
                                  upcoming_events=[
                                      f"{e.on.isoformat()} {e.event_type.value}"
                                      + (f" — {e.description}" if e.description else "")
                                      for e in calendar.events_between(
                                          now.date(), now.date() + timedelta(
                                              days=self.event_display_days),
                                          symbol=scan.symbol)] if events_fetched else [],
                                  events_fetched=events_fetched,
                                  quote=quotes.get(scan.symbol),
                                  portfolio_summary={"cash": self.ledger.cash})
            rec = self.provenance.open(decision_id=f"{scan.symbol}-{now.date()}")
            rec.model = self.decision_model.name
            rec.model_version = self.decision_model.version
            rec.input_features = {"momentum_20d": scan.momentum_20d,
                                  "volatility": scan.volatility,
                                  "dollar_volume": scan.dollar_volume}
            rec.data_timestamps = {"last_bar": scan.bars[-1].ts.isoformat()}
            if chosen:
                rec.input_features["news_urls"] = [u for s in chosen for u in s.urls[:1]]
                rec.data_timestamps["latest_news"] = max(
                    s.published_at for s in chosen if s.published_at).isoformat()
            try:
                decision = validate_decision(self.decision_model.decide(ctx))
            except MalformedDecisionError as e:
                result.no_trade_reasons.append(f"{scan.symbol}: malformed decision rejected (§28)")
                rec.result = {"rejected": "malformed decision", "error": str(e)[:200]}
                continue
            except LLMCallError as e:
                # A provider outage / exhausted credit / refusal on ONE candidate
                # must not abort the whole session (and with it exit management
                # for open positions). No decision == no trade for this symbol.
                result.no_trade_reasons.append(
                    f"{scan.symbol}: Decision AI unavailable — no trade ({type(e).__name__})")
                rec.result = {"rejected": "decision AI unavailable", "error": str(e)[:200]}
                continue
            rec.output = decision.model_dump(mode="json")
            self._emit("on_decision", DecisionEvent(
                decision_id=rec.decision_id, symbol=scan.symbol, as_of=now, decision=decision,
                price=quotes[scan.symbol].mid if scan.symbol in quotes else None, context=ctx),
                result)
            # §2: a SELL is only ever a reduction of an EXISTING long. A SELL
            # on an unheld symbol is a Decision AI error, not a trade — it is
            # rejected here and recorded, before any order can be built.
            if decision.action is DecisionAction.SELL:
                held = self.ledger.position_qty(scan.symbol)
                if held <= 0:
                    result.no_trade_reasons.append(
                        f"{scan.symbol}: SELL decision on unheld symbol rejected — "
                        f"shorting is forbidden (§2); AVOID/WAIT/NO_TRADE expected")
                    self.audit_log.record_near_miss(
                        Stage.AUDIT, NearMissKind.WRONG_SIDE, now, rec.decision_id,
                        detail=f"SELL decision for unheld {scan.symbol}")
                    self._record_decision(rec.decision_id, scan.symbol,
                                          DecisionKind.AVOID, now, scan.last_close,
                                          decision=decision)
                    continue

            if decision.action is not DecisionAction.BUY:
                # A1-4: NO_TRADE decisions are tracked and graded too
                self._record_decision(rec.decision_id, scan.symbol, DecisionKind.NO_TRADE,
                                      now, scan.last_close, decision=decision)
                continue
            result.decision_candidates += 1

            critique = self.skeptic.critique(decision, ctx)
            rec.skeptic_output = critique.model_dump(mode="json")
            if critique.recommends_veto:
                result.skeptic_vetoes += 1
                result.no_trade_reasons.append(
                    f"{scan.symbol}: skeptic veto — {'; '.join(critique.objections)}")
                self._record_decision(rec.decision_id, scan.symbol, DecisionKind.AVOID,
                                      now, scan.last_close, decision=decision,
                                      skeptic_consulted=True)
                continue

            proposal = TradeProposal(symbol=scan.symbol, side=Action.BUY,
                                     source=ProposalSource.AI, decision=decision,
                                     skeptic=critique, created_at=now)
            # F2: real time, not simulated `now` — this is the signal's actual
            # birth for staleness purposes (LLM latency, retries, a stuck
            # queue all happen between here and the risk review below).
            self._signal_captured_at[proposal.proposal_id] = self.wall_clock.now()

            # 2b. Final Trade Thesis (§27): fuses Decision + Skeptic before the
            # deterministic stages take over, and records how much they
            # disagreed even though the Skeptic did not veto.
            thesis = build_final_trade_thesis(
                proposal, decision_id=rec.decision_id,
                skeptic_id=getattr(self.skeptic, "name", critique.model_family))
            self._final_theses[rec.decision_id] = thesis
            rec.final_trade_thesis = thesis.model_dump(mode="json")

            # 3. Loss Control BEFORE sizing (§33)
            quote = self.market_data.quote(scan.symbol, now)
            entry_price = quote.ask
            prices[scan.symbol] = quote.mid
            has_event = events_fetched and calendar.has_event_before(
                scan.symbol, now.date(), self.event_horizon_days)
            gap = gap_risk_score(list(scan.bars), has_event_before_horizon=has_event)
            try:
                stop = self.loss_control.plan(proposal, list(scan.bars), entry_price, gap)
            except NoStopPlanError as e:
                result.no_trade_reasons.append(f"{scan.symbol}: no stop plan — {e}")
                self._record_decision(rec.decision_id, scan.symbol, DecisionKind.AVOID,
                                      now, scan.last_close, decision=decision,
                                      skeptic_consulted=True)
                continue
            result.stop_planned += 1
            rec.stop_plan = stop.model_dump(mode="json")

            # 4. Position Sizing (§35)
            snapshot = self.ledger.snapshot(self.mark_prices(prices, now))
            level = throttle_level(snapshot.drawdown, self.risk_controller.config)
            pctx = PortfolioContext(
                equity=snapshot.equity, settled_cash=self._settled_cash(),
                existing_exposure=self._position_notional(prices, now),
                theme_exposure=self._theme_exposure(prices, now),
                symbol_themes=self.symbol_themes,
                correlation_to_book=self._correlation_to_book(scan, now),
                adv_shares=sum(b.volume for b in scan.bars[-20:]) / 20,
                throttle_factor=throttle_factor(level, self.risk_controller.config))
            calibrated = self.calibration.calibrate(decision.confidence)
            # §30: when the sources disagree, confidence — and with it the size — shrinks.
            # The multiplier is in [0.5, 1.0]: this can only make a position smaller.
            reading = self._measure_disagreement(scan, decision, critique, chosen)
            if reading is not None:
                calibrated *= reading.risk_multiplier
                rec.disagreement = {"views": reading.views,
                                    "disagreement": round(reading.disagreement, 4),
                                    "size_multiplier": round(reading.risk_multiplier, 4)}
                result.disagreement_multipliers[scan.symbol] = round(reading.risk_multiplier, 4)
            try:
                sized = self.sizing.size(proposal, stop, quote, pctx, calibrated)
            except SizingRejected as e:
                result.no_trade_reasons.append(f"{scan.symbol}: sizing rejected — {e}")
                self._record_decision(rec.decision_id, scan.symbol, DecisionKind.AVOID,
                                      now, scan.last_close, decision=decision,
                                      had_stop_plan=True, skeptic_consulted=True)
                continue
            result.sized += 1
            rec.position_size = {"qty": sized.qty, "risk_amount": sized.risk_amount,
                                 "notional": sized.notional}
            sized_candidates.append(sized)

        if not sized_candidates:
            if not result.no_trade_reasons:
                result.no_trade_reasons.append("no BUY decisions survived the funnel")
            return result

        # 5. Capital Allocation (§36)
        snapshot = self.ledger.snapshot(self.mark_prices(prices, now))
        alloc = self.allocation.allocate(sized_candidates, equity=snapshot.equity,
                                         settled_cash=self._settled_cash(),
                                         current_exposure_notional=snapshot.positions_value)
        for sp, why in alloc.skipped:
            result.no_trade_reasons.append(f"{sp.proposal.symbol}: allocation skipped — {why}")
            rec = self.provenance.get(f"{sp.proposal.symbol}-{now.date()}")
            self._record_decision(rec.decision_id, sp.proposal.symbol, DecisionKind.WAIT,
                                  now, sp.stop_plan.entry_price, decision=sp.proposal.decision,
                                  had_stop_plan=True, skeptic_consulted=True)
        result.allocated = len(alloc.accepted)

        # 6. Audit AI → Master Risk Controller → Snapshot → Execution (A3-A4, §42, §44)
        for sized in alloc.accepted[: self.max_new_positions]:
            self._order_seq += 1
            symbol = sized.proposal.symbol
            intent = OrderIntent(
                client_order_id=make_client_order_id(self.environment,
                                                     sized.proposal.proposal_id, self._order_seq),
                proposal_id=sized.proposal.proposal_id, symbol=symbol,
                side=Action.BUY, qty=sized.qty, order_type=OrderType.MARKET,
                environment=self.environment, created_at=now)
            rec = self.provenance.get(f"{symbol}-{now.date()}")
            log_rec = self.audit_log.open(intent.client_order_id, rec.decision_id, now,
                                          {"symbol": symbol, "side": "BUY",
                                           "qty": sized.qty})

            # 6a. Independent Audit AI (A3) — semantic gate BEFORE deterministic risk
            try:
                audit_quote: Optional[Quote] = self.market_data.quote(symbol, now)
            except MarketDataUnavailable:
                audit_quote = None            # the audit prompt says "not available"
            try:
                audit = self.auditor.audit(
                    sized.proposal.decision, sized, intent,
                    AuditContext(now=now, quote=audit_quote,
                                 quote_as_of=(self._decision_time(now, audit_quote)
                                              if audit_quote else None)))
            except AuditUnavailableError as e:
                result.no_trade_reasons.append(f"{symbol}: audit unavailable — {e}")
                self.audit_log.record_near_miss(Stage.AUDIT, NearMissKind.OTHER, now,
                                                rec.decision_id, intent.client_order_id,
                                                detail=str(e))
                self._record_decision(rec.decision_id, symbol, DecisionKind.AVOID, now,
                                      sized.stop_plan.entry_price,
                                      decision=sized.proposal.decision,
                                      had_stop_plan=True, skeptic_consulted=True)
                continue
            log_rec.audit_result = audit.model_dump(mode="json")
            if audit.verdict is not AuditVerdict.PASS:
                # §8: REVIEW is not the same as REJECT — it goes to a human
                # rather than being silently discarded. Either way the order
                # does not proceed automatically.
                if audit.verdict is AuditVerdict.REVIEW:
                    result.audit_review += 1
                    self.audit_log.queue_human_review(
                        intent.client_order_id, rec.decision_id, now,
                        reasons=list(audit.reasons), severity=audit.severity)
                    result.no_trade_reasons.append(
                        f"{symbol}: audit REVIEW — queued for human review: "
                        f"{'; '.join(audit.reasons)}")
                else:
                    result.audit_rejected += 1
                    result.no_trade_reasons.append(
                        f"{symbol}: audit REJECT — {'; '.join(audit.reasons)}")
                    self.audit_log.record_audit_rejection(log_rec.audit_result, now,
                                                          rec.decision_id,
                                                          intent.client_order_id)
                self._record_decision(rec.decision_id, symbol, DecisionKind.AVOID, now,
                                      sized.stop_plan.entry_price,
                                      decision=sized.proposal.decision,
                                      had_stop_plan=True, skeptic_consulted=True)
                continue
            result.audit_passed += 1

            blocked = self.execution.entries_blocked()
            if blocked:
                result.no_trade_reasons.append(f"{symbol}: {blocked}")
                continue

            # 6b. deterministic Master Risk Controller — the FINAL barrier (A3-4)
            view = self._risk_view(sized, prices, now)
            verdict = self.risk_controller.review(intent, view,
                                                  entry_price=sized.stop_plan.entry_price)
            if isinstance(verdict, RiskRejection):
                result.risk_rejected += 1
                result.no_trade_reasons.append(
                    f"{symbol}: risk rejected — {'; '.join(verdict.reasons)}")
                rec.risk_decision = {"passed": False, "reasons": list(verdict.reasons)}
                log_rec.risk_result = {"passed": False, "reasons": list(verdict.reasons)}
                self.audit_log.record_near_miss(Stage.RISK, NearMissKind.OTHER, now,
                                                rec.decision_id, intent.client_order_id,
                                                detail="; ".join(verdict.reasons))
                self._record_decision(rec.decision_id, symbol, DecisionKind.AVOID, now,
                                      sized.stop_plan.entry_price,
                                      decision=sized.proposal.decision,
                                      had_stop_plan=True, skeptic_consulted=True)
                continue
            assert isinstance(verdict, RiskApproval)
            result.risk_passed += 1
            rec.risk_decision = {"passed": True, "approval_id": verdict.approval_id,
                                 "checks": [c.name for c in verdict.checks]}
            log_rec.risk_result = {"passed": True, "approval_id": verdict.approval_id}

            # 6c. Immutable Approved Order Snapshot (A4) → Execution
            approved = RiskApprovedOrder(intent=intent, approval=verdict)
            thesis = self._final_theses.get(rec.decision_id)
            snapshot = ApprovedOrderSnapshot.from_approved(
                approved, decision_id=rec.decision_id, audit_id=audit.audit_id,
                skeptic_id=thesis.skeptic_id if thesis else "",
                take_profit=sized.stop_plan.profit_target)
            log_rec.approved_snapshot_hash = snapshot.hash
            log_rec.broker_submitted = True
            self._record_decision(rec.decision_id, symbol, DecisionKind.BUY, now,
                                  sized.stop_plan.entry_price,
                                  decision=sized.proposal.decision,
                                  had_stop_plan=True, skeptic_consulted=True)

            state = self.execution.submit(
                approved, snapshot=snapshot,
                reference_price=sized.stop_plan.entry_price)   # planned entry (ask) -> F6 slippage
            log_rec.final_state = state.value
            rec.order_ref = intent.client_order_id
            if state in (OrderState.FILLED, OrderState.PARTIALLY_FILLED):
                filled_qty = 0.0
                for f in self.execution.broker.get_order_fills(intent.client_order_id):
                    filled_qty += f.qty
                    self._record_fill(f.symbol, f.qty if f.side is Action.BUY else -f.qty,
                                      f.price, f.fees, f.ts,
                                      note=intent.client_order_id)
                    rec.fill_refs.append(f.broker_fill_id)
                    log_rec.fills.append(f.broker_fill_id)
                    result.fills.append(f)
                    self._emit("on_fill", FillEvent(
                        symbol=f.symbol, side=f.side, qty=f.qty, price=f.price, fees=f.fees,
                        ts=f.ts, client_order_id=f.client_order_id,
                        reference_price=sized.stop_plan.entry_price), result)
                result.orders_filled += 1
                rec.result = {"state": state.value}
                self._persist_audit_record(log_rec, now)
                # the planned stop must EXIST at the broker, not just on paper
                # (§33-34, INV-15): place it immediately after the entry fills
                if self.place_protective_stop(symbol, filled_qty, sized.stop_plan, now,
                                              decision_id=rec.decision_id):
                    result.protective_stops_placed += 1
                else:
                    result.no_trade_reasons.append(
                        f"{symbol}: protective stop could not be placed — position "
                        f"is unprotected, review required")
            else:
                rec.result = {"state": state.value}
                self._persist_audit_record(log_rec, now)
                result.no_trade_reasons.append(
                    f"{sized.proposal.symbol}: order ended {state.value}")
        return result

    # -- helpers -------------------------------------------------------------
    def mark_prices(self, known: dict[str, float], now: datetime) -> dict[str, float]:
        prices = dict(known)
        for sym in self.ledger.positions:
            if sym not in prices:
                prices[sym] = self._mark_price(sym, now)
        return prices

    def _mark_price(self, symbol: str, now: datetime) -> float:
        """Valuation price for a HELD symbol. A missing quote (market closed,
        bid/ask 0) must not abort the session that manages the exits: fall back
        to the last daily close, then to cost basis. Never used to open risk."""
        try:
            return self.market_data.quote(symbol, now).mid
        except MarketDataUnavailable:
            pass
        try:
            return self.market_data.bars(symbol, now, days=5, received_at=now)[-1].bar.close
        except (MarketDataUnavailable, IndexError):
            return self.ledger.positions[symbol].avg_cost

    def _position_notional(self, prices: dict[str, float], now: datetime) -> dict[str, float]:
        marks = self.mark_prices(prices, now)
        return {sym: lot.qty * marks[sym] for sym, lot in self.ledger.positions.items()}

    def _theme_exposure(self, prices: dict[str, float], now: datetime) -> dict[str, float]:
        exposure: dict[str, float] = {}
        for sym, notional in self._position_notional(prices, now).items():
            for theme in self.symbol_themes.get(sym, []):
                exposure[theme] = exposure.get(theme, 0.0) + notional
        return exposure

    def _risk_view(self, sized: SizedProposal, prices: dict[str, float],
                   now: datetime) -> PortfolioRiskView:
        snapshot = self.ledger.snapshot(self.mark_prices(prices, now))
        pos_notional = self._position_notional(prices, now)
        return PortfolioRiskView(
            equity=snapshot.equity, settled_cash=self._settled_cash(),
            total_exposure_notional=sum(pos_notional.values()),
            position_notional=pos_notional,
            position_qty={s: l.qty for s, l in self.ledger.positions.items()},
            theme_exposure=self._theme_exposure(prices, now),
            symbol_themes=self.symbol_themes,
            drawdown=snapshot.drawdown, stop_plan_exists=True,
            gap_risk_score=sized.stop_plan.gap_risk_score,
            adv_shares=self._adv_shares(sized.proposal.symbol, now),
            correlation_to_book=self._book_correlation.get(sized.proposal.symbol, 0.0),
            reconciliation_ok=self._reconciliation_ok, data_health=self.integrity.health,
            broker_connected=self._broker_connected(),
            spread_pct=self._spread_pct(sized.proposal.symbol, now),
            known_client_order_ids=frozenset(self.execution._submitted),  # noqa: SLF001
            # F2: real elapsed wall-clock time since the signal was captured
            # above — KeyError, not a silent 0.0 fallback, if a `sized`
            # reaches here without having gone through that capture, since
            # every production call path does and a miss means the plumbing
            # broke, not that the signal is fresh.
            signal_age_sec=(self.wall_clock.now()
                            - self._signal_captured_at[sized.proposal.proposal_id]
                            ).total_seconds(),
            margin_requirement=0.0)

    def _load_news(self, symbols: list[str], now: datetime,
                   result: "PipelineResult") -> tuple[list[NewsSignal], bool]:
        """One batched fetch for the session's candidates. (signals, fetched):
        `fetched=False` (no source, or it failed) is reported to the model as
        "news not available" — never as "no news". A failure does not stop the
        session; news is advisory input, not a gate."""
        if self.news_source is None or not symbols:
            return [], False
        try:
            items = self.news_source.fetch(symbols, since=now - self.news_lookback, until=now)
        except NewsUnavailable as e:
            result.data_warnings.append(f"news unavailable — Decision runs without it ({e})")
            return [], False
        # one article at a time: the engine clusters by (tickers, day), which would
        # merge distinct stories and let one directive-like item quarantine every
        # legitimate story for the same symbol. Dedup state still carries across.
        for err in getattr(self.news_source, "last_errors", []):
            result.data_warnings.append(f"partial news failure — one source is down ({err})")
        signals = [sig for item in items for sig in self.news_engine.process([item])]
        quarantined = sum(1 for s in signals if s.injection_flagged)
        if quarantined:
            result.data_warnings.append(
                f"{quarantined} news item(s) quarantined as directive-like text (§19)")
        return signals, True

    def _load_events(self, symbols: list[str], now: datetime,
                     result: "PipelineResult") -> tuple[EventCalendar, bool]:
        """Known upcoming events for the session's candidates (one fetch). Same
        contract as news: `fetched=False` is stated to the model as "not
        available", never as "no events"; the session carries on."""
        calendar = EventCalendar()
        if self.event_source is None or not symbols:
            return calendar, False
        try:
            events = self.event_source.fetch(
                symbols, now.date(), now.date() + timedelta(days=self.event_display_days))
        except EventsUnavailable as e:
            result.data_warnings.append(f"events unavailable — Decision runs without them ({e})")
            return calendar, False
        for err in getattr(self.event_source, "last_errors", []):
            result.data_warnings.append(f"partial events failure — one source is down ({err})")
        for ev in events:
            calendar.add(ev)
        return calendar, True

    def _measure_disagreement(self, scan, decision, critique, chosen_news):
        """How much the available sources disagree about this BUY (None when fewer than two
        sources have a view). Sources: quant score, Decision, Skeptic, tradeable news, institutional
        flow, market regime."""
        views = {"quant": quant_lean(scan.score), "decision": decision_lean(decision.confidence),
                 "skeptic": skeptic_lean(critique.severity)}
        news = news_lean([s.direction for s in chosen_news if s.tradeable])
        if news is not None:
            views["news"] = news
        sig = self.flow_engine.signal(scan.symbol)
        if sig is not None and sig.score != 0.0:
            views["institutional"] = sig.score
        regime = regime_lean(self._session_regime)
        if regime is not None:
            views["regime"] = regime
        return self.disagreement.measure(scan.symbol, views) if len(views) >= 2 else None

    def _load_flow_filings(self, symbols: list[str], now: datetime,
                           result: "PipelineResult") -> tuple[dict[str, list[str]], bool]:
        """Ownership filings for the session's candidates (one fetch). `fetched=False` is stated to
        the model as "not available", never as "none"; the session carries on."""
        if self.flow_filings_source is None or not symbols:
            return {}, False
        try:
            return self.flow_filings_source.fetch(symbols, now.date()), True
        except FlowUnavailable as e:
            result.data_warnings.append(f"ownership filings unavailable — Decision runs without them ({e})")
            return {}, False

    @staticmethod
    def _untrusted(signal: NewsSignal) -> UntrustedText:
        when = signal.published_at.isoformat() if signal.published_at else "unknown time"
        body = f"[{signal.tier.name}, {when}] {signal.headline}"
        if signal.summary:
            body += f" — {signal.summary[:500]}"
        return UntrustedText(source=signal.tier.name, url=signal.urls[0] if signal.urls else "",
                             text=body)

    def _correlation_to_book(self, scan, now: datetime) -> float:
        """Max |corr| of the candidate's daily returns against every held name
        (§38). Holdings whose bars cannot be fetched count as fully correlated
        (see position_sizing/correlation.py)."""
        held = [s for s in self.ledger.positions if s != scan.symbol]
        series: dict[str, list[float]] = {}
        for sym in held:
            try:
                bars = self.market_data.bars(sym, now, days=40, received_at=now)
                series[sym] = _daily_returns([b.bar.close for b in bars])
            except MarketDataUnavailable:
                series[sym] = []
        corr = max_abs_correlation_to_book(
            _daily_returns([b.close for b in scan.bars]), series)
        self._book_correlation[scan.symbol] = corr
        return corr

    def _decision_time(self, now: datetime, quote: Quote) -> datetime:
        """`now`, advanced to the quote's own timestamp when the quote is at most
        `quote_sampling_lag` ahead of it. A quote further in the future keeps the
        original `now`, so the integrity engine still flags it."""
        if now < quote.ts <= now + self.quote_sampling_lag:
            return quote.ts
        return now

    def _spread_pct(self, symbol: str, now: datetime) -> float:
        quote = self.market_data.quote(symbol, now)
        return quote.spread / quote.mid if quote.mid > 0 else 1.0
