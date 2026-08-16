"""End-to-end trading pipeline (MASTER SPEC §6, §7, §107).

Wires: Universe → Scanner → Decision AI → Skeptic → Loss Control →
Position Sizing → Capital Allocation → Master Risk Controller →
Execution → Paper Broker → Ledger/Provenance.

The pipeline itself is deterministic glue.  Its result object always explains
NO TRADE outcomes with the funnel numbers (§93: 故障ではなく理由表示).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from packages.common.clock import Clock
from packages.common.environment import Environment
from packages.common.ledger import Ledger
from packages.common.provenance import ProvenanceStore
from packages.schemas.core import (
    Action,
    BrokerFill,
    OrderIntent,
    OrderState,
    OrderType,
    ProposalSource,
    RiskApproval,
    RiskApprovedOrder,
    RiskRejection,
    SizedProposal,
    StopPlan,
    TradeProposal,
)
from packages.schemas.audit import ApprovedOrderSnapshot, AuditVerdict
from services.capital_allocation.engine import CapitalAllocationEngine
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
)
from services.execution.engine import ExecutionEngine, make_client_order_id
from services.pdca.audit_log import NearMissKind, PreTradeAuditLog, Stage
from services.pdca.post_trade import PostTradeTracker
from services.pdca.decision_quality import (
    DecisionKind,
    DecisionQualityEngine,
    DecisionSnapshot,
)
from services.loss_control.engine import LossControlEngine, NoStopPlanError
from services.market_data.service import MarketDataService
from services.market_data.universe import UniverseManager
from services.position_sizing.engine import (
    PortfolioContext,
    PositionSizingEngine,
    SizingRejected,
)
from services.quant.scanner import QuantScanner
from services.risk.gap_risk import gap_risk_score
from services.risk.master_controller import (
    MasterRiskController,
    PortfolioRiskView,
    throttle_factor,
    throttle_level,
)


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
    sized: int = 0
    allocated: int = 0
    audit_passed: int = 0
    audit_rejected: int = 0
    risk_passed: int = 0
    risk_rejected: int = 0
    orders_filled: int = 0
    no_trade_reasons: list[str] = field(default_factory=list)
    fills: list[BrokerFill] = field(default_factory=list)

    @property
    def traded(self) -> bool:
        return self.orders_filled > 0


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
    auditor: IndependentAuditor = field(default_factory=lambda: IndependentAuditor(model=None,
                                                                                  audit_all=False))
    audit_log: PreTradeAuditLog = field(default_factory=PreTradeAuditLog)
    decision_quality: DecisionQualityEngine = field(default_factory=DecisionQualityEngine)
    post_trade: PostTradeTracker = field(default_factory=PostTradeTracker)
    symbol_themes: dict[str, list[str]] = field(default_factory=dict)
    max_new_positions: int = 5
    _order_seq: int = 0
    # symbol -> (protective stop client_order_id, stop plan, entry price, risk amount)
    open_stops: dict[str, tuple[str, StopPlan, float, float]] = field(default_factory=dict)

    def _record_decision(self, decision_id: str, symbol: str, kind: DecisionKind,
                         now, reference_price: float, decision=None,
                         had_stop_plan: bool = False, skeptic_consulted: bool = False,
                         regime: str = "UNKNOWN") -> None:
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
            regime=regime,
            model=self.decision_model.name, model_version=self.decision_model.version,
            rule_compliant=True, had_stop_plan=had_stop_plan,
            skeptic_consulted=skeptic_consulted,
            data_health_ok=self.integrity.health is not DataHealth.HALT_ENTRIES)
        self.decision_quality.record(snap)

    # -- protective exits (§33-34, §40, §43, INV-15) -------------------------
    def _exit_risk_view(self, symbol: str, qty: float, now: datetime) -> PortfolioRiskView:
        prices = self._mark_prices({}, now)
        pos_notional = self._position_notional({}, now)
        snapshot = self.ledger.snapshot(prices)
        return PortfolioRiskView(
            equity=snapshot.equity, settled_cash=self.ledger.cash,
            total_exposure_notional=sum(pos_notional.values()),
            position_notional=pos_notional,
            position_qty={s: l.qty for s, l in self.ledger.positions.items()},
            theme_exposure=self._theme_exposure({}, now),
            symbol_themes=self.symbol_themes, drawdown=snapshot.drawdown,
            stop_plan_exists=True, gap_risk_score=0.0, adv_shares=1_000_000,
            correlation_to_book=0.0, reconciliation_ok=True,
            data_health=self.integrity.health, broker_connected=True)

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
        self.open_stops[symbol] = (intent.client_order_id, stop, stop.entry_price,
                                   qty * stop.stop_distance)
        return True

    def manage_open_positions(self, now: datetime) -> tuple[int, int]:
        """Session-start exit management: let resting stops trigger, book the
        fills, and feed the learning loops.  Returns (stops_triggered, synced)."""
        fills = self.execution.sync_open_orders()
        triggered = 0
        for f in fills:
            if f.side is not Action.SELL:
                # a resting BUY (e.g. a limit entry) that fills later must still
                # reach the ledger, or ledger and broker drift apart (§48)
                self.ledger.record_fill(f.symbol, f.qty, f.price, f.fees, f.ts,
                                        note=f.client_order_id)
                continue
            self.ledger.record_fill(f.symbol, -f.qty, f.price, f.fees, f.ts,
                                    note=f.client_order_id)
            entry = self.open_stops.get(f.symbol)
            realized = 0.0
            if entry is not None:
                _, stop, entry_px, risk_amount = entry
                realized = (f.price - entry_px) * f.qty
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
        result = PipelineResult()
        now = as_of

        # 0. Exit management first: resting protective stops may have triggered
        #    since the last session (§33-34, §43)
        result.stops_triggered, _ = self.manage_open_positions(now)

        # 1. Universe + Scanner (§12, §21)
        symbols = self.universe.symbols_as_of(now.date())
        scans = self.scanner.scan(symbols, now)
        result.scanned = self.scanner.last_funnel.scanned
        result.candidates = self.scanner.last_funnel.passed_advanced
        if not scans:
            result.no_trade_reasons.append("scanner produced no candidates")
            return result

        # data integrity gate (§11)
        for s in scans:
            self.integrity.validate_bars(s.symbol, list(s.bars))
            self.integrity.validate_quote(self.market_data.quote(s.symbol, now), now)

        # 2. Decision AI + Skeptic (§27-29) — proposals only, no broker access
        sized_candidates: list[SizedProposal] = []
        prices: dict[str, float] = {}
        for scan in scans[: self.max_new_positions * 3]:
            ctx = DecisionContext(scan=scan, portfolio_summary={"cash": self.ledger.cash})
            rec = self.provenance.open(decision_id=f"{scan.symbol}-{now.date()}")
            rec.model = self.decision_model.name
            rec.model_version = self.decision_model.version
            rec.input_features = {"momentum_20d": scan.momentum_20d,
                                  "volatility": scan.volatility,
                                  "dollar_volume": scan.dollar_volume}
            rec.data_timestamps = {"last_bar": scan.bars[-1].ts.isoformat()}
            try:
                decision = validate_decision(self.decision_model.decide(ctx))
            except MalformedDecisionError as e:
                result.no_trade_reasons.append(f"{scan.symbol}: malformed decision rejected (§28)")
                rec.result = {"rejected": "malformed decision", "error": str(e)[:200]}
                continue
            rec.output = decision.model_dump(mode="json")
            if decision.action is not Action.BUY:
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

            # 3. Loss Control BEFORE sizing (§33)
            quote = self.market_data.quote(scan.symbol, now)
            entry_price = quote.ask
            prices[scan.symbol] = quote.mid
            gap = gap_risk_score(list(scan.bars))
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
            snapshot = self.ledger.snapshot(self._mark_prices(prices, now))
            level = throttle_level(snapshot.drawdown, self.risk_controller.config)
            pctx = PortfolioContext(
                equity=snapshot.equity, settled_cash=self.ledger.cash,
                existing_exposure=self._position_notional(prices, now),
                theme_exposure=self._theme_exposure(prices, now),
                symbol_themes=self.symbol_themes,
                adv_shares=sum(b.volume for b in scan.bars[-20:]) / 20,
                throttle_factor=throttle_factor(level, self.risk_controller.config))
            calibrated = self.calibration.calibrate(decision.confidence)
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
        snapshot = self.ledger.snapshot(self._mark_prices(prices, now))
        alloc = self.allocation.allocate(sized_candidates, equity=snapshot.equity,
                                         settled_cash=self.ledger.cash,
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
                audit = self.auditor.audit(sized.proposal.decision, sized, intent,
                                           AuditContext(now=now))
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
                result.audit_rejected += 1
                result.no_trade_reasons.append(
                    f"{symbol}: audit {audit.verdict.value} — {'; '.join(audit.reasons)}")
                self.audit_log.record_audit_rejection(log_rec.audit_result, now,
                                                      rec.decision_id,
                                                      intent.client_order_id)
                self._record_decision(rec.decision_id, symbol, DecisionKind.AVOID, now,
                                      sized.stop_plan.entry_price,
                                      decision=sized.proposal.decision,
                                      had_stop_plan=True, skeptic_consulted=True)
                continue
            result.audit_passed += 1

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
            snapshot = ApprovedOrderSnapshot.from_approved(
                approved, decision_id=rec.decision_id, audit_id=audit.audit_id,
                take_profit=sized.stop_plan.profit_target)
            log_rec.approved_snapshot_hash = snapshot.hash
            log_rec.broker_submitted = True
            self._record_decision(rec.decision_id, symbol, DecisionKind.BUY, now,
                                  sized.stop_plan.entry_price,
                                  decision=sized.proposal.decision,
                                  had_stop_plan=True, skeptic_consulted=True)

            state = self.execution.submit(approved, snapshot=snapshot)
            log_rec.final_state = state.value
            rec.order_ref = intent.client_order_id
            if state in (OrderState.FILLED, OrderState.PARTIALLY_FILLED):
                filled_qty = 0.0
                for f in self.execution.broker.get_fills(since=now.replace(year=2000)):
                    if f.client_order_id == intent.client_order_id:
                        filled_qty += f.qty
                        self.ledger.record_fill(f.symbol, f.qty if f.side is Action.BUY else -f.qty,
                                                f.price, f.fees, f.ts,
                                                note=intent.client_order_id)
                        rec.fill_refs.append(f.broker_fill_id)
                        log_rec.fills.append(f.broker_fill_id)
                        result.fills.append(f)
                result.orders_filled += 1
                rec.result = {"state": state.value}
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
                result.no_trade_reasons.append(
                    f"{sized.proposal.symbol}: order ended {state.value}")
        return result

    # -- helpers -------------------------------------------------------------
    def _mark_prices(self, known: dict[str, float], now: datetime) -> dict[str, float]:
        prices = dict(known)
        for sym in self.ledger.positions:
            if sym not in prices:
                prices[sym] = self.market_data.quote(sym, now).mid
        return prices

    def _position_notional(self, prices: dict[str, float], now: datetime) -> dict[str, float]:
        marks = self._mark_prices(prices, now)
        return {sym: lot.qty * marks[sym] for sym, lot in self.ledger.positions.items()}

    def _theme_exposure(self, prices: dict[str, float], now: datetime) -> dict[str, float]:
        exposure: dict[str, float] = {}
        for sym, notional in self._position_notional(prices, now).items():
            for theme in self.symbol_themes.get(sym, []):
                exposure[theme] = exposure.get(theme, 0.0) + notional
        return exposure

    def _risk_view(self, sized: SizedProposal, prices: dict[str, float],
                   now: datetime) -> PortfolioRiskView:
        snapshot = self.ledger.snapshot(self._mark_prices(prices, now))
        pos_notional = self._position_notional(prices, now)
        return PortfolioRiskView(
            equity=snapshot.equity, settled_cash=self.ledger.cash,
            total_exposure_notional=sum(pos_notional.values()),
            position_notional=pos_notional,
            position_qty={s: l.qty for s, l in self.ledger.positions.items()},
            theme_exposure=self._theme_exposure(prices, now),
            symbol_themes=self.symbol_themes,
            drawdown=snapshot.drawdown, stop_plan_exists=True,
            gap_risk_score=sized.stop_plan.gap_risk_score,
            adv_shares=1_000_000, correlation_to_book=0.0,
            reconciliation_ok=True, data_health=self.integrity.health,
            broker_connected=True)
