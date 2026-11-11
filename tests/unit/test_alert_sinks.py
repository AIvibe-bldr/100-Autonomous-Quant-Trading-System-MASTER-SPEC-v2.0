"""Real alert destinations: webhook (against a local server), e-mail (fake SMTP), history file, composite."""
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from packages.common.alert_sinks import (
    AlertDeliveryError,
    CompositeAlertSink,
    EmailAlertSink,
    FileAlertSink,
    WebhookAlertSink,
    alert_sink_from_env,
    describe_sink,
    read_alert_history,
)
from packages.common.alerts import AlertSeverity, notify


class _Hook:
    """A local HTTP server standing in for Slack/Discord/Teams."""

    def __init__(self, status=200):
        self.received, self.status, self.user_agents = [], status, []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                outer.user_agents.append(self.headers.get("User-Agent", ""))
                outer.received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(outer.status)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a):
                pass
        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}/hook/SECRET-TOKEN"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def hook():
    h = _Hook()
    yield h
    h.close()


def test_webhook_posts_json_that_slack_discord_and_teams_all_read(hook):
    sink = WebhookAlertSink(hook.url, prefix="paper-1 ")
    sink.send(AlertSeverity.CRITICAL, "risk state -> HALT_NEW_ENTRIES: broker down")
    (body,) = hook.received
    assert body["text"] == body["content"] == "paper-1 [CRITICAL] risk state -> HALT_NEW_ENTRIES: broker down"
    assert body["severity"] == "CRITICAL" and body["at"]


def test_webhook_sends_its_own_user_agent_because_discord_rejects_the_python_default(hook):
    """Real Discord answered HTTP 403 (Cloudflare error 1010) to urllib's default 'Python-urllib/x.y' agent, so no
    alert ever arrived. A local server cannot reproduce that; the header it receives can be checked."""
    WebhookAlertSink(hook.url).send(AlertSeverity.WARNING, "ua check")
    (agent,) = hook.user_agents
    assert agent and not agent.lower().startswith("python-urllib")
    assert "@" not in agent and "http" not in agent.lower()      # a generic client name: no contact details


@pytest.mark.parametrize("url", ["http://example.com/hook", "ftp://x/y", "https://", "not a url"])
def test_only_https_webhooks_are_accepted(url):
    with pytest.raises(ValueError):
        WebhookAlertSink(url)


def test_identical_alerts_inside_the_window_are_sent_once_but_different_ones_are_not_suppressed(hook):
    t = [0.0]
    sink = WebhookAlertSink(hook.url, clock=lambda: t[0])
    for _ in range(3):
        sink.send(AlertSeverity.WARNING, "same")
    sink.send(AlertSeverity.WARNING, "different")
    assert [b["message"] for b in hook.received] == ["same", "different"]
    t[0] = 61.0
    sink.send(AlertSeverity.WARNING, "same")                                    # the window has passed
    assert len(hook.received) == 3


def test_a_failing_webhook_raises_without_leaking_the_url_and_notify_swallows_it(caplog):
    bad = _Hook(status=500)
    try:
        sink = WebhookAlertSink(bad.url)
        with pytest.raises(AlertDeliveryError) as e:
            sink.send(AlertSeverity.CRITICAL, "x")
        assert "SECRET-TOKEN" not in str(e.value) and "500" in str(e.value) and sink.last_error
        with caplog.at_level(logging.ERROR):
            notify(sink, AlertSeverity.CRITICAL, "y")                           # must not raise into trading
        assert "SECRET-TOKEN" not in caplog.text
    finally:
        bad.close()


def test_an_unreachable_webhook_is_a_delivery_error_not_a_crash():
    sink = WebhookAlertSink("http://127.0.0.1:9/hook", timeout=1.0)             # nothing listens on port 9
    with pytest.raises(AlertDeliveryError, match="request failed"):
        sink.send(AlertSeverity.CRITICAL, "x")


