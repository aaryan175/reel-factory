#!/usr/bin/env python3
"""Add or update one Reel Deck login. Other users are left alone. Usage:

    .venv/bin/python tools/set_login.py <username> <password> [--role OPERATOR|EDITOR]

Role defaults to OPERATOR. EDITOR may use the deck, but its verdicts are
written as advisory — see server/receipts.py.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server import auth  # noqa: E402

parser = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("username")
parser.add_argument("password")
parser.add_argument("--role", default=auth.ROLE_OPERATOR,
                    type=str.upper, choices=list(auth.ROLES))
args = parser.parse_args()

auth.set_credentials(args.username, args.password, args.role)
print(f"login set for {args.username!r} as {args.role} in {auth.CONFIG_PATH}")
print("users now: " + ", ".join(f"{u['username']} ({u['role']})" for u in auth.list_users()))
