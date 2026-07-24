"""
Tests for brand <-> Slack-workspace matching (services/brand_routing.py).

Focus: a campaign whose brand is "Reve" must reach a workspace named
"REVE AI", while never leaking to unrelated look-alike names like "Revel"
or "Revenue". These exercise the pure matching predicate — no DB needed.

Run with `python -m pytest tests/test_brand_routing.py`, or directly with
`python tests/test_brand_routing.py`.
"""

import os
import sys

# Importing the app modules pulls in config, which reads DATABASE_URL. Default
# it to an in-memory-ish sqlite so the import never touches a real database.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.models import SlackInstallation  # noqa: E402
from services.brand_routing import (  # noqa: E402
    _token_prefix_match,
    _tokens,
    brand_matches_install,
)


def _install(brand=None, team_name=None):
    return SlackInstallation(brand=brand, team_name=team_name)


# --- The reported bug: brand "Reve" must reach the "REVE AI" workspace -------

def test_workspace_name_with_trailing_word_matches():
    # Slug minted to mirror the workspace name ("reve-ai") — used to fail.
    install = _install(brand="reve-ai", team_name="REVE AI")
    assert brand_matches_install("Reve", install)


def test_workspace_name_only_matches_when_slug_absent():
    install = _install(brand=None, team_name="REVE AI")
    assert brand_matches_install("Reve", install)


def test_exact_slug_still_matches():
    install = _install(brand="reve", team_name="REVE AI")
    assert brand_matches_install("Reve", install)


def test_brand_with_company_suffix_matches_slug():
    # ReelStats returns names like "Reve, Inc"; slug is the bare "reve".
    install = _install(brand="reve", team_name="REVE AI")
    assert brand_matches_install("Reve, Inc", install)


def test_longer_brand_name_matches_shorter_workspace():
    install = _install(brand=None, team_name="REVE AI")
    assert brand_matches_install("Reve AI Global", install)


# --- False-positive guards: must NOT match look-alike names ------------------

def test_does_not_match_unrelated_prefix_word():
    assert not brand_matches_install("Reve", _install(team_name="Revel Bakery"))
    assert not brand_matches_install("Reve", _install(team_name="Revenue Co"))


def test_does_not_match_different_brand():
    assert not brand_matches_install("Reve", _install(brand="acme", team_name="Acme Inc"))


def test_empty_brand_never_matches():
    assert not brand_matches_install("", _install(brand="reve", team_name="REVE AI"))
    assert not brand_matches_install(None, _install(brand="reve", team_name="REVE AI"))


def test_none_install_never_matches():
    assert not brand_matches_install("Reve", None)


# --- Token helpers -----------------------------------------------------------

def test_tokens_splits_on_non_alnum():
    assert _tokens("REVE AI") == ["reve", "ai"]
    assert _tokens("Reve, Inc.") == ["reve", "inc"]
    assert _tokens(None) == []


def test_token_prefix_match_is_word_boundary_aware():
    assert _token_prefix_match(["reve"], ["reve", "ai"])
    assert _token_prefix_match(["reve", "ai"], ["reve"])  # symmetric
    assert not _token_prefix_match(["reve"], ["revel"])
    assert not _token_prefix_match(["reve"], ["revenue"])
    assert not _token_prefix_match([], ["reve"])
    assert not _token_prefix_match(["reve"], [])


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
    if failures:
        print(f"\n{failures} test(s) failed")
        sys.exit(1)
    print("\nAll tests passed")
