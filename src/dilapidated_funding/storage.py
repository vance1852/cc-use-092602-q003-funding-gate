"""危房改造资金门禁的 SQLite 模式与事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS fund_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('finance','township','housing','authority','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

-- 预算按 (budget_id, version) 留痕；held/paid 只随在该版本上锁定的项目变动。
CREATE TABLE IF NOT EXISTS budget_versions (
    budget_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    total_amount TEXT NOT NULL,
    held_amount TEXT NOT NULL DEFAULT '0.00',
    paid_amount TEXT NOT NULL DEFAULT '0.00',
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','superseded')),
    published_by TEXT NOT NULL REFERENCES fund_users(user_id),
    published_at TEXT NOT NULL,
    PRIMARY KEY(budget_id, version),
    CHECK(CAST(held_amount AS REAL) + CAST(paid_amount AS REAL) <= CAST(total_amount AS REAL) + 0.000001)
);

CREATE INDEX IF NOT EXISTS idx_budget_versions_current
ON budget_versions(budget_id, state, version);

-- 乡镇施工资源：容量 = 可同时开工的改造项目数。
CREATE TABLE IF NOT EXISTS construction_resources (
    township_id TEXT PRIMARY KEY,
    capacity INTEGER NOT NULL CHECK(capacity >= 0),
    note TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL REFERENCES fund_users(user_id),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL,
    township_id TEXT NOT NULL,
    budget_id TEXT NOT NULL,
    appraisal_grade TEXT NOT NULL CHECK(appraisal_grade IN ('A','B','C','D')),
    risk_level TEXT NOT NULL CHECK(risk_level IN ('high','medium','low')),
    latest_movein_date TEXT NOT NULL,
    central_amount TEXT NOT NULL,
    local_amount TEXT NOT NULL,
    household_amount TEXT NOT NULL,
    subsidy_amount TEXT NOT NULL,
    priority_score TEXT NOT NULL DEFAULT '0',
    completed_percent TEXT NOT NULL DEFAULT '0',
    paid_amount TEXT NOT NULL DEFAULT '0.00',
    state TEXT NOT NULL DEFAULT 'submitted' CHECK(state IN (
        'submitted','confirmed','in_progress','delayed','suspended','completed','cancelled'
    )),
    revision INTEGER NOT NULL DEFAULT 1,
    budget_version INTEGER,
    submitted_by TEXT NOT NULL REFERENCES fund_users(user_id),
    submitted_at TEXT NOT NULL,
    confirmed_at TEXT,
    FOREIGN KEY(budget_id, budget_version) REFERENCES budget_versions(budget_id, version)
);

CREATE INDEX IF NOT EXISTS idx_projects_township_state
ON projects(township_id, state);

CREATE INDEX IF NOT EXISTS idx_projects_budget_state
ON projects(budget_id, state);

CREATE TABLE IF NOT EXISTS project_milestones (
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    plan_date TEXT NOT NULL,
    weight_percent TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    accepted_at TEXT,
    PRIMARY KEY(project_id, code)
);

CREATE TABLE IF NOT EXISTS project_evaluations (
    evaluation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    budget_id TEXT NOT NULL,
    budget_version INTEGER NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('eligible','ineligible')),
    priority_score TEXT NOT NULL,
    gates_json TEXT NOT NULL,
    exempted_gates_json TEXT NOT NULL DEFAULT '[]',
    stages_json TEXT NOT NULL,
    trigger TEXT NOT NULL,
    evaluated_by TEXT NOT NULL REFERENCES fund_users(user_id),
    evaluated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evaluations_project
ON project_evaluations(project_id, evaluation_id);

CREATE TABLE IF NOT EXISTS budget_reservations (
    reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    budget_id TEXT NOT NULL,
    budget_version INTEGER NOT NULL,
    amount TEXT NOT NULL,
    held_amount TEXT NOT NULL,
    paid_amount TEXT NOT NULL DEFAULT '0.00',
    state TEXT NOT NULL DEFAULT 'held' CHECK(state IN ('held','settled','released')),
    created_at TEXT NOT NULL,
    FOREIGN KEY(budget_id, budget_version) REFERENCES budget_versions(budget_id, version)
);

CREATE INDEX IF NOT EXISTS idx_reservations_project
ON budget_reservations(project_id, reservation_id);

CREATE TABLE IF NOT EXISTS project_resource_locks (
    lock_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    township_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'held' CHECK(state IN ('held','released')),
    locked_at TEXT NOT NULL,
    released_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_resource_locks_project
ON project_resource_locks(project_id, lock_id);

CREATE TABLE IF NOT EXISTS exemptions (
    exemption_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    grantor_id TEXT NOT NULL REFERENCES fund_users(user_id),
    reason TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    gates_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','expired','revoked')),
    created_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_exemptions_project
ON exemptions(project_id, exemption_id);

-- 同一项目只允许一条生效中的豁免。
CREATE UNIQUE INDEX IF NOT EXISTS idx_exemption_one_active
ON exemptions(project_id) WHERE state = 'active';

CREATE TABLE IF NOT EXISTS disbursement_stages (
    stage_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    milestone_code TEXT NOT NULL,
    name TEXT NOT NULL,
    plan_date TEXT NOT NULL,
    weight_percent TEXT NOT NULL,
    cumulative_weight TEXT NOT NULL,
    amount_cny TEXT NOT NULL,
    paid_amount TEXT NOT NULL DEFAULT '0.00',
    state TEXT NOT NULL DEFAULT 'planned' CHECK(state IN ('planned','paid','settled','cancelled')),
    paid_at TEXT,
    UNIQUE(project_id, milestone_code)
);

CREATE TABLE IF NOT EXISTS settlements (
    settlement_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    kind TEXT NOT NULL CHECK(kind IN ('acceptance','suspension','cancellation','resume')),
    completion_percent TEXT NOT NULL,
    paid_amount TEXT NOT NULL,
    released_amount TEXT NOT NULL DEFAULT '0.00',
    note TEXT NOT NULL DEFAULT '',
    actor_id TEXT NOT NULL REFERENCES fund_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_settlements_project
ON settlements(project_id, settlement_id);

CREATE TABLE IF NOT EXISTS project_timeline (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_timeline_project
ON project_timeline(project_id, event_id);

CREATE TABLE IF NOT EXISTS fund_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS fund_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fund_audit_entity
ON fund_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
