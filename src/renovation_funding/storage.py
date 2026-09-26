"""危房改造资金门禁的 SQLite 模式与事务辅助。

表设计要点：
- fund_releases 保存带版本号和有效期的资金额度，frozen/locked 金额单独统计，
  迟到的预算调整（新版本）只影响尚未确认的计划；
- renovation_plans 保存申报快照和门禁结论，状态机驱动确认、验收、暂停、取消；
- plan_stages 为可解释的拨付阶段，disbursements 记录按实际完成量结算的拨付；
- construction_resources 是有限施工资源（班组档期），确认时与预算在同一事务锁定；
- exemptions 记录紧急加固豁免的授权人、理由与失效时间；
- funding_audit_events 为哈希链审计，重启后可核对全部决策。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS funding_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('finance','township','reviewer','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fund_releases (
    release_id TEXT PRIMARY KEY,
    fiscal_year INTEGER NOT NULL,
    version INTEGER NOT NULL,
    total_amount TEXT NOT NULL,
    frozen_amount TEXT NOT NULL DEFAULT '0',
    disbursed_amount TEXT NOT NULL DEFAULT '0',
    effective_from TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    note TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','superseded','closed')),
    supersedes_release_id TEXT REFERENCES fund_releases(release_id),
    created_by TEXT NOT NULL REFERENCES funding_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(fiscal_year, version)
);

CREATE TABLE IF NOT EXISTS construction_resources (
    resource_id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    name TEXT NOT NULL,
    capacity_units TEXT NOT NULL,
    booked_units TEXT NOT NULL DEFAULT '0',
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','retired')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exemptions (
    exemption_id TEXT PRIMARY KEY,
    plan_id TEXT,
    authorized_by TEXT NOT NULL REFERENCES funding_users(user_id),
    reason TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','revoked','expired','consumed')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS renovation_plans (
    plan_id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL,
    township_id TEXT NOT NULL,
    risk_grade TEXT NOT NULL CHECK(risk_grade IN ('A','B','C','D')),
    estimated_cost TEXT NOT NULL,
    central_subsidy TEXT NOT NULL,
    local_match TEXT NOT NULL,
    household_self_raise TEXT NOT NULL,
    household_commitment TEXT NOT NULL DEFAULT '',
    latest_move_in_date TEXT NOT NULL,
    construction_team_id TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    release_id TEXT REFERENCES fund_releases(release_id),
    exemption_id TEXT REFERENCES exemptions(exemption_id),
    gate_decision TEXT CHECK(gate_decision IN ('approved','deferred')),
    gate_reasons_json TEXT NOT NULL DEFAULT '[]',
    gate_blocking_json TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending','confirmed','active','paused','completed','cancelled','expired')),
    revision INTEGER NOT NULL DEFAULT 1,
    locked_budget TEXT NOT NULL DEFAULT '0',
    locked_resource_id TEXT REFERENCES construction_resources(resource_id),
    disbursed_total TEXT NOT NULL DEFAULT '0',
    settled_amount TEXT NOT NULL DEFAULT '0',
    idempotency_key TEXT NOT NULL UNIQUE,
    submitted_by TEXT NOT NULL REFERENCES funding_users(user_id),
    submitted_at TEXT NOT NULL,
    confirmed_at TEXT,
    completed_at TEXT,
    cancelled_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_plans_state ON renovation_plans(state, submitted_at);
CREATE INDEX IF NOT EXISTS idx_plans_release ON renovation_plans(release_id);

CREATE TABLE IF NOT EXISTS plan_stages (
    stage_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES renovation_plans(plan_id),
    seq INTEGER NOT NULL,
    trigger_type TEXT NOT NULL CHECK(trigger_type IN ('plan_confirmed','milestone_verified')),
    milestone_code TEXT,
    milestone_name TEXT,
    planned_date TEXT,
    weight TEXT NOT NULL,
    planned_amount TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'planned'
        CHECK(state IN ('planned','locked','disbursed','skipped')),
    UNIQUE(plan_id, seq)
);

CREATE TABLE IF NOT EXISTS milestone_verifications (
    verification_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES renovation_plans(plan_id),
    milestone_code TEXT NOT NULL,
    completion_percent TEXT NOT NULL,
    note TEXT NOT NULL,
    verified_by TEXT NOT NULL REFERENCES funding_users(user_id),
    verified_at TEXT NOT NULL,
    UNIQUE(plan_id, milestone_code)
);

CREATE TABLE IF NOT EXISTS disbursements (
    disbursement_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES renovation_plans(plan_id),
    stage_seq INTEGER NOT NULL,
    amount TEXT NOT NULL,
    basis TEXT NOT NULL,
    release_id TEXT REFERENCES fund_releases(release_id),
    created_by TEXT NOT NULL REFERENCES funding_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(plan_id, stage_seq)
);

CREATE TABLE IF NOT EXISTS funding_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS funding_audit_events (
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

CREATE INDEX IF NOT EXISTS idx_funding_audit_entity
ON funding_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    # check_same_thread=False 供 ThreadingHTTPServer 多线程共用；
    # WAL 日志与 BEGIN IMMEDIATE 已保证并发写入串行化。
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
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


def row_dict(row: sqlite3.Row | None) -> dict[str, object] | None:
    return None if row is None else dict(row)
