"""
SQLite persistence for Velloe Ops Intelligence (stdlib sqlite3, one file).

This is the single durable database layer. Runtime workflow state stays in the
per-execution Blackboard; this module stores the durable record of what happened.

Tables (schema version 2):
  investigations   one row per full workflow or individual agent execution
  incidents        incident catalog: seeded from the Ops snapshot, upserted when Scout collects
  events           every normalized record Scout collected for an investigation (incl. incident refs)
  hypotheses       ranked Intelligence hypotheses
  evidence         supporting / contradicting / missing evidence per hypothesis
  recommendations  Intelligence proposals (immutable content + Guardian verdict)
  actions          Action Board work items with approval lifecycle (-> recommendations)
  approvals        human approve / reject decisions (-> actions)
  agent_runs       one row per agent task
  tool_executions  one row per Scout tool attempt (was tool_calls; a tool_calls view remains)
  audit_logs       explanatory decision ledger (canonical + legacy fields)
  benchmark_runs   aggregate result of each `python benchmark.py` invocation

Migrations are additive and idempotent: CREATE IF NOT EXISTS, ADD COLUMN for missing
columns, one table rename, and PRAGMA user_version. Short-lived connections per call
keep it safe to use from the web server's threads; transaction() groups writes atomically.
"""
import json
import os
import re
import sqlite3
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from config import DB_PATH

SCHEMA_VERSION = 2
_lock = threading.RLock()
_initialized = set()  # DB paths already migrated in this process
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

TABLES = ("investigations", "incidents", "events", "evidence", "hypotheses", "recommendations", "actions",
          "approvals", "agent_runs", "tool_executions", "audit_logs", "benchmark_runs")

SCHEMA = """
CREATE TABLE IF NOT EXISTS investigations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, question TEXT, title TEXT, site TEXT,
  priority TEXT, status TEXT, confidence INTEGER, faults TEXT, is_benchmark INTEGER DEFAULT 0,
  plan_json TEXT, result_json TEXT, guardian_json TEXT, budget_json TEXT, revisions INTEGER DEFAULT 0,
  error TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, investigation_id INTEGER, record_id TEXT, kind TEXT,
  site TEXT, time TEXT, source TEXT, payload_json TEXT);
CREATE TABLE IF NOT EXISTS evidence (
  id INTEGER PRIMARY KEY AUTOINCREMENT, investigation_id INTEGER, hypothesis_id TEXT,
  relation TEXT, strength TEXT, statement TEXT, refs_json TEXT);
CREATE TABLE IF NOT EXISTS hypotheses (
  id INTEGER PRIMARY KEY AUTOINCREMENT, investigation_id INTEGER, hid TEXT, title TEXT,
  confidence INTEGER, label TEXT, rank INTEGER, payload_json TEXT);
CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, investigation_id INTEGER, code TEXT, issue TEXT,
  recommended TEXT, priority TEXT, owner TEXT, effort TEXT, confidence INTEGER, evidence_json TEXT,
  reason TEXT, priority_reason TEXT, approval_reason TEXT, expected_impact TEXT, risk TEXT,
  missing_evidence_json TEXT, opportunity TEXT, status TEXT, approval_state TEXT, requires_approval INTEGER,
  action_type TEXT, ticket_ref TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS agent_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, investigation_id INTEGER, agent TEXT, task TEXT,
  status TEXT, duration_ms INTEGER, llm_used INTEGER, error TEXT, started_at TEXT);
CREATE TABLE IF NOT EXISTS tool_executions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, investigation_id INTEGER, tool TEXT, source TEXT,
  attempt INTEGER, outcome TEXT, error TEXT, duration_ms INTEGER, time TEXT);
CREATE TABLE IF NOT EXISTS audit_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, investigation_id INTEGER, ts TEXT, t REAL, agent TEXT,
  role TEXT, action TEXT, input TEXT, output TEXT, reason TEXT, status TEXT, duration_ms INTEGER,
  retry INTEGER);
CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, action_id INTEGER, decision TEXT, actor TEXT, note TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS incidents (
  id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT NOT NULL UNIQUE, site TEXT, opened_at TEXT,
  severity TEXT, category TEXT, summary TEXT, source TEXT, payload_json TEXT,
  first_seen_investigation_id INTEGER, last_seen_investigation_id INTEGER,
  times_collected INTEGER NOT NULL DEFAULT 0, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS recommendations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  investigation_id INTEGER NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
  rid TEXT, rank INTEGER, claim_type TEXT DEFAULT 'RECOMMENDATION', action_type TEXT, issue TEXT,
  recommended TEXT, priority TEXT, owner TEXT, effort TEXT, confidence INTEGER, hypothesis_id TEXT,
  evidence_json TEXT, reason TEXT, priority_reason TEXT, approval_reason TEXT, expected_impact TEXT, risk TEXT,
  missing_evidence_json TEXT, opportunity TEXT, requires_approval INTEGER, revision INTEGER DEFAULT 0,
  guardian_verdict TEXT, verified_by_id INTEGER, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS benchmark_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT, n_runs INTEGER, fault_rate REAL, seed INTEGER,
  completed_runs INTEGER DEFAULT 0, completion_rate REAL, avg_seconds REAL, results_json TEXT,
  methodology TEXT, error TEXT, started_at TEXT, finished_at TEXT);
"""

