"""
Tests for rejecting Direct Message installs (services/slack_oauth.py).

We require brands to install INFLUENCE Bot to a channel, not a DM, so the
whole brand team sees campaign notifications. handle_oauth_callback() must
raise InstallTargetError and persist nothing when the brand picked a DM.

Run with `python -m pytest tests/test_install_target.py`, or directly with
`python tests/test_install_target.py`.
"""

import os
import sys
import tempfile

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(_DB_FD)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.models import SessionLocal, SlackInstallation, init_db  # noqa: E402
from services import slack_oauth  # noqa: E402
from services.slack_oauth import (  # noqa: E402
    InstallTargetError,
    _is_dm_channel,
    handle_oauth_callback,
)

init_db()


# --- _is_dm_channel -----------------------------------------------------------

def test_dm_channel_id_is_a_dm():
    assert _is_dm_channel("D0123ABCD")


def test_public_channel_id_is_not_a_dm():
    assert not _is_dm_channel("C0123ABCD")


def test_private_channel_id_is_not_a_dm():
    # "G" ids can be private channels, so we deliberately allow them.
    assert not _is_dm_channel("G0123ABCD")


def test_empty_or_missing_channel_id_is_not_a_dm():
    assert not _is_dm_channel("")
    assert not _is_dm_channel(None)


# --- handle_oauth_callback rejects DM installs --------------------------------

def _patch_oauth(monkeypatch_targets, channel_id):
    """Stub out config/state/token-exchange so we can drive the callback."""
    slack_oauth._require_config = lambda: None
    slack_oauth._verify_state = lambda state: {"brand": "reve"}
    slack_oauth._exchange_code_for_token = lambda code: {
        "team": {"id": "T_TEST", "name": "Reve AI"},
        "access_token": "xoxb-test",
        "bot_user_id": "U_TEST",
        "scope": "chat:write,incoming-webhook",
        "incoming_webhook": {
            "channel_id": channel_id,
            "channel": "Directmessage" if channel_id.startswith("D") else "#general",
            "url": "https://hooks.slack.com/services/T/X/Y",
        },
        "authed_user": {"id": "U_ADMIN"},
    }
    # Track original callables so the caller can restore them.
    return monkeypatch_targets


def _restore_oauth(originals):
    slack_oauth._require_config = originals["_require_config"]
    slack_oauth._verify_state = originals["_verify_state"]
    slack_oauth._exchange_code_for_token = originals["_exchange_code_for_token"]


def _snapshot_originals():
    return {
        "_require_config": slack_oauth._require_config,
        "_verify_state": slack_oauth._verify_state,
        "_exchange_code_for_token": slack_oauth._exchange_code_for_token,
    }


def _count_installs_for_team(team_id):
    db = SessionLocal()
    try:
        return db.query(SlackInstallation).filter_by(team_id=team_id).count()
    finally:
        db.close()


def test_dm_install_raises_and_persists_nothing():
    originals = _snapshot_originals()
    _patch_oauth(originals, channel_id="D0123ABCD")
    try:
        raised = False
        try:
            handle_oauth_callback(code="c", state="s")
        except InstallTargetError:
            raised = True
        assert raised, "expected InstallTargetError for a DM install"
        # Nothing should have been written for this team.
        assert _count_installs_for_team("T_TEST") == 0
    finally:
        _restore_oauth(originals)


def test_channel_install_succeeds():
    originals = _snapshot_originals()
    _patch_oauth(originals, channel_id="C0123ABCD")
    try:
        install = handle_oauth_callback(code="c", state="s")
        assert install is not None
        assert install.channel_id == "C0123ABCD"
        assert install.team_id == "T_TEST"
    finally:
        # Clean up the row this test created, then restore stubs.
        db = SessionLocal()
        try:
            db.query(SlackInstallation).filter_by(team_id="T_TEST").delete()
            db.commit()
        finally:
            db.close()
        _restore_oauth(originals)


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    os.remove(_DB_PATH)
    if failures:
        print(f"\n{failures} test(s) failed")
        sys.exit(1)
    print("\nAll tests passed")