class _FakeSMTP:
    sent, logins, tls = [], [], []
    fail = False

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        self.tls.append(self.host)

    def login(self, user, password):
        self.logins.append((user, password))

    def send_message(self, msg):
        import smtplib
        if self.fail:
            raise smtplib.SMTPException("nope")
        self.sent.append(msg)


def test_email_sink_sends_with_starttls_and_login_and_puts_the_alert_in_the_subject(monkeypatch):
    monkeypatch.setattr("packages.common.alert_sinks.smtplib.SMTP", _FakeSMTP)
    _FakeSMTP.sent, _FakeSMTP.logins, _FakeSMTP.tls = [], [], []
    EmailAlertSink("smtp.example.org", ["a@example.org", "b@example.org"], "quant@example.org",
                   user="u", password="pw", prefix="acct ").send(AlertSeverity.CRITICAL, "runner stopped\nmore")
    (msg,) = _FakeSMTP.sent
    assert msg["Subject"] == "acct [CRITICAL] runner stopped" and msg["To"] == "a@example.org, b@example.org"
    assert _FakeSMTP.logins == [("u", "pw")] and _FakeSMTP.tls == ["smtp.example.org"]


def test_email_failure_is_a_delivery_error_that_does_not_carry_the_password(monkeypatch):
    monkeypatch.setattr("packages.common.alert_sinks.smtplib.SMTP", _FakeSMTP)
    _FakeSMTP.fail = True
    try:
        with pytest.raises(AlertDeliveryError) as e:
            EmailAlertSink("h", ["a@x.org"], "f@x.org", password="TOPSECRET").send(AlertSeverity.WARNING, "m")
        assert "TOPSECRET" not in str(e.value)
    finally:
        _FakeSMTP.fail = False
    with pytest.raises(ValueError):
        EmailAlertSink("h", [], "f@x.org")


def test_history_file_records_everything_and_reads_back_newest_first(tmp_path):
    f = FileAlertSink(tmp_path / "alerts.jsonl")
    for i in range(5):
        f.send(AlertSeverity.WARNING, f"m{i}")
    assert [a["message"] for a in read_alert_history(f.path, limit=3)] == ["m4", "m3", "m2"]
    f.path.write_text(f.path.read_text() + "{garbage\n")
    assert read_alert_history(f.path)[0]["message"] == "m4"                     # a bad line is skipped
    assert read_alert_history(tmp_path / "none.jsonl") == []


def test_composite_tries_every_destination_and_still_reports_the_failure(tmp_path):
    class Boom:
        def send(self, s, m):
            raise RuntimeError("down")
    f = FileAlertSink(tmp_path / "a.jsonl")
    with pytest.raises(AlertDeliveryError, match="Boom"):
        CompositeAlertSink([Boom(), f]).send(AlertSeverity.CRITICAL, "x")
    assert read_alert_history(f.path)[0]["message"] == "x"                      # the others still got it


def test_from_env_always_logs_and_adds_only_what_is_configured(tmp_path, hook):
    assert describe_sink(alert_sink_from_env(env={})) == "Logging"
    s = alert_sink_from_env(tmp_path / "h.jsonl", env={"QUANT_ALERT_WEBHOOK_URL": hook.url,
                                                       "QUANT_ALERT_PREFIX": "paper"})
    assert describe_sink(s) == "Logging, File, Webhook"
    s.send(AlertSeverity.CRITICAL, "boom")
    assert hook.received[0]["text"] == "paper [CRITICAL] boom"
    with pytest.raises(ValueError):
        alert_sink_from_env(env={"QUANT_ALERT_WEBHOOK_URL": "http://insecure.example.org/x"})
    with pytest.raises(ValueError, match="SMTP_HOST"):
        alert_sink_from_env(env={"QUANT_ALERT_EMAIL_TO": "a@x.org"})
    assert "Email" in describe_sink(alert_sink_from_env(env={
        "QUANT_ALERT_EMAIL_TO": "a@x.org", "QUANT_SMTP_HOST": "h", "QUANT_SMTP_FROM": "f@x.org"}))