# Columns added after the original schema. Applied to new and existing databases.
MIGRATIONS = {
    "investigations": {"mode": "TEXT DEFAULT 'FULL_WORKFLOW'", "agent": "TEXT", "parent_id": "INTEGER",
                       "context_json": "TEXT", "benchmark_run_id": "INTEGER"},
    "audit_logs": {"mode": "TEXT DEFAULT 'FULL_WORKFLOW'", "tool": "TEXT", "fallback_used": "INTEGER DEFAULT 0",
                   "budget_pct": "INTEGER", "execution_id": "INTEGER", "timestamp": "TEXT",
                   "retry_count": "INTEGER DEFAULT 0", "revision_count": "INTEGER DEFAULT 0",
                   "request_json": "TEXT", "response_status": "TEXT", "validation": "TEXT",
                   "normalized_result_json": "TEXT"},
    "actions": {"recommendation_id": "INTEGER", "priority_reason": "TEXT", "approval_reason": "TEXT",
                "expected_impact": "TEXT", "risk": "TEXT", "missing_evidence_json": "TEXT"},
    "recommendations": {"priority_reason": "TEXT", "approval_reason": "TEXT", "expected_impact": "TEXT",
                        "risk": "TEXT", "missing_evidence_json": "TEXT"},
    "approvals": {"investigation_id": "INTEGER", "previous_status": "TEXT", "new_status": "TEXT",
                  "ticket_ref": "TEXT"},
    "tool_executions": {"agent": "TEXT DEFAULT 'scout'", "mode": "TEXT", "response_status": "TEXT",
                        "validation": "TEXT", "fallback_used": "INTEGER DEFAULT 0", "records_count": "INTEGER"},
}

INDEXES = """
CREATE INDEX IF NOT EXISTS ix_investigations_mode ON investigations(mode, is_benchmark);
CREATE INDEX IF NOT EXISTS ix_investigations_parent ON investigations(parent_id);
CREATE INDEX IF NOT EXISTS ix_investigations_benchmark ON investigations(benchmark_run_id);
CREATE INDEX IF NOT EXISTS ix_events_investigation ON events(investigation_id);
CREATE INDEX IF NOT EXISTS ix_events_record ON events(record_id);
CREATE INDEX IF NOT EXISTS ix_evidence_investigation ON evidence(investigation_id);
CREATE INDEX IF NOT EXISTS ix_hypotheses_investigation ON hypotheses(investigation_id);
CREATE INDEX IF NOT EXISTS ix_recommendations_investigation ON recommendations(investigation_id);
CREATE INDEX IF NOT EXISTS ix_actions_investigation ON actions(investigation_id);
CREATE INDEX IF NOT EXISTS ix_actions_status ON actions(status);
CREATE INDEX IF NOT EXISTS ix_approvals_action ON approvals(action_id);
CREATE INDEX IF NOT EXISTS ix_agent_runs_investigation ON agent_runs(investigation_id);
CREATE INDEX IF NOT EXISTS ix_tool_executions_investigation ON tool_executions(investigation_id);
CREATE INDEX IF NOT EXISTS ix_audit_logs_investigation ON audit_logs(investigation_id);
CREATE INDEX IF NOT EXISTS ix_incidents_site ON incidents(site);
"""

