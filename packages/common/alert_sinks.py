"""Where alerts actually go (SAFETY_AUDIT F10): a webhook, e-mail, a history file, or all of them.

`packages/common/alerts.py` defines the one-method `AlertSink` interface and the guarded `notify`; this
module holds the real destinations. Choices that matter for a safety path:

- Configuration comes from the ENVIRONMENT only (a webhook URL and an SMTP password are secrets): nothing is
  stored in the repo, and neither is ever written to a log or an exception message.
- Webhook URLs must be https (plain http only for the loopback address, so it can be tested).
- A sink that cannot deliver raises; `notify` catches that and logs it — a broken pager must never break the
  safety path that raised the alert. Delivery failures are remembered (`last_error`) so they are visible.
- Identical alerts inside a short window are sent once (a flapping fault must not flood a channel).
- `CompositeAlertSink` tries EVERY destination: one failing does not stop the others, and a failure in any is
  re-raised afterwards so the failure is not silent.
- With nothing configured the logging sink is still used: alerts are never dropped on the floor.

Environment:
  QUANT_ALERT_WEBHOOK_URL              Slack / Discord / Teams incoming-webhook URL (https)
  QUANT_ALERT_EMAIL_TO                 comma-separated recipients
  QUANT_SMTP_HOST, QUANT_SMTP_PORT (587), QUANT_SMTP_USER, QUANT_SMTP_PASSWORD, QUANT_SMTP_FROM,
  QUANT_SMTP_STARTTLS (default 1)
  QUANT_ALERT_PREFIX                   text put before every message, e.g. the host or account name"""
from __future__ import annotations

import json
import logging
import os
import smtplib
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Callable, Optional, Sequence

from packages.common.alerts import AlertSeverity, AlertSink, LoggingAlertSink

log = logging.getLogger("quant.alerts")
_MAX_MESSAGE = 1500
_DEDUPE_WINDOW_SEC = 60.0
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class AlertDeliveryError(RuntimeError):
    """An alert could not be delivered. Never carries a URL or a credential."""


_USER_AGENT = "quant-alerts/1.0"


def _clip(text: str) -> str:
    return text if len(text) <= _MAX_MESSAGE else text[:_MAX_MESSAGE - 1] + "…"


class _Dedupe:
    def __init__(self, window: float, clock: Callable[[], float]) -> None:
        self._window, self._clock, self._seen = window, clock, {}

    def is_repeat(self, key: str) -> bool:
        now = self._clock()
        self._seen = {k: t for k, t in self._seen.items() if now - t < self._window}
        if key in self._seen:
            return True
        self._seen[key] = now
        return False


