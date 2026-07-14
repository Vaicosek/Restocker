#!/usr/bin/env python3
"""Pre-deploy healthcheck for the Restocker bot.

Compiles every Python file and sanity-checks the app WITHOUT starting the bot, so a
broken push (syntax error, corrupted file, bad DB) is caught before it takes the live
bot down. Exit code 0 = safe to (re)start, non-zero = do NOT restart.

Usage:
    python healthcheck.py            # run the checks
    python healthcheck.py --quiet    # only print on failure

Wire it into your deploy hook, e.g.:
    git pull && python healthcheck.py && sudo systemctl restart restocker
so the restart only happens when the checks pass.
"""
import sys
import os
import glob
import py_compile
import sqlite3

ROOT = os.path.dirname(os.path.abspath(__file__))
QUIET = "--quiet" in sys.argv
FAILS: list[str] = []
WARNS: list[str] = []


def _rel(p: str) -> str:
    return os.path.relpath(p, ROOT)


def check_compile() -> int:
    """py_compile every .py file — catches syntax errors (the #1 deploy-breaker)."""
    files: list[str] = []
    for pat in ("*.py", "cogs/*.py", "views/*.py", "valuation/*.py"):
        files += glob.glob(os.path.join(ROOT, pat))
    files = sorted({f for f in files if os.path.basename(f) != "healthcheck.py"})
    for f in files:
        try:
            py_compile.compile(f, doraise=True)
        except py_compile.PyCompileError as e:
            last = (e.msg or str(e)).strip().splitlines()[-1]
            FAILS.append(f"COMPILE {_rel(f)} — {last}")
        except Exception as e:  # noqa: BLE001
            FAILS.append(f"COMPILE {_rel(f)} — {type(e).__name__}: {e}")
    return len(files)


def check_required_files() -> None:
    for req in ("Restocker_main.py", "Restocker_db.py", "Restocker_web.py"):
        if not os.path.exists(os.path.join(ROOT, req)):
            FAILS.append(f"MISSING required file: {req}")


def check_db_import() -> None:
    """Import the DB module (no side effects / doesn't start the bot) to catch
    import-time errors that py_compile can't see, e.g. a bad top-level reference."""
    if os.path.exists(os.path.join(ROOT, "Restocker_db.py")):
        sys.path.insert(0, ROOT)
        try:
            import importlib
            importlib.invalidate_caches()
            importlib.import_module("Restocker_db")
        except Exception as e:  # noqa: BLE001
            FAILS.append(f"IMPORT Restocker_db — {type(e).__name__}: {e}")


def check_db_integrity() -> None:
    db = os.path.join(ROOT, os.getenv("RESTOCKER_DB", "restocker.db"))
    if not os.path.exists(db):
        WARNS.append(f"{os.path.basename(db)} not found (fresh install?) — skipping integrity check")
        return
    try:
        con = sqlite3.connect(db)
        row = con.execute("PRAGMA integrity_check").fetchone()
        con.close()
        if not row or str(row[0]).lower() != "ok":
            FAILS.append(f"DB integrity_check returned: {row[0] if row else 'no result'}")
    except Exception as e:  # noqa: BLE001
        FAILS.append(f"DB {os.path.basename(db)} — {type(e).__name__}: {e}")


def check_env() -> None:
    if not os.path.exists(os.path.join(ROOT, ".env")):
        WARNS.append(".env not found — the bot may miss its token/config")


def main() -> int:
    n_files = check_compile()
    check_required_files()
    check_db_import()
    check_db_integrity()
    check_env()

    if FAILS:
        print("❌ Restocker preflight FAILED — do NOT restart the bot:")
        for f in FAILS:
            print(f"   • {f}")
        for w in WARNS:
            print(f"   ⚠️  {w}")
        return 1

    if not QUIET:
        print(f"✅ Restocker preflight OK — {n_files} Python file(s) compile, DB integrity OK.")
        for w in WARNS:
            print(f"   ⚠️  {w}")
        print("   Safe to (re)start.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
