"""
Push 1 tests — safety, data protection, security. No network, no real DB.

Run from the repo root:
    python -m pytest tests/test_push1.py -v
"""
import os
import sys
import sqlite3
import hashlib
import importlib

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PROJECT_MODULES = None


def _seed(db_path, n_closed=137, n_open=3):
    """A DB shaped like production: >50 closed trades, mixed results, a few open."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE paper_trades (
            trade_id TEXT PRIMARY KEY, symbol TEXT, direction TEXT, strategy TEXT,
            entry_price REAL, entry_time TEXT, exit_price REAL, exit_time TEXT,
            result TEXT, risk_pct REAL, risk_dollars REAL, rr REAL, pnl REAL,
            score REAL, ai_reasoning TEXT, status TEXT DEFAULT 'OPEN')
    """)
    conn.execute("CREATE TABLE signals (id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id TEXT, symbol TEXT, result TEXT)")
    strategies = ["ORB SCALP", "RP PROFITS", "ICT 5STEP", "SUPPLY DEMAND"]
    for i in range(n_closed):
        result = ("WIN", "LOSS", "LOSS", "BE")[i % 4] if i % 9 else "WIN"
        pnl = {"WIN": 187.33, "LOSS": -50.10, "BE": 0.0}[result] + i * 0.01
        conn.execute(
            "INSERT INTO paper_trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"T{i:04d}", ("NQ", "ES", "XAUUSD")[i % 3], "LONG", strategies[i % 4], 100.0,
             f"2026-08-{1 + i % 28:02d}T10:00:00.000000", 101.0,
             f"2026-08-{1 + i % 28:02d}T11:{i % 60:02d}:00.000000",
             result, 1.0, 50.0, 3.0, pnl, 8.0, "seed", "CLOSED"),
        )
        conn.execute("INSERT INTO signals (trade_id, symbol, result) VALUES (?,?,?)", (f"T{i:04d}", "NQ", result))
    for i in range(n_open):
        conn.execute(
            "INSERT INTO paper_trades (trade_id, symbol, direction, strategy, entry_price, entry_time, risk_dollars, score, status) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (f"OPEN{i}", "NQ", "LONG", "ORB SCALP", 100.0, "2026-09-01T10:00:00.000000", 50.0, 8.0, "OPEN"),
        )
    conn.commit()
    conn.close()


def _counts(db_path):
    conn = sqlite3.connect(db_path)
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    out = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    conn.close()
    return out


def _dump(db_path, table="paper_trades"):
    conn = sqlite3.connect(db_path)
    rows = conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
    conn.close()
    return hashlib.sha256(repr(rows).encode()).hexdigest()


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "data" / "trades.db")
    os.makedirs(os.path.dirname(path))
    _seed(path)
    monkeypatch.setenv("DB_PATH", path)
    monkeypatch.delenv("MIGRATION_RETRY", raising=False)
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    return path


@pytest.fixture
def mig():
    import migrations
    return importlib.reload(migrations)