# Read-only alias so existing SQL/tools that read tool_calls keep working after the rename.
COMPAT_VIEWS = {"tool_calls": "SELECT * FROM tool_executions"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def _session():
    """One connection: commit on success, roll back on error, always close (no leaked handles)."""
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ident(name):
    if not _IDENT.fullmatch(name):  # fullmatch: '$' alone would accept a trailing newline
        raise ValueError(f"invalid SQL identifier: {name!r}")
    return name


def _insert_sql(table, fields):
    cols = ", ".join(_ident(k) for k in fields)
    marks = ", ".join("?" for _ in fields)
    return f"INSERT INTO {_ident(table)} ({cols}) VALUES ({marks})", tuple(fields.values())


def _update_sql(table, row_id, fields):
    sets = ", ".join(f"{_ident(k)} = ?" for k in fields)
    return f"UPDATE {_ident(table)} SET {sets} WHERE id = ?", (*fields.values(), row_id)


# ---------------------------------------------------------------------- schema / migrations
def _objects(c):
    return {r["name"]: r["type"] for r in c.execute("SELECT name, type FROM sqlite_master")}


def _migrate_tool_calls(c):
    """v1 -> v2: tool_calls table becomes tool_executions (data preserved)."""
    objs = _objects(c)
    if objs.get("tool_calls") != "table":
        return
    if "tool_executions" not in objs:
        c.execute("ALTER TABLE tool_calls RENAME TO tool_executions")
    else:  # both exist (interrupted upgrade): move rows, then drop the legacy table
        cols = [r["name"] for r in c.execute("PRAGMA table_info(tool_calls)")]
        col_sql = ", ".join(_ident(x) for x in cols if x != "id")
        c.execute(f"INSERT INTO tool_executions ({col_sql}) SELECT {col_sql} FROM tool_calls")
        c.execute("DROP TABLE tool_calls")


def init(force=False):
    """Create or upgrade the schema, then seed reference data. Idempotent and safe on old databases."""
    path = os.path.abspath(DB_PATH)
    if path in _initialized and not force and os.path.exists(path):
        return
    with _lock:
        conn = _connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")  # readers do not block the workflow writer
            _migrate_tool_calls(conn)
            conn.executescript(SCHEMA)
            added = False
            for table, cols in MIGRATIONS.items():
                have = {r["name"] for r in conn.execute(f"PRAGMA table_info({_ident(table)})")}
                for col, ddl in cols.items():
                    if col not in have:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {_ident(col)} {ddl}")
                        added = True
            objs = _objects(conn)
            for view, select in COMPAT_VIEWS.items():
                if added or objs.get(view) != "view":  # SELECT * views freeze their column list
                    conn.execute(f"DROP VIEW IF EXISTS {view}")
                    conn.execute(f"CREATE VIEW {view} AS {select}")
            conn.executescript(INDEXES)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
        finally:
            conn.close()
        seed()
        _initialized.add(path)


def seed():
    """Seed the incident catalog from the demo Ops snapshot. Never overwrites collected rows."""
    try:
        from tools.ops_api import _norm_snapshot, load_snapshot
        rows = [_norm_snapshot("incidents", r) for r in load_snapshot().get("incidents", [])]
    except Exception:  # unreadable snapshot: the catalog simply fills as Scout collects incidents
        return 0
    stamp = now()
    with _lock, _session() as c:
        for r in rows:
            c.execute("INSERT OR IGNORE INTO incidents (incident_id, site, opened_at, severity, category, summary, "
                      "source, payload_json, times_collected, created_at, updated_at) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                      (r["id"], r.get("site"), r.get("time"), r.get("severity"), r.get("category"),
                       r.get("summary"), "seed: Ops snapshot", dumps(r), stamp, stamp))
    return len(rows)


