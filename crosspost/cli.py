"""Command-line interface: ``python -m crosspost.cli <command>``."""
from __future__ import annotations

import argparse
import getpass
import sys

from sqlalchemy import select

from .config import get_settings
from .db import session_scope
from .logging_setup import configure_logging


def cmd_migrate(args: argparse.Namespace) -> None:
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config("alembic.ini"), "head")
    print("Database migrated to head.")


def cmd_create_user(args: argparse.Namespace) -> None:
    from .models import ROLES, User
    from .security import hash_password, password_problems

    if args.role not in ROLES:
        sys.exit(f"role must be one of {ROLES}")
    password = args.password or getpass.getpass("Password: ")
    problems = password_problems(password)
    if problems:
        sys.exit(" ".join(problems))
    with session_scope() as db:
        email = args.email.strip().lower()
        user = db.scalar(select(User).where(User.email == email))
        if user:
            user.password_hash, user.role, user.is_active = hash_password(password), args.role, True
            print(f"Updated {email} ({args.role}).")
        else:
            db.add(User(email=email, password_hash=hash_password(password), role=args.role))
            print(f"Created {email} ({args.role}).")


def cmd_sync(args: argparse.Namespace) -> None:
    from .mls.registry import get_provider
    from .services.sync import SyncError, run_sync

    with session_scope() as db:
        try:
            run = run_sync(db, get_provider(), mode=args.mode, mls_id=args.mls_id, triggered_by="cli")
        except SyncError as exc:
            sys.exit(f"Sync failed: {exc}")
        print(f"Sync {run.status}: fetched {run.fetched_count}, new {run.created_count}, updated {run.updated_count}, "
              f"unchanged {run.unchanged_count}")
        if run.error:
            print(run.error)


def cmd_run_jobs(args: argparse.Namespace) -> None:
    from .jobs.queue import run_pending

    print(f"Processed {run_pending()} job(s).")


def cmd_worker(args: argparse.Namespace) -> None:
    from .jobs.queue import worker_loop

    worker_loop()


def cmd_check_config(args: argparse.Namespace) -> None:
    problems = get_settings().validate_for_production()
    if problems:
        print("Configuration problems:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print(f"Configuration OK for {get_settings().app_env}.")


def cmd_connect_test_account(args: argparse.Namespace) -> None:
    from .services.accounts import connect_account
    from .social.base import TokenSet

    if get_settings().is_production:
        sys.exit("The sandbox adapter is for development only.")
    with session_scope() as db:
        account = connect_account(db, "test", args.name, f"Sandbox ({args.name})", TokenSet("sandbox-token"))
        print(f"Connected sandbox account #{account.id}.")


def cmd_generate_key(args: argparse.Namespace) -> None:
    from cryptography.fernet import Fernet

    print(Fernet.generate_key().decode())


def main(argv: list[str] | None = None) -> None:
    configure_logging(get_settings().log_level, "text")
    parser = argparse.ArgumentParser(prog="crosspost")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply database migrations").set_defaults(func=cmd_migrate)
    p = sub.add_parser("create-user", help="create or update a dashboard user")
    p.add_argument("email")
    p.add_argument("--role", default="admin")
    p.add_argument("--password", help="omit to be prompted (recommended)")
    p.set_defaults(func=cmd_create_user)
    p = sub.add_parser("sync", help="synchronize listings from the configured MLS provider")
    p.add_argument("--mode", choices=["incremental", "full"], default="incremental")
    p.add_argument("--mls-id")
    p.set_defaults(func=cmd_sync)
    sub.add_parser("run-jobs", help="process all due background jobs once").set_defaults(func=cmd_run_jobs)
    sub.add_parser("worker", help="run the background worker and scheduler").set_defaults(func=cmd_worker)
    sub.add_parser("check-config", help="validate configuration for the current APP_ENV").set_defaults(func=cmd_check_config)
    p = sub.add_parser("connect-test-account", help="connect the sandbox publishing adapter (development)")
    p.add_argument("--name", default="sandbox")
    p.set_defaults(func=cmd_connect_test_account)
    sub.add_parser("generate-key", help="print a new Fernet key for TOKEN_ENCRYPTION_KEY").set_defaults(func=cmd_generate_key)
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