def _load_app(monkeypatch, tmp_path, **env):
    """Import main.py fresh against the current env (modules read DB_PATH at import time)."""
    global PROJECT_MODULES
    if PROJECT_MODULES is None:
        PROJECT_MODULES = {f[:-3] for f in os.listdir(ROOT) if f.endswith(".py")}
    for k in ("DASHBOARD_PASSCODE", "DASHBOARD_SECRET_KEY", "ADMIN_SECRET"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-real-key")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    monkeypatch.chdir(tmp_path)          # main.py creates ./logs
    for name in list(sys.modules):
        if name in PROJECT_MODULES:
            del sys.modules[name]
    main = importlib.import_module("main")
    from fastapi.testclient import TestClient
    # No `with`: lifespan (scanner, agents, network loops) is deliberately NOT started.
    return main, TestClient(main.app, follow_redirects=False)


# ── 1. migrations ────────────────────────────────────────────────────────────

def test_migration_runs_twice_with_identical_results(db, mig):
    trades_before = _dump(db)

    first = mig.run_migrations(db)
    assert first["applied_now"] == [m[0] for m in mig.MIGRATIONS]
    assert first["backup"] and os.path.exists(first["backup"])
    schema_after_first = sqlite3.connect(db).execute(
        "SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
    applied_first = sqlite3.connect(db).execute("SELECT * FROM schema_migrations ORDER BY id").fetchall()

    second = mig.run_migrations(db)
    assert second["applied_now"] == []
    assert second["backup"] is None                      # nothing pending → no new backup
    assert sqlite3.connect(db).execute(
        "SELECT name, sql FROM sqlite_master ORDER BY name").fetchall() == schema_after_first
    assert sqlite3.connect(db).execute("SELECT * FROM schema_migrations ORDER BY id").fetchall() == applied_first

    assert _dump(db) == trades_before                    # every trade row byte-identical
    for f in mig._STRICT_FIELDS:
        assert first["before"][f] == first["after"][f] == second["after"][f]


def test_row_counts_and_baseline_preserved(db, mig):
    before_counts = _counts(db)
    conn = sqlite3.connect(db)
    base = mig.take_snapshot(conn)
    conn.close()
    assert base["total_trades"] == 140 and base["closed_trades"] == 137 and base["open_trades"] == 3

    report = mig.run_migrations(db)

    after_counts = _counts(db)
    for table, n in before_counts.items():
        assert after_counts[table] == n, table
    assert mig.diff_snapshots(base, report["after"]) == []
    assert _counts(report["backup"])["paper_trades"] == 140          # backup is a full copy
    name = os.path.basename(report["backup"])
    assert name.startswith("trades.db.bak-") and os.path.dirname(report["backup"]) == os.path.dirname(db)


def test_migration_that_changes_history_is_rolled_back(db, mig):
    trades_before = _dump(db)

    def bad(conn):
        conn.execute("UPDATE paper_trades SET pnl = pnl + 1 WHERE trade_id = 'T0001'")

    with pytest.raises(mig.MigrationError):
        mig.run_migrations(db, migrations=mig.MIGRATIONS + [("999_bad", bad)])

    assert _dump(db) == trades_before                                # backup restored
    assert os.path.exists(db + ".migration-failed")
    with pytest.raises(mig.MigrationError):                          # and it will not silently retry
        mig.run_migrations(db)


def test_old_baselines_survive_new_trades_but_catch_tampering(db, mig):
    mig.run_migrations(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO paper_trades (trade_id, symbol, strategy, exit_time, result, pnl, status) "
        "VALUES ('NEW1','NQ','ORB SCALP','2099-01-01T00:00:00.000000','WIN',500.0,'CLOSED')")
    conn.commit()
    assert all(c["ok"] for c in mig.verify_stored_baselines(db))     # new trades are fine

    conn.execute("UPDATE paper_trades SET pnl = 9999 WHERE trade_id = 'T0003'")
    conn.commit()
    conn.close()
    assert not any(c["ok"] for c in mig.verify_stored_baselines(db))  # old trade changed → flagged


def test_add_column_helper_is_idempotent(db, mig):
    conn = sqlite3.connect(db)
    assert mig.add_column_if_missing(conn, "paper_trades", "zz_test", "INTEGER DEFAULT 0") is True
    assert mig.add_column_if_missing(conn, "paper_trades", "zz_test", "INTEGER DEFAULT 0") is False
    assert conn.execute("SELECT COUNT(*) FROM paper_trades WHERE zz_test = 0").fetchone()[0] == 140
    conn.close()


def test_fresh_db_needs_no_backup(tmp_path, mig):
    path = str(tmp_path / "new" / "trades.db")
    report = mig.run_migrations(path)
    assert report["backup"] is None and report["after"]["total_trades"] == 0


# ── 2. daily backup ──────────────────────────────────────────────────────────

def test_daily_backup_copy_and_prune(db, tmp_path):
    import db_backup
    importlib.reload(db_backup)
    dest_dir = str(tmp_path / "second-location")

    path = db_backup.make_backup(db, dest_dir, day="20261109")
    assert _counts(path)["paper_trades"] == 140 and _dump(path) == _dump(db)
    mtime = os.path.getmtime(path)
    assert db_backup.make_backup(db, dest_dir, day="20261109") == path     # once per day
    assert os.path.getmtime(path) == mtime

    for d in ("20261110", "20261111", "20261112"):
        db_backup.make_backup(db, dest_dir, day=d)
    assert db_backup.prune(dest_dir, keep=2) == ["trades-20261109.db", "trades-20261110.db"]
    assert sorted(os.listdir(dest_dir)) == ["trades-20261111.db", "trades-20261112.db"]
    assert os.path.exists(db)                                              # live DB never touched
    assert db_backup.resolve_backup("../../trades.db", db, dest_dir) is None


def test_restore_is_explicit_one_shot_and_keeps_the_replaced_db(db, tmp_path, monkeypatch):
    import db_backup
    importlib.reload(db_backup)
    dest_dir = str(tmp_path / "second-location")
    db_backup.make_backup(db, dest_dir, day="20261109")
    good = _dump(db)

    assert db_backup.restore_if_requested(db, dest_dir) is None       # env var unset → never acts

    conn = sqlite3.connect(db)                                        # simulate damage after the backup
    conn.execute("UPDATE paper_trades SET pnl = 0 WHERE trade_id = 'T0005'")
    conn.commit(); conn.close()
    damaged = _dump(db)

    monkeypatch.setenv("RESTORE_FROM", "trades-20261109.db")
    assert db_backup.restore_if_requested(db, dest_dir)
    assert _dump(db) == good
    kept = [n for n in os.listdir(os.path.dirname(db)) if ".replaced-" in n]
    assert len(kept) == 1 and _dump(os.path.join(os.path.dirname(db), kept[0])) == damaged

    assert db_backup.restore_if_requested(db, dest_dir) is None       # same value again → no-op

    monkeypatch.setenv("RESTORE_FROM", "trades-19990101.db")
    with pytest.raises(RuntimeError):
        db_backup.restore_if_requested(db, dest_dir)
    assert _dump(db) == good


# ── 3. /stats and /login ─────────────────────────────────────────────────────

def test_stats_totals_equal_direct_sql(db, monkeypatch, tmp_path):
    main, client = _load_app(monkeypatch, tmp_path, DASHBOARD_PASSCODE="a-brand-new-passcode")
    conn = sqlite3.connect(db)
    total, wins, losses, pnl = conn.execute(
        "SELECT COUNT(*), SUM(result='WIN'), SUM(result='LOSS'), SUM(pnl) "
        "FROM paper_trades WHERE status='CLOSED'").fetchone()
    conn.close()
    assert total == 137                                   # well past the old LIMIT 50

    r = client.get("/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == total
    assert body["wins"] == wins
    assert body["losses"] == losses
    assert body["total_pnl"] == round(pnl, 2)
    assert body["win_rate"] == round(wins / total, 3)

    h = client.get("/health")
    assert h.status_code == 200 and h.json()["db_path"] == db and h.json()["migrations"] >= 1


def test_dashboard_refuses_to_load_without_passcode(db, monkeypatch, tmp_path):
    main, client = _load_app(monkeypatch, tmp_path)
    assert client.get("/login").status_code == 503
    assert client.post("/login", data={"passcode": ""}).status_code == 503
    assert client.get("/dashboard").status_code == 503
    assert client.get("/stats").status_code == 200        # the bot itself keeps running


def test_old_leaked_passcode_is_rejected_even_if_set(db, monkeypatch, tmp_path):
    leaked = "the-old-public-value"
    main, client = _load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "_LEAKED_PASSCODE_SHA256", hashlib.sha256(leaked.encode()).hexdigest())
    assert main._resolve_passcode(leaked)[0] == ""
    assert main._resolve_passcode("")[0] == ""
    assert main._resolve_passcode("something-new") == ("something-new", "")
    src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    assert 'os.getenv("DASHBOARD_PASSCODE", "")' in src   # no hardcoded fallback in source
    assert 'getenv("DASHBOARD_SECRET_KEY", "bot-secret-key")' not in src


def test_login_works_with_new_passcode(db, monkeypatch, tmp_path):
    main, client = _load_app(monkeypatch, tmp_path, DASHBOARD_PASSCODE="a-brand-new-passcode")
    assert client.get("/login").status_code == 200
    assert client.post("/login", data={"passcode": "wrong"}).status_code == 401
    ok = client.post("/login", data={"passcode": "a-brand-new-passcode"})
    assert ok.status_code == 303 and ok.headers["location"] == "/dashboard"
    client.cookies.set(main.SESSION_COOKIE_NAME, ok.cookies[main.SESSION_COOKIE_NAME])
    assert client.get("/dashboard").status_code == 200
    client.cookies.clear()

    # a cookie signed with the old public default key must NOT be accepted
    from itsdangerous import URLSafeTimedSerializer
    forged = URLSafeTimedSerializer("bot-secret-key").dumps({"auth": True})
    client.cookies.set(main.SESSION_COOKIE_NAME, forged)
    assert client.get("/dashboard").status_code == 303


def test_admin_db_status_requires_secret(db, monkeypatch, tmp_path):
    main, client = _load_app(monkeypatch, tmp_path, ADMIN_SECRET="test-admin", DASHBOARD_PASSCODE="x-new")
    assert client.get("/admin/db-status").status_code == 401
    r = client.get("/admin/db-status", headers={"X-Admin-Token": "test-admin"})
    assert r.status_code == 200
    body = r.json()
    assert body["history_intact"] is True
    assert body["snapshot"]["closed_trades"] == 137
    assert [m["id"] for m in body["migrations_applied"]] == body["migrations_known"]
    assert any(b["kind"] == "pre-migration" for b in body["backups"])
