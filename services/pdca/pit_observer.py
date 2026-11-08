"""Records what the Decision AI was shown and what it answered, as Point-in-Time records (§10).

For every decision two records are kept, both stamped with the time WE received them:
  decision_input:<SYMBOL>   the quote, regime, data age, events and news the model was given
  decision_output:<SYMBOL>  the validated decision
so any past decision can be reconstructed "as of" its moment and nothing that arrived later can
leak into it — the basis for auditing and for replaying sessions faithfully. Observation only."""
from __future__ import annotations

from typing import Any, Protocol

from services.pdca.observer import DecisionEvent, SessionObserver

_MAX_TEXT = 2000          # per news item; the full text lives at its URL


class PitSink(Protocol):
    def put(self, key: str, value: Any, event_time, received_time, source: str = ...) -> None: ...


class PitObserver(SessionObserver):
    name = "pit"

    def __init__(self, store: PitSink) -> None:
        self._store = store
        self.recorded = 0

    def on_decision(self, event: DecisionEvent) -> None:
        ctx, d = event.context, event.decision
        q = ctx.quote
        inputs = {
            "decision_id": event.decision_id,
            "last_bar_ts": ctx.scan.bars[-1].ts.isoformat(),
            "quote": None if q is None else {"ts": q.ts.isoformat(), "bid": q.bid, "ask": q.ask,
                                             "bid_size": q.bid_size, "ask_size": q.ask_size,
                                             "source": q.source},
            "regime": ctx.regime,
            "events": list(ctx.upcoming_events), "events_fetched": ctx.events_fetched,
            "news_fetched": ctx.news_fetched,
            "news": [{"source": n.source, "url": n.url, "text": n.text[:_MAX_TEXT]}
                     for n in ctx.news],
        }
        event_time = q.ts if q is not None else event.as_of
        self._store.put(f"decision_input:{event.symbol}", inputs, event_time, event.as_of,
                        source="pipeline")
        self._store.put(f"decision_output:{event.symbol}", d.model_dump(mode="json"),
                        event.as_of, event.as_of, source=d.decision_version)
        self.recorded += 1

    def summary(self) -> dict[str, Any]:
        return {"decisions_recorded": self.recorded}
