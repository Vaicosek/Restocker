"""
Dummy-data regression tests for the Restocker SQLite layer.

Run from the RestockerLocal folder:
    python -m pytest tests/ -q
or standalone (no pytest needed):
    python tests/test_restocker_db.py

Every test uses a throwaway temp DB — it never touches the live restocker.db.
"""
import importlib.util
import os
import tempfile
import threading
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_DBFILE = _HERE.parent / "Restocker_db.py"


def _fresh_db():
    spec = importlib.util.spec_from_file_location("Restocker_db_test", str(_DBFILE))
    db = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(db)
    db.DB_PATH = Path(tempfile.mkdtemp()) / "test.db"
    db._local.__dict__.clear()
    db.init_db()
    return db


def test_balances_and_ledger():
    db = _fresh_db()
    db.set_balance("100", coins=500, principal=500, lp=0)
    b = db.get_balance("100")
    assert b["coins"] == 500 and b["principal"] == 500
    # partial set preserves omitted fields
    db.set_balance("100", coins=450)
    assert db.get_balance("100")["principal"] == 500
    db.record_coin_ledger("100", -50, 450, "test")
    led = db.get_coin_ledger("100")
    assert len(led) == 1 and led[0]["delta"] == -50
    # unknown user defaults to zero
    assert db.get_balance("999")["coins"] == 0


def test_adjust_balance_semantics():
    db = _fresh_db()
    c, p, d = db.adjust_balance("1", 100)
    assert (c, p, d) == (100, 100, 100)
    c, p, d = db.adjust_balance("1", 50, counts_as_principal=False)
    assert (c, p, d) == (150, 100, 50)
    c, p, d = db.adjust_balance("1", -30)
    assert (c, p, d) == (120, 70, -30)
    c, p, d = db.adjust_balance("1", -20, reduce_principal=False)
    assert (c, p, d) == (100, 70, -20)
    # overdraft clamps at 0, principal never negative
    c, p, d = db.adjust_balance("1", -999)
    assert c == 0 and p >= 0 and d == -100


def test_adjust_balance_is_atomic_under_concurrency():
    db = _fresh_db()
    db.adjust_balance("3", 0)

    def worker():
        for _ in range(10):
            db.adjust_balance("3", 10)

    ts = [threading.Thread(target=worker) for _ in range(20)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    # 20 threads * 10 adds * 10 coins == 2000, exactly — proves no lost updates.
    assert db.get_balance("3")["coins"] == 2000


def test_items_crud():
    db = _fresh_db()
    db.upsert_item("Diamond Sword", coin=60, stock=10)
    assert int(db.get_item("Diamond Sword")["coin"]) == 60
    db.update_item_stock("Diamond Sword", 3)
    assert int(db.get_item("Diamond Sword")["stock"]) == 3
    db.rename_item("Diamond Sword", "Diamond Blade")
    assert db.get_item("Diamond Blade") and db.get_item("Diamond Sword") is None
    assert db.delete_item("Diamond Blade")


def test_orders_and_claims_roundtrip():
    db = _fresh_db()
    nid = db.next_order_id()
    o = {"id": nid, "item": "Diamond Axe", "requested": 30, "produced": 0,
         "status": "claimed",
         "claims": [{"user_id": 555000111222333444, "user_tag": "jimbob", "qty": 30,
                     "claimed_at": "2026-01-01T00:00:00+00:00"}],
         "messages": {"channel_id": None, "message_id": None, "dms": {}}}
    db.save_order(o)
    lo = db.get_order(nid)
    assert lo["status"] == "claimed" and lo["requested"] == 30
    # DOCUMENTED behaviour: order_claims.user_id persists as TEXT. Callers must
    # compare as int (Restocker_main.load_orders coerces it; views use _claim_of).
    assert lo["claims"][0]["user_id"] == "555000111222333444"
    assert int(lo["claims"][0]["user_id"]) == 555000111222333444
    assert db.next_order_id() > nid


def test_platform_investors_team_projects():
    db = _fresh_db()
    db.set_platform_balance(1000)
    assert db.get_platform_balance() == 1000
    db.upsert_investor("7", balance=100.0, principal=100.0)
    assert db.get_investor("7")["balance"] == 100.0
    assert db.adjust_etf_units("7", 5.0, 250.0) == 5.0
    db.set_team_member("w1", "m1")
    assert db.get_manager_of("w1") == "m1"
    pid = db.create_project("Build", "f1", "m1", 500)
    db.add_project_member(pid, "w1", 1.0)
    assert db.get_project(pid)["budget"] == 500


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(fns)} passed")