# ---------------------------------------------------------------------- generic CRUD
def execute(sql, params=()):
    with _lock, _session() as c:
        return c.execute(sql, params).lastrowid


def query(sql, params=()):
    with _session() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def one(sql, params=()):
    rows = query(sql, params)
    return rows[0] if rows else None


def insert(table, **fields):
    return execute(*_insert_sql(table, fields))


def update(table, row_id, **fields):
    execute(*_update_sql(table, row_id, fields))


class Tx:
    """Same insert/update/one/query interface as the module, bound to one open transaction."""

    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=()):
        return self.conn.execute(sql, params)  # cursor: callers may check rowcount

    def query(self, sql, params=()):
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def one(self, sql, params=()):
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def insert(self, table, **fields):
        return self.conn.execute(*_insert_sql(table, fields)).lastrowid

    def update(self, table, row_id, **fields):
        return self.conn.execute(*_update_sql(table, row_id, fields)).rowcount


@contextmanager
def transaction():
    """BEGIN IMMEDIATE ... COMMIT; any exception rolls everything back.
    Do not call module-level write helpers inside the block: use the yielded Tx."""
    with _lock:
        conn = _connect()
        conn.isolation_level = None  # explicit transaction control
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield Tx(conn)
            conn.execute("COMMIT")
        except BaseException:
            try:  # BEGIN may have failed (e.g. database locked): keep the original error, not "no transaction"
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()


# ---------------------------------------------------------------------- domain helpers
def upsert_incident(record, investigation_id=None, source=None):
    """Store an incident Scout collected; keeps first/last investigation and a collection counter."""
    stamp = now()
    execute("INSERT INTO incidents (incident_id, site, opened_at, severity, category, summary, source, payload_json, "
            "first_seen_investigation_id, last_seen_investigation_id, times_collected, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?) "
            "ON CONFLICT(incident_id) DO UPDATE SET site = excluded.site, opened_at = excluded.opened_at, "
            "severity = excluded.severity, category = excluded.category, summary = excluded.summary, "
            "source = excluded.source, payload_json = excluded.payload_json, "
            "first_seen_investigation_id = COALESCE(incidents.first_seen_investigation_id, "
            "excluded.first_seen_investigation_id), last_seen_investigation_id = excluded.last_seen_investigation_id, "
            "times_collected = incidents.times_collected + 1, updated_at = excluded.updated_at",
            (record["id"], record.get("site"), record.get("time"), record.get("severity"), record.get("category"),
             record.get("summary"), source, dumps(record), investigation_id, investigation_id, stamp, stamp))


def info():
    """Schema version, journal mode and row counts (used by `python store.py` and tests)."""
    init()
    with _session() as c:
        return {"path": os.path.abspath(DB_PATH),
                "schema_version": c.execute("PRAGMA user_version").fetchone()[0],
                "journal_mode": c.execute("PRAGMA journal_mode").fetchone()[0],
                "tables": {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES},
                "views": sorted(COMPAT_VIEWS)}


def dumps(value):
    return json.dumps(value, default=str, ensure_ascii=False)


def loads(value, default=None):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def reset():
    """Delete all stored runs (local demo data only), then re-seed the incident catalog."""
    init()
    with _lock, _session() as c:
        for table in ("approvals", "actions", "recommendations", "evidence", "hypotheses", "events",
                      "agent_runs", "tool_executions", "audit_logs", "investigations", "benchmark_runs",
                      "incidents"):
            c.execute(f"DELETE FROM {table}")
    seed()


if __name__ == "__main__":
    # python store.py  -> create/upgrade the database and print its schema version and row counts
    json.dump(info(), sys.stdout, indent=2)
    print()
