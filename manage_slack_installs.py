"""
List and remove SlackInstallation rows (per-brand Slack workspace installs).

Use this to clean up test/dummy installs — e.g. several throwaway workspaces
that were all installed under the same brand slug while testing — so brand
matching (services.brand_routing.find_install_for_brand_name) can't land on
a stale test workspace instead of the real one.

For a read-only view, the `/influence-installs [brand]` Slack command (admin
workspace only) lists every install and can test where a brand name would
route without touching the database — reach for this script only when you
need to actually delete or deactivate a row.

Run against whichever DATABASE_URL is in the environment. On Railway:

    railway run python manage_slack_installs.py list
    railway run python manage_slack_installs.py delete <id>
    railway run python manage_slack_installs.py delete-team <team_id>
    railway run python manage_slack_installs.py deactivate <id>

`delete`, `delete-team`, and `deactivate` all print the row(s) first and ask
for a y/n confirmation before writing anything.

A note on `delete`: `ChatSpace.brand_install_id` is a real foreign key to
`slack_installations.id` with no cascade. If a chat space was ever opened
against this install (even during testing), Postgres will refuse the delete
with `... violates foreign key constraint "chat_spaces_brand_install_id_fkey"`.
When that happens, use `deactivate <id>` instead — it blanks out the install's
`brand` and `team_name` so brand matching can never select it again, without
deleting the row (so the chat history it's referenced from stays intact).
"""

import argparse
import sys

from models.models import SessionLocal, SlackInstallation


def _fmt(install: SlackInstallation) -> str:
    return (
        f"id={install.id}  brand={install.brand!r}  "
        f"team_name={install.team_name!r}  team_id={install.team_id}  "
        f"channel={install.channel_name!r}  installed_at={install.installed_at}"
    )


def list_installs() -> int:
    db = SessionLocal()
    try:
        installs = db.query(SlackInstallation).order_by(SlackInstallation.installed_at.desc()).all()
        if not installs:
            print("No Slack installs found.")
            return 0
        for install in installs:
            print(_fmt(install))
        return 0
    finally:
        db.close()


def _confirm(prompt: str) -> bool:
    reply = input(f"{prompt} [y/N] ").strip().lower()
    return reply in ("y", "yes")


def delete_by_id(install_id: int) -> int:
    db = SessionLocal()
    try:
        install = db.query(SlackInstallation).get(install_id)
        if install is None:
            print(f"error: no install with id={install_id}", file=sys.stderr)
            return 1
        print(_fmt(install))
        if not _confirm("Delete this install?"):
            print("Aborted.")
            return 1
        db.delete(install)
        db.commit()
        print(f"Deleted install id={install_id}.")
        return 0
    finally:
        db.close()


def delete_by_team(team_id: str) -> int:
    db = SessionLocal()
    try:
        installs = db.query(SlackInstallation).filter_by(team_id=team_id).all()
        if not installs:
            print(f"error: no installs with team_id={team_id}", file=sys.stderr)
            return 1
        for install in installs:
            print(_fmt(install))
        if not _confirm(f"Delete {len(installs)} install(s) for team_id={team_id}?"):
            print("Aborted.")
            return 1
        for install in installs:
            db.delete(install)
        db.commit()
        print(f"Deleted {len(installs)} install(s) for team_id={team_id}.")
        return 0
    finally:
        db.close()


def deactivate_by_id(install_id: int) -> int:
    """
    Blank out `brand` and `team_name` on an install so it can never be
    matched to a brand again — the row (and anything that references it,
    like a ChatSpace.brand_install_id) stays intact. Use this when `delete`
    fails with the chat_spaces foreign-key error above.
    """
    db = SessionLocal()
    try:
        install = db.query(SlackInstallation).get(install_id)
        if install is None:
            print(f"error: no install with id={install_id}", file=sys.stderr)
            return 1
        print(_fmt(install))
        if not _confirm(
            "Clear brand + team_name on this install (row stays, but it will "
            "never match a brand again)?"
        ):
            print("Aborted.")
            return 1
        install.brand = None
        install.team_name = None
        db.commit()
        print(f"Deactivated install id={install_id} (brand/team_name cleared).")
        return 0
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List every Slack install (newest first)")

    p_delete = sub.add_parser("delete", help="Delete one install by its id")
    p_delete.add_argument("id", type=int)

    p_delete_team = sub.add_parser(
        "delete-team", help="Delete every install for a Slack team_id"
    )
    p_delete_team.add_argument("team_id")

    p_deactivate = sub.add_parser(
        "deactivate",
        help="Blank out brand+team_name on one install (use when delete hits a foreign-key error)",
    )
    p_deactivate.add_argument("id", type=int)

    args = parser.parse_args()

    if args.cmd == "list":
        return list_installs()
    if args.cmd == "delete":
        return delete_by_id(args.id)
    if args.cmd == "delete-team":
        return delete_by_team(args.team_id)
    if args.cmd == "deactivate":
        return deactivate_by_id(args.id)
    return 1


if __name__ == "__main__":
    sys.exit(main())
