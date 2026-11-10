"""Offline account administration; passwords are read with getpass, never argv.

Examples:
  python -m headbop.manage users create alice
  python -m headbop.manage users reset alice
  python -m headbop.manage users disable alice
  python -m headbop.manage users list
"""

import argparse
import getpass
import os
from pathlib import Path
import sys
import warnings

from .auth import AuthBusyError, AuthStore
from .store import Store


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path,
                        default=Path(os.getenv("HEADBOP_DATA_DIR", "/tmp/headbop-ai")))
    groups = parser.add_subparsers(dest="group", required=True)
    users = groups.add_parser("users", help="Manage individual accounts")
    actions = users.add_subparsers(dest="action", required=True)
    for action in ("create", "reset", "disable"):
        command = actions.add_parser(action)
        command.add_argument("username")
    actions.add_parser("list")
    projects = groups.add_parser("projects", help="Manage ownership of legacy projects")
    project_actions = projects.add_subparsers(dest="action", required=True)
    adopt = project_actions.add_parser("adopt", help="Assign an ownerless legacy project")
    adopt.add_argument("project_id")
    adopt.add_argument("username")
    args = parser.parse_args(argv)
    try:
        store = AuthStore(args.data_dir / "security.sqlite3")
        if args.group == "projects":
            user = store.get_user(args.username)
            if user is None or user["disabled"]:
                raise ValueError("An active account is required.")
            Store(args.data_dir).adopt(args.project_id, user["id"])
            print("Legacy project assigned:", args.project_id, user["username"])
        elif args.action == "list":
            for user in store.list_users():
                print(user["username"], user["id"], "disabled" if user["disabled"] else "active")
        elif args.action == "disable":
            store.disable_user(args.username)
            print("Account disabled; all sessions revoked.")
        else:
            # getpass otherwise falls back to an echoed stdin read when a
            # terminal is unavailable. Never expose an administrator's secret.
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                password = getpass.getpass("Password (12–128 characters): ")
                confirmation = getpass.getpass("Confirm password: ")
            if password != confirmation:
                raise ValueError("Passwords do not match")
            if args.action == "create":
                user = store.create_user(args.username, password)
                print("Account created:", user["username"], user["id"])
            else:
                store.reset_password(args.username, password)
                print("Password reset; all sessions revoked.")
        return 0
    except FileExistsError:
        print("Project already has an owner; ownership was not changed.", file=sys.stderr)
        return 1
    except (ValueError, FileNotFoundError, AuthBusyError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except getpass.GetPassWarning:
        print("A terminal with hidden password input is required.", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Account operation cancelled.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
