"""Raised when a current quote cannot be obtained (market closed, bid/ask 0,
feed outage). Lives in packages/common so that both the market-data service
(`MarketDataUnavailable` derives from it) and broker adapters can name the
condition without depending on `services/*` (CLAUDE.md §4)."""


class QuoteUnavailableError(RuntimeError):
    pass
