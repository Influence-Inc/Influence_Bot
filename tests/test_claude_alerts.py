"""
Tests for the Claude credits alert (utils/claude_alerts.py): which Anthropic
errors alert the Slack channel, how often, and that sending never raises.

Run with `python -m pytest tests/test_claude_alerts.py`.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from utils import claude_alerts  # noqa: E402


class FakeError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status_code = status


@pytest.fixture
def slack(monkeypatch, tmp_path):
    sent = []

    class Reply:
        def raise_for_status(self):
            return None

    def post(url, json, timeout):
        sent.append(json["text"])
        return Reply()

    monkeypatch.setenv(claude_alerts.WEBHOOK_ENV, "https://hooks.slack.com/services/T/B/x")
    monkeypatch.setattr(claude_alerts, "MARKERS", tmp_path)
    monkeypatch.setattr(claude_alerts.requests, "post", post)
    return sent


def test_low_credit_balance_alerts_once(slack):
    err = FakeError(400, "Your credit balance is too low to access the Anthropic API.")
    assert claude_alerts.report(err) and claude_alerts.report(err)
    assert len(slack) == 1
    assert ": out of credits*" in slack[0] and "credit balance is too low" in slack[0]


def test_a_rejected_key_alerts(slack):
    assert claude_alerts.report(FakeError(401, "invalid x-api-key"))
    assert ": key refused*" in slack[0]


@pytest.mark.parametrize(
    "err",
    [FakeError(429, "rate_limit_error"), FakeError(529, "Overloaded"), ConnectionError("reset")],
)
def test_passing_errors_say_nothing(slack, err):
    assert claude_alerts.report(err) is False and slack == []


def test_nothing_without_a_webhook(monkeypatch):
    monkeypatch.delenv(claude_alerts.WEBHOOK_ENV, raising=False)
    assert claude_alerts.notify("k", "x") is False


def test_a_failing_webhook_never_raises(slack, monkeypatch):
    def down(*_a, **_k):
        raise OSError("slack down")

    monkeypatch.setattr(claude_alerts.requests, "post", down)
    assert claude_alerts.notify("k", "x") is False
