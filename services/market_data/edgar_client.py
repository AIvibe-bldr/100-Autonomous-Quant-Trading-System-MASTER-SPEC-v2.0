"""SEC EDGAR client (MASTER SPEC §18, tier 1 = primary source). Data only.

SEC needs no API key but requires every request to identify the caller with a
`User-Agent` of the form "<app name> <contact email>" and caps traffic at 10
requests/second. So:

- the identity comes from `QUANT_SEC_USER_AGENT` (set by the operator) — it is
  never hard-coded, defaulted or invented; unset or malformed → EDGAR is simply
  unavailable (fail closed, callers carry on without it);
- requests are throttled well under the cap and results are cached per client.

Failures raise `EdgarUnavailable` (status code only; no URL, no identity)."""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable, Optional

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
ENV_VAR = "QUANT_SEC_USER_AGENT"
_MIN_INTERVAL_SEC = 0.15          # ~6.6 req/s, under SEC's 10/s
_UA_SHAPE = re.compile(r"^\S+(\s+\S+)*\s+\S+@\S+\.\S+$")      # a name, then an email

HttpGet = Callable[[str, dict[str, str]], Any]


class EdgarUnavailable(RuntimeError):
    """EDGAR cannot be used (no/invalid identity, HTTP failure)."""


@dataclass(frozen=True)
class Filing:
    cik: int
    accession: str
    form: str
    filing_date: date
    accepted_at: datetime
    items: tuple[str, ...]          # 8-K item numbers, e.g. ("2.02", "9.01")
    primary_document: str

    @property
    def url(self) -> str:
        return ARCHIVE_URL.format(cik=self.cik, acc=self.accession.replace("-", ""),
                                  doc=self.primary_document)


def _urllib_get(url: str, headers: dict[str, str]) -> Any:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise EdgarUnavailable(f"SEC HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        raise EdgarUnavailable(f"SEC request failed ({type(e).__name__})") from None


class EdgarClient:
    def __init__(self, user_agent: str, *, http_get: Optional[HttpGet] = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if not user_agent or not _UA_SHAPE.match(user_agent.strip()):
            raise EdgarUnavailable(
                f"{ENV_VAR} must be '<app name> <contact email>' (SEC fair-access policy)")
        self._headers = {"User-Agent": user_agent.strip(), "Accept-Encoding": "identity"}
        self._get = http_get or _urllib_get
        self._sleep = sleep
        self._last = 0.0
        self._cik: Optional[dict[str, int]] = None
        self._filings: dict[str, list[Filing]] = {}

    @classmethod
    def from_env(cls, **kw: Any) -> "EdgarClient":
        return cls(os.environ.get(ENV_VAR, ""), **kw)

    def _fetch(self, url: str) -> Any:
        wait = _MIN_INTERVAL_SEC - (time.monotonic() - self._last)
        if wait > 0:
            self._sleep(wait)
        self._last = time.monotonic()
        return self._get(url, self._headers)

    def _cik_for(self, ticker: str) -> Optional[int]:
        if self._cik is None:
            body = self._fetch(TICKERS_URL)
            self._cik = {str(r["ticker"]).upper(): int(r["cik_str"]) for r in body.values()}
        return self._cik.get(ticker.upper())

    def recent_filings(self, ticker: str) -> list[Filing]:
        """The company's recent filings, newest first as SEC lists them. An
        unknown ticker has none ([]); a failure raises (never reads as 'none')."""
        key = ticker.upper()
        if key in self._filings:
            return self._filings[key]
        cik = self._cik_for(key)
        out: list[Filing] = []
        if cik is not None:
            recent = self._fetch(SUBMISSIONS_URL.format(cik=cik))["filings"]["recent"]
            for i, acc in enumerate(recent["accessionNumber"]):
                try:
                    out.append(Filing(
                        cik=cik, accession=acc, form=recent["form"][i],
                        filing_date=date.fromisoformat(recent["filingDate"][i]),
                        accepted_at=datetime.fromisoformat(
                            recent["acceptanceDateTime"][i].replace("Z", "+00:00")
                        ).astimezone(timezone.utc),
                        items=tuple(x.strip() for x in str(recent["items"][i]).split(",") if x.strip()),
                        primary_document=recent["primaryDocument"][i]))
                except (KeyError, IndexError, ValueError, TypeError):
                    continue                                   # one bad row must not hide the rest
        self._filings[key] = out
        return out
