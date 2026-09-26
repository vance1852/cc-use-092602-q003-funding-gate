"""危房改造分期资金门禁用例。

覆盖：财政人员发布带版本与有效期的资金额度；乡镇申报风险等级、施工节点、
最迟入住日期与资金构成；系统形成可解释拨付阶段；确认时在同一事务内一次性
锁定预算与施工资源（失败不留部分冻结）；验收、暂停、取消按实际完成量结算；
紧急加固豁免记录授权人、理由与失效时间；重启后恢复并重新评估待确认计划。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from typing import Any, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .gating import build_stages, evaluate_application, quantize_money
from .models import FundingMix, Milestone, RenovationPlan
from .planning_json import canonical_json, decimal_text, digest
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "finance": {"release.write", "report.read"},
    "township": {"resource.write", "plan.write", "plan.confirm", "plan.lifecycle", "report.read"},
    "reviewer": {"exemption.write", "milestone.verify", "report.read"},
    "auditor": {"report.read", "audit.read"},
}

ZERO = Decimal("0")
HUNDRED = Decimal("100")
SYSTEM_ACTOR = "system"


class RenovationFundingService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)
        # 重启后恢复待确认计划：失效豁免、迟到预算调整都在此刻重新评估。
        self.recovery_summary = self.recover_pending()

    # ---------- 基础 ----------

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _today(self) -> str:
        return self._now()[:10]

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM funding_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM funding_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO funding_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO funding_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    # ---------- 资金额度（带版本与有效期） ----------

    def publish_release(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """财政人员发布额度版本。

        迟到的预算调整以新版本写入；旧版本转为 superseded，其冻结/已拨金额
        原样保留——已确认计划继续占用旧版本，只有尚未确认的计划在重新评估
        时适用最新版本。
        """
        self._require(actor_id, "release.write")
        release_id = str(raw.get("release_id", "")).strip()
        if not release_id:
            raise ValidationFailed("release_id 不能为空")
        fiscal_year = raw.get("fiscal_year")
        if isinstance(fiscal_year, bool) or not isinstance(fiscal_year, int) or not 2000 <= fiscal_year <= 2100:
            raise ValidationFailed("fiscal_year 必须是 2000 到 2100 的整数")
        total_amount = quantize_money(Decimal(str(raw.get("total_amount", "0"))))
        if total_amount <= 0:
            raise ValidationFailed("total_amount 必须大于 0")
        try:
            start = parse_utc(str(raw.get("effective_from")), "effective_from")
            end = parse_utc(str(raw.get("expires_at")), "expires_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if end <= start:
            raise ValidationFailed("expires_at 必须晚于 effective_from")
        note = str(raw.get("note", "")).strip()
        with transaction(self.connection, immediate=True):
            previous = self.connection.execute(
                "SELECT release_id,version FROM fund_releases WHERE fiscal_year=? AND state='active' "
                "ORDER BY version DESC LIMIT 1",
                (fiscal_year,),
            ).fetchone()
            version = 1 if previous is None else int(previous["version"]) + 1
            try:
                self.connection.execute(
                    "INSERT INTO fund_releases(release_id,fiscal_year,version,total_amount,"
                    "effective_from,expires_at,note,supersedes_release_id,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        release_id, fiscal_year, version, decimal_text(total_amount),
                        utc_text(start), utc_text(end), note,
                        None if previous is None else previous["release_id"], actor_id, self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("额度编号或年度版本冲突") from exc
            if previous is not None:
                self.connection.execute(
                    "UPDATE fund_releases SET state='superseded' WHERE release_id=?",
                    (previous["release_id"],),
                )
            self._audit("fund_release", release_id, "release.published", actor_id, {
                "fiscal_year": fiscal_year, "version": version, "total_amount": decimal_text(total_amount),
                "supersedes": None if previous is None else previous["release_id"],
                "effective_from": utc_text(start), "expires_at": utc_text(end), "note": note,
            })
            # 迟到调整只影响尚未确认的计划：在同事务内刷新其门禁结论。
            refreshed = self._refresh_pending(actor_id)
        return {**self.release(release_id), "refreshed_pending_plans": refreshed}

    def release(self, release_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM fund_releases WHERE release_id=?", (release_id,)
        ).fetchone()
        if row is None:
            raise NotFound("资金额度版本不存在")
        return self._release_view(row)

    def _release_view(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["available_amount"] = decimal_text(
            Decimal(row["total_amount"]) - self._year_committed(int(row["fiscal_year"]))
        )
        return result

    def _year_committed(self, fiscal_year: int) -> Decimal:
        """该年度所有版本已承诺（冻结 + 已拨）的资金总额。

        预算调整发布新版本后，已确认计划的占用仍然有效，
        待确认计划看到的是新总额减去历年已承诺部分。
        """
        rows = self.connection.execute(
            "SELECT frozen_amount,disbursed_amount FROM fund_releases WHERE fiscal_year=?",
            (fiscal_year,),
        ).fetchall()
        return sum(
            (Decimal(item["frozen_amount"]) + Decimal(item["disbursed_amount"]) for item in rows),
            ZERO,
        )

    def list_releases(self, fiscal_year: int | None = None) -> list[dict[str, Any]]:
        if fiscal_year is None:
            rows = self.connection.execute(
                "SELECT * FROM fund_releases ORDER BY fiscal_year DESC, version DESC"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM fund_releases WHERE fiscal_year=? ORDER BY version DESC", (fiscal_year,)
            ).fetchall()
        return [self._release_view(row) for row in rows]

    def _active_release(self, *, now: str | None = None) -> sqlite3.Row | None:
        """取当前时点生效的最高版本额度。

        新版本可能带未来生效日期（迟到的预算调整）：在生效前旧版本
        继续适用；生效后未确认计划自动跟随新版本，已确认计划不动。
        """
        moment = now or self._now()
        return self.connection.execute(
            "SELECT * FROM fund_releases WHERE state<>'closed' "
            "AND effective_from<=? AND expires_at>=? "
            "ORDER BY fiscal_year DESC, version DESC LIMIT 1",
            (moment, moment),
        ).fetchone()

    # ---------- 施工资源 ----------

    def register_resource(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "resource.write")
        resource_id = str(raw.get("resource_id", "")).strip()
        team_id = str(raw.get("team_id", "")).strip()
        name = str(raw.get("name", "")).strip()
        if not resource_id or not team_id or not name:
            raise ValidationFailed("resource_id、team_id、name 不能为空")
        capacity = Decimal(str(raw.get("capacity_units", "0")))
        if capacity <= 0 or capacity != capacity.to_integral_value():
            raise ValidationFailed("capacity_units 必须是正整数（可同时承接的项目数）")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO construction_resources(resource_id,team_id,name,capacity_units,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (resource_id, team_id, name, decimal_text(capacity), self._now()),
                )
                self._audit("resource", resource_id, "resource.registered", actor_id,
                            {"team_id": team_id, "capacity_units": decimal_text(capacity)})
        except sqlite3.IntegrityError as exc:
            raise Conflict("施工资源编号已存在") from exc
        return {"resource_id": resource_id, "team_id": team_id, "name": name,
                "capacity_units": decimal_text(capacity), "booked_units": "0", "available_units": decimal_text(capacity)}

    # ---------- 申报与可解释门禁 ----------

    def submit_plan(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "plan.write")
        plan = RenovationPlan.from_dict(raw)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM funding_idempotency "
            "WHERE scope='plan' AND idempotency_key=?",
            (plan.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同申报内容")
            return json.loads(stored["response_json"])
        with transaction(self.connection, immediate=True):
            try:
                self.connection.execute(
                    "INSERT INTO renovation_plans(plan_id,household_id,township_id,risk_grade,estimated_cost,"
                    "central_subsidy,local_match,household_self_raise,household_commitment,"
                    "latest_move_in_date,construction_team_id,definition_json,"
                    "idempotency_key,submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        plan.plan_id, plan.household_id, plan.township_id, plan.risk_grade,
                        decimal_text(plan.estimated_cost), decimal_text(plan.funding_mix.central_subsidy),
                        decimal_text(plan.funding_mix.local_match), decimal_text(plan.funding_mix.household_self_raise),
                        plan.household_commitment, plan.latest_move_in_date, plan.construction_team_id,
                        canonical_json(raw), plan.idempotency_key, actor_id, self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("计划编号或幂等键冲突") from exc
            row = self._plan_row(plan.plan_id)
            gate = self._compute_gate(row)
            self._write_gate_state(row, gate)
            response = self._plan_detail(plan.plan_id)
            self.connection.execute(
                "INSERT INTO funding_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                "VALUES('plan',?,?,?,?)",
                (plan.idempotency_key, request_digest, canonical_json(response), self._now()),
            )
            self._audit("plan", plan.plan_id, "plan.submitted", actor_id, {
                "risk_grade": plan.risk_grade, "estimated_cost": decimal_text(plan.estimated_cost),
                "township_id": plan.township_id, "household_id": plan.household_id,
                "gate_decision": response["gate"]["decision"],
                "blocking_reasons": response["gate"]["blocking_reasons"],
            })
        return response

    def _plan_row(self, plan_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM renovation_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFound("改造计划不存在")
        return row

    @staticmethod
    def _milestones(row: sqlite3.Row) -> list[Milestone]:
        definition = json.loads(row["definition_json"])
        return [Milestone.from_dict(item, index) for index, item in enumerate(definition["milestones"])]

    def _active_exemption(self, row: sqlite3.Row) -> sqlite3.Row | None:
        if row["exemption_id"] is None:
            return None
        exemption = self.connection.execute(
            "SELECT * FROM exemptions WHERE exemption_id=?", (row["exemption_id"],)
        ).fetchone()
        if exemption is None or exemption["state"] != "active":
            return None
        if exemption["expires_at"] < self._now():
            return None
        return exemption

    @staticmethod
    def _exemption_payload(exemption: sqlite3.Row) -> dict[str, object]:
        return {
            "exemption_id": exemption["exemption_id"],
            "authorized_by": exemption["authorized_by"],
            "reason": exemption["reason"],
            "granted_at": exemption["granted_at"],
            "expires_at": exemption["expires_at"],
        }

    def _compute_gate(self, row: sqlite3.Row) -> dict[str, Any]:
        """依据最新有效额度版本与有效豁免重算门禁结论和拨付阶段。"""
        release = self._active_release()
        exemption = self._active_exemption(row)
        available = ZERO
        if release is not None:
            available = Decimal(release["total_amount"]) - self._year_committed(int(release["fiscal_year"]))
        milestones = self._milestones(row)
        decision = evaluate_application(
            risk_grade=row["risk_grade"],
            estimated_cost=Decimal(row["estimated_cost"]),
            funding_mix=FundingMix(
                Decimal(row["central_subsidy"]),
                Decimal(row["local_match"]),
                Decimal(row["household_self_raise"]),
            ),
            milestones=milestones,
            latest_move_in_date=row["latest_move_in_date"],
            release_active=release is not None,
            release_expires_at=None if release is None else release["expires_at"],
            budget_available=available,
            exemption=None if exemption is None else self._exemption_payload(exemption),
            today=self._today(),
        )
        stages = build_stages(
            risk_grade=row["risk_grade"],
            estimated_cost=Decimal(row["estimated_cost"]),
            milestones=milestones,
            exemption=None if exemption is None else self._exemption_payload(exemption),
        )
        return {
            "release": release,
            "exemption": exemption,
            "decision": decision,
            "stages": stages,
            "milestones": milestones,
        }

    def _write_gate_state(self, row: sqlite3.Row, gate: dict[str, Any]) -> None:
        decision = gate["decision"]
        if row["state"] == "pending":
            # 迟到的预算调整只影响尚未确认的计划：待确认计划始终跟随最新有效版本。
            self.connection.execute(
                "UPDATE renovation_plans SET gate_decision=?,gate_reasons_json=?,gate_blocking_json=?,"
                "release_id=? WHERE plan_id=?",
                (
                    decision["decision"],
                    canonical_json(decision["reasons"]),
                    canonical_json(decision["blocking_reasons"]),
                    None if gate["release"] is None else gate["release"]["release_id"],
                    row["plan_id"],
                ),
            )
        else:
            self.connection.execute(
                "UPDATE renovation_plans SET gate_decision=?,gate_reasons_json=?,gate_blocking_json=? "
                "WHERE plan_id=?",
                (
                    decision["decision"],
                    canonical_json(decision["reasons"]),
                    canonical_json(decision["blocking_reasons"]),
                    row["plan_id"],
                ),
            )
        # 仅待确认计划允许按最新版本/豁免重写阶段；确认后阶段即锁定。
        if row["state"] == "pending":
            self.connection.execute("DELETE FROM plan_stages WHERE plan_id=?", (row["plan_id"],))
            for stage in gate["stages"]:
                self.connection.execute(
                    "INSERT INTO plan_stages(plan_id,seq,trigger_type,milestone_code,milestone_name,"
                    "planned_date,weight,planned_amount,state) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        row["plan_id"], stage.seq, stage.trigger, stage.milestone_code,
                        stage.milestone_name, stage.planned_date,
                        stage.as_dict()["weight"], decimal_text(stage.amount), "planned",
                    ),
                )

    def _refresh_pending(self, actor_id: str) -> int:
        """把迟到预算调整/豁免变化应用到所有待确认计划，返回刷新条数。"""
        rows = self.connection.execute(
            "SELECT * FROM renovation_plans WHERE state='pending' ORDER BY submitted_at,plan_id"
        ).fetchall()
        for row in rows:
            gate = self._compute_gate(row)
            self._write_gate_state(row, gate)
            self._audit("plan", row["plan_id"], "plan.gate_refreshed", actor_id, {
                "gate_decision": gate["decision"]["decision"],
                "blocking_reasons": gate["decision"]["blocking_reasons"],
                "release_id": None if gate["release"] is None else gate["release"]["release_id"],
            })
        return len(rows)

    def _refresh_gate(self, plan_id: str, actor_id: str) -> dict[str, Any]:
        row = self._plan_row(plan_id)
        gate = self._compute_gate(row)
        self._write_gate_state(row, gate)
        return gate

    # ---------- 紧急加固豁免 ----------

    def grant_exemption(
        self, actor_id: str, exemption_id: str, plan_id: str, reason: str, expires_at: str
    ) -> dict[str, Any]:
        """登记紧急加固豁免：必须记录授权人、理由与失效时间。"""
        self._require(actor_id, "exemption.write")
        reason = reason.strip() if isinstance(reason, str) else ""
        if not exemption_id.strip():
            raise ValidationFailed("exemption_id 不能为空")
        if not reason:
            raise ValidationFailed("豁免理由不能为空")
        try:
            expiry = parse_utc(expires_at, "expires_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if expiry <= self.clock.now():
            raise ValidationFailed("豁免失效时间必须晚于当前时间")
        with transaction(self.connection, immediate=True):
            plan = self._plan_row(plan_id)
            if plan["state"] not in ("pending", "confirmed", "active", "paused"):
                raise InvalidState("当前状态的计划不能追加豁免")
            existing = self.connection.execute(
                "SELECT exemption_id FROM exemptions WHERE plan_id=? AND state='active'", (plan_id,)
            ).fetchone()
            if existing is not None:
                raise Conflict("该计划已有生效中的豁免")
            try:
                self.connection.execute(
                    "INSERT INTO exemptions(exemption_id,plan_id,authorized_by,reason,granted_at,expires_at,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (exemption_id.strip(), plan_id, actor_id, reason, self._now(), utc_text(expiry), self._now()),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("豁免编号冲突") from exc
            self.connection.execute(
                "UPDATE renovation_plans SET exemption_id=?,revision=revision+1 WHERE plan_id=?",
                (exemption_id.strip(), plan_id),
            )
            gate = self._refresh_gate(plan_id, actor_id)
            self._audit("exemption", exemption_id.strip(), "exemption.granted", actor_id, {
                "plan_id": plan_id, "reason": reason, "expires_at": utc_text(expiry),
                "gate_decision": gate["decision"]["decision"],
            })
        return self.exemption(exemption_id.strip())

    def exemption(self, exemption_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM exemptions WHERE exemption_id=?", (exemption_id,)).fetchone()
        if row is None:
            raise NotFound("豁免不存在")
        result = dict(row)
        result["valid"] = row["state"] == "active" and row["expires_at"] >= self._now()
        return result

    def revoke_exemption(self, actor_id: str, exemption_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "exemption.write")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute("SELECT * FROM exemptions WHERE exemption_id=?", (exemption_id,)).fetchone()
            if row is None:
                raise NotFound("豁免不存在")
            if row["state"] != "active":
                raise InvalidState("豁免已失效，不能撤销")
            self.connection.execute(
                "UPDATE exemptions SET state='revoked',revoked_at=? WHERE exemption_id=?",
                (self._now(), exemption_id),
            )
            if row["plan_id"] is not None:
                self._refresh_gate(row["plan_id"], actor_id)
            self._audit("exemption", exemption_id, "exemption.revoked", actor_id, {"reason": reason})
        return self.exemption(exemption_id)

    # ---------- 确认：一次性锁定预算与施工资源 ----------

    def confirm_plan(self, actor_id: str, plan_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "plan.confirm")
        with transaction(self.connection, immediate=True):
            row = self._plan_row(plan_id)
            if row["state"] != "pending":
                raise InvalidState("只有待确认计划可以确认")
            if row["revision"] != expected_revision:
                raise Conflict("计划已被更新，请基于最新版本确认")
            gate = self._compute_gate(row)
            if gate["decision"]["decision"] != "approved":
                raise InvalidState(
                    "门禁未通过：" + "；".join(gate["decision"]["blocking_reasons"])
                )
            release = gate["release"]
            cost = Decimal(row["estimated_cost"])
            available = Decimal(release["total_amount"]) - self._year_committed(int(release["fiscal_year"]))
            if available < cost:
                # 与后续资源占用处于同一事务：此处抛错整体回滚，不会留下部分冻结。
                raise Conflict("额度余额不足以锁定整个项目预算")
            resource = self.connection.execute(
                "SELECT * FROM construction_resources WHERE team_id=? AND state='active' "
                "AND CAST(capacity_units AS REAL)-CAST(booked_units AS REAL)>=1 "
                "ORDER BY resource_id LIMIT 1",
                (row["construction_team_id"],),
            ).fetchone()
            if resource is None:
                raise Conflict("施工班组没有可锁定的档期资源")
            # 预算冻结与资源占用必须同时成功；任一条件失手则整个事务回滚。
            cursor = self.connection.execute(
                "UPDATE fund_releases SET frozen_amount=? "
                "WHERE release_id=? AND CAST(total_amount AS REAL) - ("
                "SELECT COALESCE(SUM(CAST(frozen_amount AS REAL)+CAST(disbursed_amount AS REAL)),0) "
                "FROM fund_releases WHERE fiscal_year=?) >= CAST(? AS REAL)",
                (
                    decimal_text(Decimal(release["frozen_amount"]) + cost),
                    release["release_id"],
                    int(release["fiscal_year"]),
                    decimal_text(cost),
                ),
            )
            if cursor.rowcount != 1:
                raise Conflict("预算锁定失败")
            cursor = self.connection.execute(
                "UPDATE construction_resources SET booked_units=? "
                "WHERE resource_id=? AND CAST(capacity_units AS REAL)-CAST(booked_units AS REAL)>=1",
                (decimal_text(Decimal(resource["booked_units"]) + 1), resource["resource_id"]),
            )
            if cursor.rowcount != 1:
                raise Conflict("施工资源锁定失败")
            # 确认即触发启动阶段拨付（trigger=plan_confirmed）。
            advance = gate["stages"][0].amount
            self.connection.execute(
                "UPDATE fund_releases SET frozen_amount=?,disbursed_amount=? WHERE release_id=?",
                (
                    decimal_text(Decimal(release["frozen_amount"]) + cost - advance),
                    decimal_text(Decimal(release["disbursed_amount"]) + advance),
                    release["release_id"],
                ),
            )
            self.connection.execute(
                "UPDATE renovation_plans SET state='confirmed',revision=revision+1,locked_budget=?,"
                "locked_resource_id=?,release_id=?,disbursed_total=?,gate_decision='approved',"
                "confirmed_at=? WHERE plan_id=?",
                (decimal_text(cost), resource["resource_id"], release["release_id"],
                 decimal_text(advance), self._now(), plan_id),
            )
            self.connection.execute(
                "UPDATE plan_stages SET state='disbursed' WHERE plan_id=? AND seq=1", (plan_id,)
            )
            self.connection.execute(
                "UPDATE plan_stages SET state='locked' WHERE plan_id=? AND seq>1", (plan_id,)
            )
            self.connection.execute(
                "INSERT INTO disbursements(plan_id,stage_seq,amount,basis,release_id,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (plan_id, 1, decimal_text(advance), "计划确认时锁定预算并预拨启动资金",
                 release["release_id"], actor_id, self._now()),
            )
            self._audit("plan", plan_id, "plan.confirmed", actor_id, {
                "release_id": release["release_id"], "release_version": release["version"],
                "locked_budget": decimal_text(cost), "resource_id": resource["resource_id"],
                "advance_amount": decimal_text(advance),
                "reasons": gate["decision"]["reasons"],
            })
        return self.plan_detail(actor_id, plan_id)

    # ---------- 验收：按实际完成量结算 ----------

    def record_verification(
        self, actor_id: str, plan_id: str, milestone_code: str, completion_percent: object, note: str
    ) -> dict[str, Any]:
        self._require(actor_id, "milestone.verify")
        percent = Decimal(str(completion_percent))
        if not percent.is_finite() or percent < ZERO or percent > HUNDRED:
            raise ValidationFailed("completion_percent 必须是 0 到 100 之间的数值")
        note = note.strip() if isinstance(note, str) else ""
        if not note:
            raise ValidationFailed("验收意见 note 不能为空")
        with transaction(self.connection, immediate=True):
            plan = self._plan_row(plan_id)
            if plan["state"] not in ("confirmed", "active"):
                raise InvalidState("只有已确认或执行中的计划可以登记节点验收")
            stage = self.connection.execute(
                "SELECT * FROM plan_stages WHERE plan_id=? AND milestone_code=?",
                (plan_id, milestone_code),
            ).fetchone()
            if stage is None:
                raise NotFound("施工节点不存在")
            if stage["state"] == "disbursed":
                raise Conflict("该节点已完成验收结算")
            if stage["state"] != "locked":
                raise InvalidState("节点阶段尚未进入可结算状态")
            amount = quantize_money(Decimal(stage["planned_amount"]) * percent / HUNDRED)
            release_id = plan["release_id"]
            release = self.connection.execute(
                "SELECT * FROM fund_releases WHERE release_id=?", (release_id,)
            ).fetchone()
            self.connection.execute(
                "UPDATE fund_releases SET frozen_amount=?,disbursed_amount=? WHERE release_id=?",
                (
                    decimal_text(Decimal(release["frozen_amount"]) - amount),
                    decimal_text(Decimal(release["disbursed_amount"]) + amount),
                    release_id,
                ),
            )
            cursor = self.connection.execute(
                "INSERT INTO milestone_verifications(plan_id,milestone_code,completion_percent,note,"
                "verified_by,verified_at) VALUES(?,?,?,?,?,?)",
                (plan_id, milestone_code, decimal_text(percent), note, actor_id, self._now()),
            )
            verification_id = int(cursor.lastrowid)
            self.connection.execute(
                "UPDATE plan_stages SET state='disbursed' WHERE stage_id=?", (stage["stage_id"],)
            )
            self.connection.execute(
                "INSERT INTO disbursements(plan_id,stage_seq,amount,basis,release_id,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    plan_id, stage["seq"], decimal_text(amount),
                    f"节点 {milestone_code} 验收完成度 {decimal_text(percent)}%，按实际完成量结算：{note}",
                    release_id, actor_id, self._now(),
                ),
            )
            self.connection.execute(
                "UPDATE renovation_plans SET state='active',revision=revision+1,"
                "disbursed_total=? WHERE plan_id=?",
                (decimal_text(Decimal(plan["disbursed_total"]) + amount), plan_id),
            )
            self._audit("plan", plan_id, "milestone.verified", actor_id, {
                "verification_id": verification_id, "milestone_code": milestone_code,
                "completion_percent": decimal_text(percent), "disbursed_amount": decimal_text(amount),
            })
        return self.plan_detail(actor_id, plan_id)

    # ---------- 暂停 / 恢复 / 取消 / 完成 ----------

    def pause_plan(self, actor_id: str, plan_id: str, reason: str) -> dict[str, Any]:
        """暂停：已验收工作量已经结算，未到期阶段继续冻结但停止后续拨付。"""
        self._require(actor_id, "plan.lifecycle")
        reason = reason.strip() if isinstance(reason, str) else ""
        if not reason:
            raise ValidationFailed("暂停原因不能为空")
        with transaction(self.connection, immediate=True):
            plan = self._plan_row(plan_id)
            if plan["state"] not in ("confirmed", "active"):
                raise InvalidState("只有已确认或执行中的计划可以暂停")
            self.connection.execute(
                "UPDATE renovation_plans SET state='paused',revision=revision+1 WHERE plan_id=?", (plan_id,)
            )
            self._audit("plan", plan_id, "plan.paused", actor_id, {
                "reason": reason, "settled_amount": plan["disbursed_total"],
            })
        return self.plan_detail(actor_id, plan_id)

    def resume_plan(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        self._require(actor_id, "plan.lifecycle")
        with transaction(self.connection, immediate=True):
            plan = self._plan_row(plan_id)
            if plan["state"] != "paused":
                raise InvalidState("只有已暂停的计划可以恢复")
            self.connection.execute(
                "UPDATE renovation_plans SET state='active',revision=revision+1 WHERE plan_id=?", (plan_id,)
            )
            self._audit("plan", plan_id, "plan.resumed", actor_id, {})
        return self.plan_detail(actor_id, plan_id)

    def _release_frozen_remainder(self, plan: sqlite3.Row) -> Decimal:
        """把未拨付的冻结余额退回额度可用性，并释放施工资源。"""
        remainder = Decimal(plan["locked_budget"]) - Decimal(plan["disbursed_total"])
        if remainder > ZERO:
            release = self.connection.execute(
                "SELECT * FROM fund_releases WHERE release_id=?", (plan["release_id"],)
            ).fetchone()
            if release is not None:
                self.connection.execute(
                    "UPDATE fund_releases SET frozen_amount=? WHERE release_id=?",
                    (decimal_text(Decimal(release["frozen_amount"]) - remainder), plan["release_id"]),
                )
        if plan["locked_resource_id"] is not None:
            resource = self.connection.execute(
                "SELECT * FROM construction_resources WHERE resource_id=?", (plan["locked_resource_id"],)
            ).fetchone()
            if resource is not None:
                self.connection.execute(
                    "UPDATE construction_resources SET booked_units=? WHERE resource_id=?",
                    (decimal_text(Decimal(resource["booked_units"]) - 1), plan["locked_resource_id"]),
                )
        return remainder

    def cancel_plan(self, actor_id: str, plan_id: str, reason: str) -> dict[str, Any]:
        """取消：按实际完成量结算，未发生的阶段跳过，冻结余额全部退回。"""
        self._require(actor_id, "plan.lifecycle")
        reason = reason.strip() if isinstance(reason, str) else ""
        if not reason:
            raise ValidationFailed("取消原因不能为空")
        with transaction(self.connection, immediate=True):
            plan = self._plan_row(plan_id)
            if plan["state"] not in ("pending", "confirmed", "active", "paused"):
                raise InvalidState("当前状态的计划不能取消")
            refunded = ZERO
            if plan["state"] != "pending":
                refunded = self._release_frozen_remainder(plan)
                self.connection.execute(
                    "UPDATE plan_stages SET state='skipped' WHERE plan_id=? AND state IN ('planned','locked')",
                    (plan_id,),
                )
            self.connection.execute(
                "UPDATE renovation_plans SET state='cancelled',revision=revision+1,"
                "settled_amount=disbursed_total,cancelled_at=? WHERE plan_id=?",
                (self._now(), plan_id),
            )
            self._audit("plan", plan_id, "plan.cancelled", actor_id, {
                "reason": reason, "settled_amount": plan["disbursed_total"], "refunded": decimal_text(refunded),
            })
        return self.plan_detail(actor_id, plan_id)

    def complete_plan(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        """竣工验收：结清实际完成量，退回未使用冻结，释放施工资源。"""
        self._require(actor_id, "plan.lifecycle")
        with transaction(self.connection, immediate=True):
            plan = self._plan_row(plan_id)
            if plan["state"] not in ("active", "paused"):
                raise InvalidState("只有执行中或暂停的计划可以竣工")
            pending_stages = self.connection.execute(
                "SELECT COUNT(*) c FROM plan_stages WHERE plan_id=? AND state='locked'", (plan_id,)
            ).fetchone()["c"]
            if pending_stages:
                raise InvalidState(f"还有 {pending_stages} 个节点未验收，不能竣工")
            refunded = self._release_frozen_remainder(plan)
            self.connection.execute(
                "UPDATE renovation_plans SET state='completed',revision=revision+1,"
                "settled_amount=disbursed_total,completed_at=? WHERE plan_id=?",
                (self._now(), plan_id),
            )
            self._audit("plan", plan_id, "plan.completed", actor_id, {
                "settled_amount": plan["disbursed_total"], "refunded": decimal_text(refunded),
            })
        return self.plan_detail(actor_id, plan_id)

    # ---------- 查询：可解释 ----------

    def plan_detail(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        return self._plan_detail(plan_id)

    def _plan_detail(self, plan_id: str) -> dict[str, Any]:
        row = self._plan_row(plan_id)
        gate = self._compute_gate(row) if row["state"] in ("pending",) else None
        stage_rows = self.connection.execute(
            "SELECT * FROM plan_stages WHERE plan_id=? ORDER BY seq", (plan_id,)
        ).fetchall()
        stages: list[dict[str, Any]] = []
        for stage_row in stage_rows:
            stages.append({
                "seq": stage_row["seq"],
                "trigger": stage_row["trigger_type"],
                "milestone_code": stage_row["milestone_code"],
                "milestone_name": stage_row["milestone_name"],
                "planned_date": stage_row["planned_date"],
                "weight": stage_row["weight"],
                "planned_amount": stage_row["planned_amount"],
                "state": stage_row["state"],
            })
        disbursements = [
            dict(item) for item in self.connection.execute(
                "SELECT disbursement_id,stage_seq,amount,basis,created_by,created_at "
                "FROM disbursements WHERE plan_id=? ORDER BY stage_seq", (plan_id,)
            ).fetchall()
        ]
        verifications = [
            dict(item) for item in self.connection.execute(
                "SELECT verification_id,milestone_code,completion_percent,note,verified_by,verified_at "
                "FROM milestone_verifications WHERE plan_id=? ORDER BY verification_id", (plan_id,)
            ).fetchall()
        ]
        release_view = None
        if row["release_id"] is not None:
            release_row = self.connection.execute(
                "SELECT * FROM fund_releases WHERE release_id=?", (row["release_id"],)
            ).fetchone()
            if release_row is not None:
                release_view = {
                    "release_id": release_row["release_id"],
                    "fiscal_year": release_row["fiscal_year"],
                    "version": release_row["version"],
                    "state": release_row["state"],
                    "expires_at": release_row["expires_at"],
                }
        result = {
            "plan_id": row["plan_id"],
            "household_id": row["household_id"],
            "township_id": row["township_id"],
            "risk_grade": row["risk_grade"],
            "estimated_cost": row["estimated_cost"],
            "funding_mix": {
                "central_subsidy": row["central_subsidy"],
                "local_match": row["local_match"],
                "household_self_raise": row["household_self_raise"],
            },
            "household_commitment": row["household_commitment"],
            "latest_move_in_date": row["latest_move_in_date"],
            "construction_team_id": row["construction_team_id"],
            "state": row["state"],
            "revision": row["revision"],
            "locked_budget": row["locked_budget"],
            "locked_resource_id": row["locked_resource_id"],
            "disbursed_total": row["disbursed_total"],
            "settled_amount": row["settled_amount"],
            "submitted_at": row["submitted_at"],
            "confirmed_at": row["confirmed_at"],
            "completed_at": row["completed_at"],
            "fund_release": release_view,
            "gate": {
                "decision": row["gate_decision"],
                "reasons": json.loads(row["gate_reasons_json"]),
                "blocking_reasons": json.loads(row["gate_blocking_json"]),
            },
            "exemption": None
            if gate is None or gate["exemption"] is None
            else self._exemption_payload(gate["exemption"]),
            "stages": stages,
            "disbursements": disbursements,
            "verifications": verifications,
        }
        # 已确认计划的豁免信息直接取关联记录。
        if result["exemption"] is None and row["exemption_id"] is not None:
            exemption_row = self.connection.execute(
                "SELECT * FROM exemptions WHERE exemption_id=?", (row["exemption_id"],)
            ).fetchone()
            if exemption_row is not None and exemption_row["state"] == "active":
                result["exemption"] = self._exemption_payload(exemption_row)
        return result

    def plan_explanation(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        """后台说明：项目为何获批、延期或需要豁免。"""
        self._require(actor_id, "report.read")
        detail = self._plan_detail(plan_id)
        events = [
            dict(item) for item in self.connection.execute(
                "SELECT event_id,event_type,actor_id,payload_json,created_at "
                "FROM funding_audit_events WHERE entity_type='plan' AND entity_id=? ORDER BY event_id",
                (plan_id,),
            ).fetchall()
        ]
        exemption_events = [
            dict(item) for item in self.connection.execute(
                "SELECT e.event_id,e.event_type,e.actor_id,e.payload_json,e.created_at "
                "FROM funding_audit_events e JOIN exemptions x ON e.entity_id=x.exemption_id "
                "WHERE x.plan_id=? ORDER BY e.event_id",
                (plan_id,),
            ).fetchall()
        ]
        for item in events + exemption_events:
            item["payload"] = json.loads(item.pop("payload_json"))
        headline = self._headline(detail)
        return {
            "plan_id": plan_id,
            "state": detail["state"],
            "headline": headline,
            "gate": detail["gate"],
            "fund_release": detail["fund_release"],
            "exemption": detail["exemption"],
            "stages": detail["stages"],
            "timeline": events + exemption_events,
        }

    @staticmethod
    def _headline(detail: Mapping[str, Any]) -> str:
        gate = detail["gate"]
        if detail["state"] in ("confirmed", "active", "completed"):
            return f"项目已获批：{('；'.join(gate['reasons']) or '满足全部资金门禁条件')}"
        if detail["state"] in ("cancelled", "expired") or gate.get("decision") == "deferred":
            return "项目延期：" + "；".join(gate.get("blocking_reasons") or ("等待重新评估",))
        if detail["exemption"]:
            return (
                f"项目获批并持紧急加固豁免推进，授权人 {detail['exemption']['authorized_by']}："
                + "；".join(gate.get("reasons") or ())
            )
        if gate.get("decision") == "approved":
            return "项目已获批（待确认）：" + "；".join(gate.get("reasons") or ("满足全部资金门禁条件",))
        return "项目待确认，门禁结论：" + str(gate.get("decision"))

    def list_plans(self, actor_id: str, state: str | None = None) -> list[dict[str, Any]]:
        self._require(actor_id, "report.read")
        if state is not None:
            rows = self.connection.execute(
                "SELECT plan_id FROM renovation_plans WHERE state=? ORDER BY submitted_at,plan_id", (state,)
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT plan_id FROM renovation_plans ORDER BY submitted_at,plan_id"
            ).fetchall()
        summaries: list[dict[str, Any]] = []
        for row in rows:
            detail = self._plan_detail(row["plan_id"])
            summaries.append({
                "plan_id": detail["plan_id"],
                "state": detail["state"],
                "risk_grade": detail["risk_grade"],
                "estimated_cost": detail["estimated_cost"],
                "disbursed_total": detail["disbursed_total"],
                "settled_amount": detail["settled_amount"],
                "gate_decision": detail["gate"]["decision"],
                "revision": detail["revision"],
                "release_id": None if detail["fund_release"] is None else detail["fund_release"]["release_id"],
            })
        return summaries

    # ---------- 重启恢复 ----------

    def recover_pending(self) -> dict[str, Any]:
        """服务启动时恢复待确认计划并刷新时效状态。"""
        with transaction(self.connection, immediate=True):
            now = self._now()
            # 过期豁免置为 expired。
            self.connection.execute(
                "UPDATE exemptions SET state='expired' WHERE state='active' AND expires_at<?", (now,)
            )
            expired_exemptions = self.connection.execute("SELECT changes()").fetchone()[0]
            pending = self.connection.execute(
                "SELECT * FROM renovation_plans WHERE state='pending' ORDER BY submitted_at,plan_id"
            ).fetchall()
            approved = deferred = 0
            for row in pending:
                gate = self._compute_gate(row)
                self._write_gate_state(row, gate)
                if gate["decision"]["decision"] == "approved":
                    approved += 1
                else:
                    deferred += 1
            # 超过最迟入住日期仍未确认的计划不可再按期完工。
            self.connection.execute(
                "UPDATE renovation_plans SET state='expired' WHERE state='pending' AND latest_move_in_date<?",
                (self._today(),),
            )
            expired_plans = self.connection.execute("SELECT changes()").fetchone()[0]
            summary = {
                "recovered_pending": len(pending),
                "approved": approved,
                "deferred": deferred,
                "expired_exemptions": expired_exemptions,
                "expired_plans": expired_plans,
                "recovered_at": now,
            }
            if pending or expired_exemptions:
                self._audit("system", "startup", "system.recovered", SYSTEM_ACTOR, summary)
        return summary

    # ---------- 审计链 ----------

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM funding_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