class WebhookAlertSink:
    """POSTs JSON that Slack ("text"), Discord ("content") and Teams ("text") incoming webhooks accept."""

    def __init__(self, url: str, *, prefix: str = "", timeout: float = 10.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        parts = urllib.parse.urlsplit(url)
        if not (parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in _LOOPBACK)):
            raise ValueError("the alert webhook must be an https URL")
        if not parts.hostname:
            raise ValueError("the alert webhook URL has no host")
        self._url, self._prefix, self._timeout = url, prefix, timeout
        self._dedupe = _Dedupe(_DEDUPE_WINDOW_SEC, clock)
        self.last_error: Optional[str] = None

    def send(self, severity: AlertSeverity, message: str) -> None:
        if self._dedupe.is_repeat(f"{severity.value}|{message}"):
            return
        text = _clip(f"{self._prefix}[{severity.value}] {message}".strip())
        body = json.dumps({"text": text, "content": text, "severity": severity.value, "message": _clip(message),
                           "at": datetime.now(timezone.utc).isoformat()}).encode()
        # Discord (behind Cloudflare) answers 403 / error 1010 to urllib's default "Python-urllib/x.y" agent, so a
        # generic client name is sent. It carries no contact details (SEC EDGAR is the one place that needs those).
        req = urllib.request.Request(self._url, data=body,
                                     headers={"Content-Type": "application/json", "User-Agent": _USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                resp.read(256)
            self.last_error = None
        except urllib.error.HTTPError as e:
            self.last_error = f"webhook HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            self.last_error = f"webhook request failed ({type(e).__name__})"
        if self.last_error:
            raise AlertDeliveryError(self.last_error)


class EmailAlertSink:
    def __init__(self, host: str, to: Sequence[str], sender: str, *, port: int = 587, user: str = "",
                 password: str = "", starttls: bool = True, prefix: str = "", timeout: float = 15.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if not to:
            raise ValueError("e-mail alerts need at least one recipient")
        self._cfg = dict(host=host, port=port, user=user, password=password, starttls=starttls)
        self._to, self._from, self._prefix, self._timeout = list(to), sender, prefix, timeout
        self._dedupe = _Dedupe(_DEDUPE_WINDOW_SEC, clock)
        self.last_error: Optional[str] = None

    def send(self, severity: AlertSeverity, message: str) -> None:
        if self._dedupe.is_repeat(f"{severity.value}|{message}"):
            return
        msg = EmailMessage()
        msg["Subject"] = _clip(f"{self._prefix}[{severity.value}] {message}".strip()).splitlines()[0][:150]
        msg["From"], msg["To"] = self._from, ", ".join(self._to)
        msg.set_content(f"{_clip(message)}\n\nat {datetime.now(timezone.utc).isoformat()}\n")
        try:
            with smtplib.SMTP(self._cfg["host"], self._cfg["port"], timeout=self._timeout) as smtp:
                if self._cfg["starttls"]:
                    smtp.starttls()
                if self._cfg["user"]:
                    smtp.login(self._cfg["user"], self._cfg["password"])
                smtp.send_message(msg)
            self.last_error = None
        except (smtplib.SMTPException, OSError) as e:
            self.last_error = f"e-mail delivery failed ({type(e).__name__})"
            raise AlertDeliveryError(self.last_error) from None


class FileAlertSink:
    """Append-only history of every alert (what the ops dashboard shows). Never dedupes: it is the record."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def send(self, severity: AlertSeverity, message: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"at": datetime.now(timezone.utc).isoformat(), "severity": severity.value,
                           "message": _clip(message)})
        with self.path.open("a") as f:
            f.write(line + "\n")


def read_alert_history(path: Path | str, limit: int = 50) -> list[dict]:
    """The newest `limit` alerts, newest first. A missing file is an empty history; bad lines are skipped."""
    try:
        lines = Path(path).read_text().splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
        if len(out) >= limit:
            break
    return out


class CompositeAlertSink:
    def __init__(self, sinks: Sequence[AlertSink]) -> None:
        self._sinks = list(sinks)

    def send(self, severity: AlertSeverity, message: str) -> None:
        failures = []
        for sink in self._sinks:
            try:
                sink.send(severity, message)
            except Exception as e:  # noqa: BLE001 — try every destination, then report
                failures.append(f"{type(sink).__name__}: {e}")
        if failures:
            raise AlertDeliveryError("; ".join(failures))


def alert_sink_from_env(history_path: Path | str | None = None,
                        env: Optional[dict[str, str]] = None) -> CompositeAlertSink:
    """Logging always; plus a history file, a webhook and/or e-mail when configured. Raises ValueError for a
    configuration that cannot work (e.g. an http webhook), so a typo is found at start-up, not at 3 a.m."""
    env = dict(os.environ if env is None else env)
    prefix = env.get("QUANT_ALERT_PREFIX", "").strip()
    prefix = f"{prefix} " if prefix else ""
    sinks: list[AlertSink] = [LoggingAlertSink()]
    if history_path is not None:
        sinks.append(FileAlertSink(history_path))
    if env.get("QUANT_ALERT_WEBHOOK_URL"):
        sinks.append(WebhookAlertSink(env["QUANT_ALERT_WEBHOOK_URL"].strip(), prefix=prefix))
    to = [x.strip() for x in env.get("QUANT_ALERT_EMAIL_TO", "").split(",") if x.strip()]
    if to:
        host, sender = env.get("QUANT_SMTP_HOST", "").strip(), env.get("QUANT_SMTP_FROM", "").strip()
        if not host or not sender:
            raise ValueError("QUANT_ALERT_EMAIL_TO needs QUANT_SMTP_HOST and QUANT_SMTP_FROM")
        sinks.append(EmailAlertSink(
            host, to, sender, port=int(env.get("QUANT_SMTP_PORT", "587")), user=env.get("QUANT_SMTP_USER", ""),
            password=env.get("QUANT_SMTP_PASSWORD", ""), starttls=env.get("QUANT_SMTP_STARTTLS", "1") != "0",
            prefix=prefix))
    return CompositeAlertSink(sinks)


def describe_sink(sink: CompositeAlertSink) -> str:
    """Which destinations are active, for the start-up log (names only — no URL, no address)."""
    names = [type(s).__name__.replace("AlertSink", "") for s in sink._sinks]
    return ", ".join(names)
