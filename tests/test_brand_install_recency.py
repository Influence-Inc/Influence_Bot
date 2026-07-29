"""
Regression tests for DB-backed brand_routing lookups.

1. When multiple SlackInstallation rows match the same brand (e.g. several
   dummy test workspaces installed under the same brand slug before the real
   one installed), find_install_for_brand_name() must return the most
   recently installed one — not whichever the database happens to return
   first with no ordering, which could permanently shadow the real
   workspace with a stale test install.

2. find_install_by_team() must skip deactivated installs (brand and
   team_name both blanked via manage_slack_installs.py's `deactivate`).
   Otherwise a decommissioned/duplicate row sitting under a workspace's own
   team_id — including the INFLUENCE admin home workspace, if a stray brand
   install was ever created there by mistake — permanently makes that
   workspace look like "a brand workspace" and locks it out of admin-only
   slash commands, even after the row's brand identity is cleared.

This test touches a real (scratch, file-based) SQLite database, unlike
test_brand_routing.py which only exercises the pure matching predicate.

Run with `python -m pytest tests/test_brand_install_recency.py`, or directly
with `python tests/test_brand_install_recency.py`.
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(_DB_FD)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.models import SessionLocal, SlackInstallation, init_db  # noqa: E402
from services.brand_routing import find_install_by_team, find_install_for_brand_name  # noqa: E402

init_db()


def _seed(team_id, team_name, brand, installed_at):
    db = SessionLocal()
    try:
        db.add(SlackInstallation(
            team_id=team_id,
            team_name=team_name,
            brand=brand,
            bot_token="xoxb-test",
            installed_at=installed_at,
        ))
        db.commit()
    finally:
        db.close()


def _clear():
    db = SessionLocal()
    try:
        db.query(SlackInstallation).delete()
        db.commit()
    finally:
        db.close()


def test_prefers_most_recent_install_when_multiple_match():
    _clear()
    now = datetime.now(timezone.utc)
    _seed("T_TEST1", "Dummy Test Workspace 1", "reve", now - timedelta(days=10))
    _seed("T_TEST2", "Dummy Test Workspace 2", "reve", now - timedelta(days=5))
    _seed("T_REAL", "REVE AI", "reve", now)

    found = find_install_for_brand_name("Reve")
    assert found is not None
    assert found.team_id == "T_REAL"


def test_prefers_most_recent_regardless_of_insertion_order():
    # The real workspace row is inserted FIRST (lowest primary key) but has
    # the newest installed_at; a dummy row is inserted after but backdated.
    # This guards against a fix that accidentally sorts by id/insertion
    # order instead of installed_at.
    _clear()
    now = datetime.now(timezone.utc)
    _seed("T_REAL", "REVE AI", "reve", now)
    _seed("T_TEST1", "Dummy Test Workspace 1", "reve", now - timedelta(days=10))

    found = find_install_for_brand_name("Reve")
    assert found is not None
    assert found.team_id == "T_REAL"


def test_find_install_by_team_skips_deactivated_install():
    # A deactivated install (brand + team_name both blank) must not make its
    # workspace look like a brand workspace — e.g. the admin home workspace,
    # if a stray brand install once got created there and was later
    # deactivated rather than deleted (deleting can fail on a ChatSpace
    # foreign key), must see itself as "not a brand workspace" again.
    _clear()
    now = datetime.now(timezone.utc)
    _seed("T_ADMIN", None, None, now)

    assert find_install_by_team("T_ADMIN") is None


def test_find_install_by_team_falls_through_to_older_active_install():
    _clear()
    now = datetime.now(timezone.utc)
    _seed("T_SAME", "Some Brand", "somebrand", now - timedelta(days=10))
    _seed("T_SAME", None, None, now)  # newer, but deactivated

    found = find_install_by_team("T_SAME")
    assert found is not None
    assert found.brand == "somebrand"


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
