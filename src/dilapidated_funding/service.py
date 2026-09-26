"""危房改造分期资金门禁应用服务。

关键不变量：
- 确认计划时预算冻结与施工资源锁定在同一个 IMMEDIATE 事务内完成，失败整体回滚，不留部分冻结；
- 预算发布带版本号与有效期，新版本只作用于尚未确认的计划，已确认计划保留版本外键；
- 紧急加固豁免必须记录授权人、理由与失效时间，且只能放宽可豁免门禁；
- 验收、暂停（恢复）、取消一律按实际完成量结算，预算冻结随结算转支付或释放。
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from typing import Any, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import (
    EmergencyExemptionInput,
    BudgetVersionInput,
    MilestoneInput,
    ProjectApplication,
)
from .numeric import ZERO, canonical_json, decimal_text, digest, money
from .policy import (
    build_stages,
    evaluate_gates,
    explain_eligibility,
    priority_score,
    settle_amounts,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "finance": {"budget.write", "report.read"},
    "township": {"project.write", "resource.write", "project.evaluate", "report.read"},
    "housing": {"project.evaluate", "project.settle", "report.read"},
    "authority": {"exemption.grant", "project.cancel", "report.read", "audit.read"},
    "auditor": {"report.read", "audit.read"},
}

TERMINAL_STATES = {"completed", "cancelled"}
WORKING_STATES = {"confirmed", "in_progress", "delayed"}


class FundGateService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # ----- 基础设 -----

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _today(self) -> str:
        return self.clock.now().date().isoformat()

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM fund_users WHERE user_id=?", (user_id,)
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
            "SELECT event_hash FROM fund_audit_events ORDER BY event_id DESC LIMIT 1"
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
        event_hash = digest(body)
        self.connection.execute(
            "INSERT INTO fund_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
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

    def _timeline(
        self,
        project_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        self.connection.execute(
            "INSERT INTO project_timeline(project_id,event_type,actor_id,payload_json,created_at) "
            "VALUES(?,?,?,?,?)",
            (project_id, event_type, actor_id, canonical_json(payload), self._now()),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO fund_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    # ----- 预算版本 -----

    def publish_budget(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """财政人员发布带版本号和有效期的资金额度。

        新版本会把同一预算池的历史版本标记为 superseded；历史版本上的冻结/支付不受影响，
        迟到的版本号（不大于当前最大版本）一律拒绝。
        """
        self._require(actor_id, "budget.write")
        budget = BudgetVersionInput.from_dict(raw)
        now_text = self._now()
        valid_from = utc_text(parse_utc(budget.valid_from))
        valid_to = utc_text(parse_utc(budget.valid_to))
        with transaction(self.connection, immediate=True):
            latest = self.connection.execute(
                "SELECT MAX(version) AS version FROM budget_versions WHERE budget_id=?",
                (budget.budget_id,),
            ).fetchone()
            if latest["version"] is not None and budget.version <= int(latest["version"]):
                raise Conflict(
                    f"预算版本号必须大于当前最新版本 {latest['version']}，迟到调整请使用新版本号"
                )
            self.connection.execute(
                "UPDATE budget_versions SET state='superseded' "
                "WHERE budget_id=? AND state='active' AND valid_to<?",
                (budget.budget_id, valid_from),
            )
            self.connection.execute(
                "INSERT INTO budget_versions(budget_id,version,total_amount,valid_from,valid_to,note,"
                "published_by,published_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    budget.budget_id,
                    budget.version,
                    decimal_text(budget.total_amount),
                    valid_from,
                    valid_to,
                    budget.note,
                    actor_id,
                    now_text,
                ),
            )
            self._audit("budget", budget.budget_id, "budget.published", actor_id, {
                "version": budget.version,
                "total_amount": decimal_text(budget.total_amount),
                "valid_from": valid_from,
                "valid_to": valid_to,
            })
        return self.budget_version(budget.budget_id, budget.version)

    def budget_version(self, budget_id: str, version: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM budget_versions WHERE budget_id=? AND version=?",
            (budget_id, version),
        ).fetchone()
        if row is None:
            raise NotFound("预算版本不存在")
        result = dict(row)
        result["available_amount"] = decimal_text(
            money(Decimal(row["total_amount"]) - Decimal(row["held_amount"]) - Decimal(row["paid_amount"]))
        )
        return result

    def _current_budget(self, budget_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM budget_versions WHERE budget_id=? ORDER BY version DESC LIMIT 1",
            (budget_id,),
        ).fetchone()

    def _applicable_budget(self, budget_id: str, at_text: str) -> sqlite3.Row | None:
        """当前时刻适用的预算版本：有效期覆盖该时刻的最高版本。

        新版本 valid_from 未到之前，仍在有效期内的老版本继续适用；
        迟到发布（版本号不大于最新版本）在发布环节即被拒绝。
        """
        return self.connection.execute(
            "SELECT * FROM budget_versions WHERE budget_id=? AND valid_from<=? AND valid_to>=? "
            "ORDER BY version DESC LIMIT 1",
            (budget_id, at_text, at_text),
        ).fetchone()

    def register_resource(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """乡镇登记施工资源容量（可同时开工的改造项目数），可调整。"""
        self._require(actor_id, "resource.write")
        township_id = raw.get("township_id")
        if not isinstance(township_id, str) or not township_id.strip():
            raise ValidationFailed("township_id 不能为空")
        capacity = raw.get("capacity")
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 0:
            raise ValidationFailed("capacity 必须是非负整数")
        held = self.connection.execute(
            "SELECT COUNT(*) AS n FROM project_resource_locks WHERE township_id=? AND state='held'",
            (township_id,),
        ).fetchone()["n"]
        if capacity < held:
            raise Conflict(f"当前已有 {held} 个项目锁定施工资源，容量不能调减到该值以下")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO construction_resources(township_id,capacity,note,updated_by,updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(township_id) DO UPDATE SET "
                "capacity=excluded.capacity,note=excluded.note,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                (township_id, capacity, str(raw.get("note", "")), actor_id, self._now()),
            )
            self._audit("resource", township_id, "resource.registered", actor_id, {"capacity": capacity})
        row = self.connection.execute(
            "SELECT * FROM construction_resources WHERE township_id=?", (township_id,)
        ).fetchone()
        return dict(row)

    def _resource_available(self, township_id: str) -> tuple[bool, int, int]:
        row = self.connection.execute(
            "SELECT capacity FROM construction_resources WHERE township_id=?", (township_id,)
        ).fetchone()
        if row is None:
            return False, 0, 0
        held = self.connection.execute(
            "SELECT COUNT(*) AS n FROM project_resource_locks WHERE township_id=? AND state='held'",
            (township_id,),
        ).fetchone()["n"]
        return held < int(row["capacity"]), held, int(row["capacity"])

    # ----- 项目申报与评估 -----

    def submit_project(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "project.write")
        application = ProjectApplication.from_dict(raw)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM fund_idempotency "
            "WHERE scope='project' AND idempotency_key=?",
            (application.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同申报内容")
            return json.loads(stored["response_json"])
        if self._current_budget(application.budget_id) is None:
            raise NotFound("预算池不存在，请先由财政发布预算版本")
        with transaction(self.connection, immediate=True):
            try:
                self.connection.execute(
                    "INSERT INTO projects(project_id,household_id,township_id,budget_id,appraisal_grade,"
                    "risk_level,latest_movein_date,central_amount,local_amount,household_amount,"
                    "subsidy_amount,submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        application.project_id,
                        application.household_id,
                        application.township_id,
                        application.budget_id,
                        application.appraisal_grade,
                        application.risk_level,
                        application.latest_movein_date,
                        decimal_text(application.central_amount),
                        decimal_text(application.local_amount),
                        decimal_text(application.household_amount),
                        decimal_text(application.subsidy_amount),
                        actor_id,
                        self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("项目编号冲突") from exc
            for ordinal, milestone in enumerate(application.milestones):
                self.connection.execute(
                    "INSERT INTO project_milestones(project_id,code,name,plan_date,weight_percent,ordinal) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        application.project_id,
                        milestone.code,
                        milestone.name,
                        milestone.plan_date,
                        decimal_text(milestone.weight_percent),
                        ordinal,
                    ),
                )
            self._timeline(application.project_id, "project.submitted", actor_id, {
                "household_id": application.household_id,
                "risk_level": application.risk_level,
                "appraisal_grade": application.appraisal_grade,
            })
            evaluation = self._evaluate_locked(application, trigger="submit", actor_id=actor_id)
            response = {
                "project_id": application.project_id,
                "state": "submitted",
                "revision": 1,
                "evaluation": evaluation,
            }
            self.connection.execute(
                "INSERT INTO fund_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                "VALUES('project',?,?,?,?)",
                (application.idempotency_key, request_digest, canonical_json(response), self._now()),
            )
            self._audit("project", application.project_id, "project.submitted", actor_id,
                        {"idempotency_key": application.idempotency_key})
        return response

    def _expire_due_exemptions(self) -> None:
        rows = self.connection.execute(
            "SELECT exemption_id,project_id FROM exemptions WHERE state='active' AND expires_at<=?",
            (self._now(),),
        ).fetchall()
        for row in rows:
            self.connection.execute(
                "UPDATE exemptions SET state='expired',revoked_at=? WHERE exemption_id=? AND state='active'",
                (self._now(), row["exemption_id"]),
            )
            self._timeline(row["project_id"], "exemption.expired", "system", {"exemption_id": row["exemption_id"]})

    def _active_exemption(self, project_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM exemptions WHERE project_id=? AND state='active' AND expires_at>? "
            "ORDER BY exemption_id DESC LIMIT 1",
            (project_id, self._now()),
        ).fetchone()

    def _load_application(self, project_id: str) -> ProjectApplication:
        row = self.connection.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if row is None:
            raise NotFound("项目不存在")
        milestones = []
        for item in self.connection.execute(
            "SELECT * FROM project_milestones WHERE project_id=? ORDER BY ordinal", (project_id,)
        ).fetchall():
            milestones.append(MilestoneInput(
                code=item["code"],
                name=item["name"],
                plan_date=item["plan_date"],
                weight_percent=Decimal(item["weight_percent"]),
            ))
        return ProjectApplication(
            project_id=row["project_id"],
            household_id=row["household_id"],
            township_id=row["township_id"],
            budget_id=row["budget_id"],
            appraisal_grade=row["appraisal_grade"],
            risk_level=row["risk_level"],
            latest_movein_date=row["latest_movein_date"],
            central_amount=Decimal(row["central_amount"]),
            local_amount=Decimal(row["local_amount"]),
            household_amount=Decimal(row["household_amount"]),
            milestones=tuple(milestones),
            idempotency_key=f"stored-{project_id}",
        )

    def _evaluate_locked(
        self,
        application: ProjectApplication,
        *,
        trigger: str,
        actor_id: str,
        already_started: bool = False,
    ) -> dict[str, Any]:
        """调用方已持有 IMMEDIATE 事务。"""
        self._expire_due_exemptions()
        now_text = self._now()
        budget_row = self._applicable_budget(application.budget_id, now_text)
        if budget_row is None:
            # 没有任何在有效期内的版本：门禁仍需落库一条评估，挂在最新版本上说明失效原因。
            budget_row = self._current_budget(application.budget_id)
        if budget_row is None:
            raise NotFound("预算池不存在")
        budget_valid = budget_row["valid_from"] <= now_text <= budget_row["valid_to"]
        remaining = money(
            Decimal(budget_row["total_amount"])
            - Decimal(budget_row["held_amount"])
            - Decimal(budget_row["paid_amount"])
        )
        resource_ok, _, _ = self._resource_available(application.township_id)
        exemption_row = self._active_exemption(application.project_id)
        exempted_gates: list[str] = []
        if exemption_row is not None:
            exempted_gates = json.loads(exemption_row["gates_json"])
        gates = evaluate_gates(
            application,
            as_of_date=self._today(),
            budget_remaining=remaining,
            budget_valid=budget_valid,
            budget_valid_to=budget_row["valid_to"],
            resource_available=resource_ok,
            active_exemption_gates=exempted_gates,
            already_started=already_started,
        )
        eligible, reasons = explain_eligibility(gates)
        score = priority_score(application)
        stages = build_stages(application, application.subsidy_amount)
        self.connection.execute(
            "UPDATE projects SET priority_score=? WHERE project_id=?",
            (decimal_text(score), application.project_id),
        )
        cursor = self.connection.execute(
            "INSERT INTO project_evaluations(project_id,budget_id,budget_version,decision,priority_score,"
            "gates_json,exempted_gates_json,stages_json,trigger,evaluated_by,evaluated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                application.project_id,
                application.budget_id,
                budget_row["version"],
                "eligible" if eligible else "ineligible",
                decimal_text(score),
                canonical_json([gate.as_dict() for gate in gates]),
                canonical_json(exempted_gates),
                canonical_json(stages),
                trigger,
                actor_id,
                now_text,
            ),
        )
        self._timeline(application.project_id, "project.evaluated", actor_id, {
            "evaluation_id": cursor.lastrowid,
            "decision": "eligible" if eligible else "ineligible",
            "budget_version": budget_row["version"],
            "blocking_gates": [gate.code for gate in gates if not gate.passed],
            "exempted_gates": exempted_gates,
        })
        return {
            "evaluation_id": cursor.lastrowid,
            "decision": "eligible" if eligible else "ineligible",
            "priority_score": decimal_text(score),
            "budget_id": application.budget_id,
            "budget_version": budget_row["version"],
            "budget_valid": budget_valid,
            "gates": [gate.as_dict() for gate in gates],
            "exempted_gates": exempted_gates,
            "reasons": reasons,
            "stages": stages,
            "evaluated_at": now_text,
            "trigger": trigger,
        }

    def evaluate_project(self, actor_id: str, project_id: str) -> dict[str, Any]:
        """用最新预算版本与当前豁免重新评估尚未确认的计划。"""
        self._require(actor_id, "project.evaluate")
        row = self.connection.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if row is None:
            raise NotFound("项目不存在")
        if row["state"] != "submitted":
            raise InvalidState("只有待确认计划可以重新评估")
        application = self._load_application(project_id)
        with transaction(self.connection, immediate=True):
            result = self._evaluate_locked(application, trigger="refresh", actor_id=actor_id)
            self._audit("project", project_id, "project.evaluated", actor_id, {
                "decision": result["decision"],
                "budget_version": result["budget_version"],
            })
        return result

    # ----- 紧急加固豁免 -----

    def grant_exemption(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """紧急加固豁免：记录授权人、理由、失效时间与放宽的门禁清单。"""
        self._require(actor_id, "exemption.grant")
        exemption = EmergencyExemptionInput.from_dict(raw)
        if exemption.grantor_id != actor_id:
            raise Forbidden("授权人必须与当前签发账号一致")
        project = self.connection.execute(
            "SELECT state FROM projects WHERE project_id=?", (exemption.project_id,)
        ).fetchone()
        if project is None:
            raise NotFound("项目不存在")
        if parse_utc(exemption.expires_at) <= self.clock.now():
            raise ValidationFailed("失效时间必须晚于当前时间")
        expires_at = utc_text(parse_utc(exemption.expires_at))
        with transaction(self.connection, immediate=True):
            self._expire_due_exemptions()
            existing = self._active_exemption(exemption.project_id)
            if existing is not None:
                raise Conflict("该项目已有生效中的豁免，请勿重复授权")
            cursor = self.connection.execute(
                "INSERT INTO exemptions(project_id,grantor_id,reason,expires_at,gates_json,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    exemption.project_id,
                    exemption.grantor_id,
                    exemption.reason,
                    expires_at,
                    canonical_json(list(exemption.gates)),
                    self._now(),
                ),
            )
            exemption_id = int(cursor.lastrowid)
            self._timeline(exemption.project_id, "exemption.granted", actor_id, {
                "exemption_id": exemption_id,
                "grantor_id": exemption.grantor_id,
                "reason": exemption.reason,
                "expires_at": expires_at,
                "gates": list(exemption.gates),
            })
            self._audit("project", exemption.project_id, "exemption.granted", actor_id, {
                "exemption_id": exemption_id,
                "gates": list(exemption.gates),
                "expires_at": expires_at,
            })
        return {
            "exemption_id": exemption_id,
            "project_id": exemption.project_id,
            "grantor_id": exemption.grantor_id,
            "reason": exemption.reason,
            "expires_at": expires_at,
            "gates": list(exemption.gates),
            "state": "active",
        }

    def revoke_exemption(self, actor_id: str, exemption_id: int) -> dict[str, Any]:
        self._require(actor_id, "exemption.grant")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT * FROM exemptions WHERE exemption_id=?", (exemption_id,)
            ).fetchone()
            if row is None:
                raise NotFound("豁免不存在")
            if row["state"] != "active" or parse_utc(row["expires_at"]) <= self.clock.now():
                raise InvalidState("豁免已失效，无需撤销")
            self.connection.execute(
                "UPDATE exemptions SET state='revoked',revoked_at=? WHERE exemption_id=?",
                (self._now(), exemption_id),
            )
            self._timeline(row["project_id"], "exemption.revoked", actor_id, {"exemption_id": exemption_id})
            self._audit("project", row["project_id"], "exemption.revoked", actor_id,
                        {"exemption_id": exemption_id})
        return {"exemption_id": exemption_id, "state": "revoked"}

    # ----- 确认计划：一次性锁定预算和施工资源 -----

    def _record_lock_failure(self, project_id: str, event_type: str, actor_id: str, reason: str) -> None:
        """锁定失败本身不产生冻结，但要独立留痕说明原因（与锁定事务分离）。"""
        with transaction(self.connection, immediate=True):
            self._timeline(project_id, event_type, actor_id, {"reason": reason, "at": self._now()})
            self._audit("project", project_id, event_type, actor_id, {"reason": reason})

    def confirm_project(self, actor_id: str, project_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "project.evaluate")
        row = self.connection.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if row is None:
            raise NotFound("项目不存在")
        if row["state"] != "submitted":
            raise InvalidState("只有待确认计划可以确认")
        if row["revision"] != expected_revision:
            raise Conflict("计划版本已变化，请刷新后重试")
        application = self._load_application(project_id)
        # 第一阶段：按最新预算版本与豁免重新评估并落库，失败原因保留给后台解释。
        with transaction(self.connection, immediate=True):
            evaluation = self._evaluate_locked(application, trigger="confirm", actor_id=actor_id)
            self._audit("project", project_id, "project.confirm_attempt", actor_id, {
                "decision": evaluation["decision"],
                "budget_version": evaluation["budget_version"],
            })
        if evaluation["decision"] != "eligible":
            blocked = [gate["code"] for gate in evaluation["gates"] if not gate["passed"]]
            raise InvalidState(f"门禁未全部通过，不能确认计划：{', '.join(blocked)}")
        subsidy = application.subsidy_amount
        # 第二阶段：预算冻结与施工资源锁定在同一事务内二次校验并提交，失败整体回滚不留部分冻结。
        try:
            with transaction(self.connection, immediate=True):
                current = self.connection.execute(
                    "SELECT state,revision FROM projects WHERE project_id=?", (project_id,)
                ).fetchone()
                if current["state"] != "submitted" or current["revision"] != expected_revision:
                    raise Conflict("计划已被其他操作变更，锁定整体放弃")
                budget = self.connection.execute(
                    "SELECT * FROM budget_versions WHERE budget_id=? AND version=?",
                    (application.budget_id, evaluation["budget_version"]),
                ).fetchone()
                remaining = money(
                    Decimal(budget["total_amount"]) - Decimal(budget["held_amount"]) - Decimal(budget["paid_amount"])
                )
                if remaining < subsidy:
                    raise InvalidState("预算余额不足，锁定已整体放弃")
                resource_ok, held, capacity = self._resource_available(application.township_id)
                if not resource_ok:
                    raise InvalidState("乡镇施工资源已满，锁定已整体放弃")
                self.connection.execute(
                    "UPDATE budget_versions SET held_amount=? WHERE budget_id=? AND version=?",
                    (decimal_text(money(Decimal(budget["held_amount"]) + subsidy)),
                     application.budget_id, budget["version"]),
                )
                self.connection.execute(
                    "INSERT INTO budget_reservations(project_id,budget_id,budget_version,amount,held_amount,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (project_id, application.budget_id, budget["version"],
                     decimal_text(subsidy), decimal_text(subsidy), self._now()),
                )
                self.connection.execute(
                    "INSERT INTO project_resource_locks(project_id,township_id,locked_at) VALUES(?,?,?)",
                    (project_id, application.township_id, self._now()),
                )
                for stage in evaluation["stages"]:
                    self.connection.execute(
                        "INSERT INTO disbursement_stages(project_id,milestone_code,name,plan_date,weight_percent,"
                        "cumulative_weight,amount_cny) VALUES(?,?,?,?,?,?,?)",
                        (
                            project_id,
                            stage["milestone_code"],
                            stage["name"],
                            stage["plan_date"],
                            stage["weight_percent"],
                            stage["cumulative_weight_percent"],
                            stage["amount_cny"],
                        ),
                    )
                cursor = self.connection.execute(
                    "UPDATE projects SET state='confirmed',budget_version=?,confirmed_at=?,revision=revision+1 "
                    "WHERE project_id=? AND state='submitted' AND revision=?",
                    (budget["version"], self._now(), project_id, expected_revision),
                )
                if cursor.rowcount != 1:
                    raise InvalidState("计划状态已变化，锁定整体放弃")
                self._timeline(project_id, "project.confirmed", actor_id, {
                    "budget_id": application.budget_id,
                    "budget_version": budget["version"],
                    "locked_amount": decimal_text(subsidy),
                    "resource_capacity": [held + 1, capacity],
                    "evaluation_id": evaluation["evaluation_id"],
                })
                self._audit("project", project_id, "project.confirmed", actor_id, {
                    "budget_version": budget["version"],
                    "locked_amount": decimal_text(subsidy),
                })
        except (Conflict, InvalidState) as exc:
            self._record_lock_failure(project_id, "project.confirm_failed", actor_id, str(exc))
            raise
        return {
            "project_id": project_id,
            "state": "confirmed",
            "revision": expected_revision + 1,
            "budget_id": application.budget_id,
            "budget_version": evaluation["budget_version"],
            "locked_amount": decimal_text(subsidy),
            "stages": evaluation["stages"],
        }

    # ----- 验收 / 暂停 / 取消：按实际完成量结算 -----

    def _settle_locked(
        self,
        project_id: str,
        completion_percent: Decimal,
        *,
        release_remaining: bool,
        final_state: str | None,
        allowed_states: set[str],
    ) -> dict[str, Any]:
        """调用方已持有 IMMEDIATE 事务。项目行在事务内重读，避免并发结算互相覆盖。已支付部分只增不减。"""
        project = self.connection.execute(
            "SELECT * FROM projects WHERE project_id=?", (project_id,)
        ).fetchone()
        if project is None:
            raise NotFound("项目不存在")
        if project["state"] not in allowed_states:
            raise InvalidState(f"项目当前状态 {project['state']} 不能执行该结算")
        stages = self.connection.execute(
            "SELECT * FROM disbursement_stages WHERE project_id=? ORDER BY stage_id",
            (project["project_id"],),
        ).fetchall()
        if not stages:
            raise InvalidState("项目尚未确认，没有拨付阶段")
        completion = money(min(Decimal("100"), max(ZERO, completion_percent)))
        settlement = settle_amounts([dict(row) for row in stages], completion)
        stage_payable = {row["milestone_code"]: Decimal(row["payable_cny"]) for row in settlement["stages"]}

        reservation = self.connection.execute(
            "SELECT * FROM budget_reservations WHERE project_id=? AND state='held' "
            "ORDER BY reservation_id DESC LIMIT 1",
            (project["project_id"],),
        ).fetchone()
        if reservation is None:
            raise InvalidState("项目没有生效中的预算冻结")
        held_before = Decimal(reservation["held_amount"])

        newly_paid = ZERO
        for stage in stages:
            target = max(stage_payable[stage["milestone_code"]], Decimal(stage["paid_amount"]))
            increment = money(target - Decimal(stage["paid_amount"]))
            newly_paid = money(newly_paid + increment)
            if release_remaining and target < Decimal(stage["amount_cny"]):
                stage_state = "cancelled"
            elif target > ZERO:
                stage_state = "paid"
            else:
                stage_state = stage["state"]
            if target > ZERO:
                self.connection.execute(
                    "UPDATE disbursement_stages SET paid_amount=?,state=?,"
                    "paid_at=COALESCE(paid_at,?) WHERE stage_id=?",
                    (decimal_text(target), stage_state, self._now(), stage["stage_id"]),
                )
            else:
                self.connection.execute(
                    "UPDATE disbursement_stages SET paid_amount=?,state=? WHERE stage_id=?",
                    (decimal_text(target), stage_state, stage["stage_id"]),
                )
            if target >= Decimal(stage["amount_cny"]):
                self.connection.execute(
                    "UPDATE project_milestones SET accepted_at=COALESCE(accepted_at,?) "
                    "WHERE project_id=? AND code=?",
                    (self._now(), project["project_id"], stage["milestone_code"]),
                )
        if newly_paid > held_before:
            raise InvalidState("本次结算金额超过冻结余额，事务回滚")
        released = money(held_before - newly_paid) if release_remaining else ZERO

        budget = self.connection.execute(
            "SELECT * FROM budget_versions WHERE budget_id=? AND version=?",
            (reservation["budget_id"], reservation["budget_version"]),
        ).fetchone()
        budget_held = money(Decimal(budget["held_amount"]) - newly_paid - released)
        budget_paid = money(Decimal(budget["paid_amount"]) + newly_paid)
        self.connection.execute(
            "UPDATE budget_versions SET held_amount=?,paid_amount=? WHERE budget_id=? AND version=?",
            (decimal_text(budget_held), decimal_text(budget_paid),
             reservation["budget_id"], reservation["budget_version"]),
        )
        reservation_state = reservation["state"]
        if release_remaining:
            reservation_state = "released" if final_state == "cancelled" else "settled"
        elif completion == Decimal("100"):
            reservation_state = "settled"
        self.connection.execute(
            "UPDATE budget_reservations SET held_amount=?,paid_amount=?,state=? WHERE reservation_id=?",
            (
                decimal_text(money(held_before - newly_paid - released)),
                decimal_text(money(Decimal(reservation["paid_amount"]) + newly_paid)),
                reservation_state,
                reservation["reservation_id"],
            ),
        )
        if release_remaining:
            self.connection.execute(
                "UPDATE project_resource_locks SET state='released',released_at=? "
                "WHERE project_id=? AND state='held'",
                (self._now(), project["project_id"]),
            )
        paid_total = money(Decimal(project["paid_amount"]) + newly_paid)
        self._update_project_settlement(project["project_id"], completion, paid_total, final_state)
        return {
            "completion_percent": decimal_text(completion),
            "paid_amount": decimal_text(paid_total),
            "newly_paid_amount": decimal_text(newly_paid),
            "released_amount": decimal_text(released),
            "stages": settlement["stages"],
        }

    def _update_project_settlement(
        self, project_id: str, completion: Decimal, paid_total: Decimal, final_state: str | None
    ) -> None:
        if final_state is None:
            self.connection.execute(
                "UPDATE projects SET completed_percent=?,paid_amount=?,state='in_progress' WHERE project_id=?",
                (decimal_text(completion), decimal_text(paid_total), project_id),
            )
        else:
            self.connection.execute(
                "UPDATE projects SET completed_percent=?,paid_amount=?,state=? WHERE project_id=?",
                (decimal_text(completion), decimal_text(paid_total), final_state, project_id),
            )

    def record_acceptance(
        self, actor_id: str, project_id: str, completion_percent: object, note: str = ""
    ) -> dict[str, Any]:
        """住建部门按现场实际完成量验收并触发拨付；完成 100% 即竣工。"""
        self._require(actor_id, "project.settle")
        try:
            completion = Decimal(str(completion_percent))
        except Exception as exc:  # noqa: BLE001
            raise ValidationFailed("completion_percent 必须是数值") from exc
        if not ZERO <= completion <= Decimal("100"):
            raise ValidationFailed("completion_percent 必须在 0 到 100 之间")
        project = self.connection.execute(
            "SELECT * FROM projects WHERE project_id=?", (project_id,)
        ).fetchone()
        if project is None:
            raise NotFound("项目不存在")
        if project["state"] not in WORKING_STATES:
            raise InvalidState("当前状态不能报验")
        with transaction(self.connection, immediate=True):
            result = self._settle_locked(
                project_id,
                completion,
                release_remaining=False,
                final_state="completed" if completion == Decimal("100") else None,
                allowed_states=WORKING_STATES,
            )
            if completion == Decimal("100"):
                self.connection.execute(
                    "UPDATE budget_reservations SET state='settled' WHERE project_id=? AND state='held'",
                    (project_id,),
                )
                self.connection.execute(
                    "UPDATE project_resource_locks SET state='released',released_at=? "
                    "WHERE project_id=? AND state='held'",
                    (self._now(), project_id),
                )
                self.connection.execute(
                    "UPDATE disbursement_stages SET state='paid' WHERE project_id=? AND state='planned'",
                    (project_id,),
                )
            self.connection.execute(
                "INSERT INTO settlements(project_id,kind,completion_percent,paid_amount,released_amount,"
                "note,actor_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (project_id, "acceptance", result["completion_percent"], result["paid_amount"],
                 result["released_amount"], note, actor_id, self._now()),
            )
            self._timeline(project_id, "project.accepted", actor_id, {
                "completion_percent": result["completion_percent"],
                "newly_paid_amount": result["newly_paid_amount"],
                "note": note,
            })
            self._audit("project", project_id, "project.accepted", actor_id, {
                "completion_percent": result["completion_percent"],
                "paid_amount": result["paid_amount"],
            })
        state = "completed" if completion == Decimal("100") else "in_progress"
        return {"project_id": project_id, "state": state, **result}

    def suspend_project(
        self, actor_id: str, project_id: str, completion_percent: object, note: str = ""
    ) -> dict[str, Any]:
        """暂停：按实际完成量结算已完工部分，剩余冻结预算与施工资源释放。"""
        self._require(actor_id, "project.settle")
        return self._close_partial(actor_id, project_id, completion_percent, "suspension", "suspended", note)

    def cancel_project(
        self, actor_id: str, project_id: str, completion_percent: object, note: str = ""
    ) -> dict[str, Any]:
        """取消：按实际完成量结算，未施工部分的冻结预算全部释放。"""
        self._require(actor_id, "project.cancel")
        return self._close_partial(actor_id, project_id, completion_percent, "cancellation", "cancelled", note)

    def _close_partial(
        self,
        actor_id: str,
        project_id: str,
        completion_percent: object,
        kind: str,
        final_state: str,
        note: str,
    ) -> dict[str, Any]:
        try:
            completion = Decimal(str(completion_percent))
        except Exception as exc:  # noqa: BLE001
            raise ValidationFailed("completion_percent 必须是数值") from exc
        if not ZERO <= completion <= Decimal("100"):
            raise ValidationFailed("completion_percent 必须在 0 到 100 之间")
        with transaction(self.connection, immediate=True):
            project = self.connection.execute(
                "SELECT * FROM projects WHERE project_id=?", (project_id,)
            ).fetchone()
            if project is None:
                raise NotFound("项目不存在")
            if project["state"] in TERMINAL_STATES:
                raise InvalidState("项目已结束，不能再次结算")
            if kind == "suspension" and project["state"] == "suspended":
                raise InvalidState("项目已暂停，请恢复后继续或直接取消")
            if project["state"] == "submitted":
                # 尚未确认、没有任何冻结：直接终结，不产生支付。
                self.connection.execute(
                    "UPDATE projects SET state=?,completed_percent='0' WHERE project_id=?",
                    (final_state, project_id),
                )
                result = {
                    "completion_percent": "0",
                    "paid_amount": project["paid_amount"],
                    "newly_paid_amount": "0.00",
                    "released_amount": "0.00",
                    "stages": [],
                }
            elif project["state"] == "suspended":
                # 暂停时已按完成量结算并释放冻结；取消只终结，金额维持暂停结算结果。
                self.connection.execute(
                    "UPDATE projects SET state=? WHERE project_id=?", (final_state, project_id)
                )
                result = {
                    "completion_percent": project["completed_percent"],
                    "paid_amount": project["paid_amount"],
                    "newly_paid_amount": "0.00",
                    "released_amount": "0.00",
                    "stages": [],
                }
            else:
                allowed = {"confirmed", "in_progress", "delayed"}
                if kind == "cancellation":
                    allowed = {"confirmed", "in_progress", "delayed"}
                result = self._settle_locked(
                    project_id,
                    completion,
                    release_remaining=True,
                    final_state=final_state,
                    allowed_states=allowed,
                )
            self.connection.execute(
                "INSERT INTO settlements(project_id,kind,completion_percent,paid_amount,released_amount,"
                "note,actor_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (project_id, kind, result["completion_percent"], result["paid_amount"],
                 result["released_amount"], note, actor_id, self._now()),
            )
            self._timeline(project_id, f"project.{kind}", actor_id, {
                "completion_percent": result["completion_percent"],
                "paid_amount": result["paid_amount"],
                "released_amount": result["released_amount"],
                "note": note,
            })
            self._audit("project", project_id, f"project.{kind}", actor_id, {
                "completion_percent": result["completion_percent"],
                "released_amount": result["released_amount"],
            })
        return {"project_id": project_id, "state": final_state, **result}

    def resume_project(self, actor_id: str, project_id: str) -> dict[str, Any]:
        """暂停后恢复：按当前预算版本与施工资源重新过门，重新冻结剩余补助。"""
        self._require(actor_id, "project.settle")
        project = self.connection.execute(
            "SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if project is None:
            raise NotFound("项目不存在")
        if project["state"] != "suspended":
            raise InvalidState("只有暂停中的项目可以恢复")
        application = self._load_application(project_id)
        with transaction(self.connection, immediate=True):
            evaluation = self._evaluate_locked(
                application, trigger="resume", actor_id=actor_id, already_started=True
            )
            self._audit("project", project_id, "project.resume_attempt", actor_id, {
                "decision": evaluation["decision"],
                "budget_version": evaluation["budget_version"],
            })
        if evaluation["decision"] != "eligible":
            blocked = [gate["code"] for gate in evaluation["gates"] if not gate["passed"]]
            raise InvalidState(f"恢复时门禁未通过：{', '.join(blocked)}")
        need_hold = money(application.subsidy_amount - Decimal(project["paid_amount"]))
        try:
            with transaction(self.connection, immediate=True):
                current = self.connection.execute(
                    "SELECT state FROM projects WHERE project_id=?", (project_id,)
                ).fetchone()
                if current["state"] != "suspended":
                    raise InvalidState("项目状态已变化，恢复已放弃")
                budget = self.connection.execute(
                    "SELECT * FROM budget_versions WHERE budget_id=? AND version=?",
                    (application.budget_id, evaluation["budget_version"]),
                ).fetchone()
                remaining = money(
                    Decimal(budget["total_amount"]) - Decimal(budget["held_amount"]) - Decimal(budget["paid_amount"])
                )
                if remaining < need_hold:
                    raise InvalidState("预算余额不足以重新冻结剩余补助，恢复已整体放弃")
                resource_ok, _, _ = self._resource_available(application.township_id)
                if not resource_ok:
                    raise InvalidState("乡镇施工资源已满，恢复已整体放弃")
                self.connection.execute(
                    "UPDATE budget_versions SET held_amount=? WHERE budget_id=? AND version=?",
                    (decimal_text(money(Decimal(budget["held_amount"]) + need_hold)),
                     application.budget_id, budget["version"]),
                )
                self.connection.execute(
                    "INSERT INTO budget_reservations(project_id,budget_id,budget_version,amount,held_amount,"
                    "paid_amount,created_at) VALUES(?,?,?,?,?,?,?)",
                    (project_id, application.budget_id, budget["version"],
                     decimal_text(application.subsidy_amount), decimal_text(need_hold),
                     decimal_text(Decimal(project["paid_amount"])), self._now()),
                )
                self.connection.execute(
                    "INSERT INTO project_resource_locks(project_id,township_id,locked_at) VALUES(?,?,?)",
                    (project_id, application.township_id, self._now()),
                )
                cursor = self.connection.execute(
                    "UPDATE projects SET state='in_progress',budget_version=? WHERE project_id=? AND state='suspended'",
                    (budget["version"], project_id),
                )
                if cursor.rowcount != 1:
                    raise InvalidState("项目状态已变化，恢复已整体放弃")
                self.connection.execute(
                    "INSERT INTO settlements(project_id,kind,completion_percent,paid_amount,note,actor_id,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (project_id, "resume", project["completed_percent"], project["paid_amount"], "", actor_id, self._now()),
                )
                self._timeline(project_id, "project.resumed", actor_id, {
                    "budget_version": budget["version"],
                    "reheld_amount": decimal_text(need_hold),
                })
                self._audit("project", project_id, "project.resumed", actor_id,
                            {"budget_version": budget["version"]})
        except InvalidState as exc:
            self._record_lock_failure(project_id, "project.resume_failed", actor_id, str(exc))
            raise
        return {"project_id": project_id, "state": "in_progress", "budget_version": evaluation["budget_version"]}

    # ----- 延期识别 -----

    def sweep_delays(self, actor_id: str) -> dict[str, Any]:
        """把已过计划节点仍未完成相应工程量的项目标记为延期。"""
        self._require(actor_id, "report.read")
        today = self._today()
        delayed: list[str] = []
        with transaction(self.connection, immediate=True):
            self._expire_due_exemptions()
            rows = self.connection.execute(
                "SELECT * FROM projects WHERE state IN ('confirmed','in_progress','delayed')"
            ).fetchall()
            for project in rows:
                expected = self.connection.execute(
                    "SELECT cumulative_weight FROM disbursement_stages WHERE project_id=? AND plan_date<? "
                    "ORDER BY stage_id DESC LIMIT 1",
                    (project["project_id"], today),
                ).fetchone()
                overdue = expected is not None and Decimal(project["completed_percent"]) < Decimal(expected["cumulative_weight"])
                if overdue and project["state"] != "delayed":
                    self.connection.execute(
                        "UPDATE projects SET state='delayed' WHERE project_id=?", (project["project_id"],)
                    )
                    self._timeline(project["project_id"], "project.delayed", actor_id, {
                        "expected_completion_percent": expected["cumulative_weight"],
                        "actual_completion_percent": project["completed_percent"],
                    })
                    delayed.append(project["project_id"])
            if delayed:
                self._audit("project", "*", "project.delayed.batch", actor_id, {"projects": delayed})
        return {"delayed_projects": delayed, "count": len(delayed)}

    # ----- 后台查询：解释为何获批、延期或需要豁免 -----

    def _latest_evaluation(self, project_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM project_evaluations WHERE project_id=? ORDER BY evaluation_id DESC LIMIT 1",
            (project_id,),
        ).fetchone()

    def project_explanation(self, actor_id: str, project_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        project = self.connection.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if project is None:
            raise NotFound("项目不存在")
        evaluation_row = self._latest_evaluation(project_id)
        exemption_row = self.connection.execute(
            "SELECT * FROM exemptions WHERE project_id=? ORDER BY exemption_id DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        stages = [dict(row) for row in self.connection.execute(
            "SELECT * FROM disbursement_stages WHERE project_id=? ORDER BY stage_id", (project_id,)
        ).fetchall()]
        timeline = [dict(row) for row in self.connection.execute(
            "SELECT event_id,event_type,actor_id,payload_json,created_at FROM project_timeline "
            "WHERE project_id=? ORDER BY event_id", (project_id,)
        ).fetchall()]
        for event in timeline:
            event["payload"] = json.loads(event.pop("payload_json"))

        summary_lines: list[str] = []
        evaluation = None
        if evaluation_row is not None:
            gates = json.loads(evaluation_row["gates_json"])
            exempted = json.loads(evaluation_row["exempted_gates_json"])
            evaluation = {
                "evaluation_id": evaluation_row["evaluation_id"],
                "decision": evaluation_row["decision"],
                "priority_score": evaluation_row["priority_score"],
                "budget_id": evaluation_row["budget_id"],
                "budget_version": evaluation_row["budget_version"],
                "trigger": evaluation_row["trigger"],
                "evaluated_by": evaluation_row["evaluated_by"],
                "evaluated_at": evaluation_row["evaluated_at"],
                "gates": gates,
                "exempted_gates": exempted,
                "stages": json.loads(evaluation_row["stages_json"]),
            }
            blocking = [gate for gate in gates if not gate["passed"]]
            waived = [gate for gate in gates if gate["code"] in exempted]
            if project["state"] in {"confirmed", "in_progress", "delayed", "completed"}:
                summary_lines.append(
                    f"项目已获批：评估 {evaluation_row['evaluation_id']} 于 {evaluation_row['evaluated_at']} "
                    f"全部门禁通过，锁定预算版本 {evaluation_row['budget_id']} 第 {evaluation_row['budget_version']} 版。"
                )
            if blocking:
                summary_lines.append("尚未获批/延期待办的阻断原因：")
                summary_lines.extend(f"- {gate['name']}（{gate['code']}）：{gate['reason']}" for gate in blocking)
            for gate in waived:
                summary_lines.append(f"- {gate['name']}经豁免通过：{gate['reason']}")
        if exemption_row is not None:
            active = exemption_row["state"] == "active" and parse_utc(exemption_row["expires_at"]) > self.clock.now()
            summary_lines.append(
                f"紧急加固豁免（{exemption_row['state'] if not active else 'active'}）：授权人 {exemption_row['grantor_id']}，"
                f"理由“{exemption_row['reason']}”，失效时间 {exemption_row['expires_at']}，"
                f"放宽门禁 {json.loads(exemption_row['gates_json'])}。"
            )
        if project["state"] == "delayed":
            summary_lines.append("项目已标记延期：实际完成量低于已到期施工节点的累计权重。")
        reservation = self.connection.execute(
            "SELECT * FROM budget_reservations WHERE project_id=? ORDER BY reservation_id DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        return {
            "project_id": project_id,
            "household_id": project["household_id"],
            "township_id": project["township_id"],
            "state": project["state"],
            "revision": project["revision"],
            "appraisal_grade": project["appraisal_grade"],
            "risk_level": project["risk_level"],
            "priority_score": project["priority_score"],
            "latest_movein_date": project["latest_movein_date"],
            "funding": {
                "central_amount": project["central_amount"],
                "local_amount": project["local_amount"],
                "household_amount": project["household_amount"],
                "subsidy_amount": project["subsidy_amount"],
                "completed_percent": project["completed_percent"],
                "paid_amount": project["paid_amount"],
                "budget_version": project["budget_version"],
            },
            "latest_evaluation": evaluation,
            "latest_exemption": None if exemption_row is None else {
                "exemption_id": exemption_row["exemption_id"],
                "state": exemption_row["state"],
                "grantor_id": exemption_row["grantor_id"],
                "reason": exemption_row["reason"],
                "expires_at": exemption_row["expires_at"],
                "gates": json.loads(exemption_row["gates_json"]),
            },
            "reservation": None if reservation is None else dict(reservation),
            "disbursement_stages": stages,
            "timeline": timeline,
            "explanation": summary_lines,
        }

    def pending_confirmations(self, actor_id: str) -> dict[str, Any]:
        """待确认计划恢复清单：服务重启后据此找回所有 submitted 项目及最近评估结论。"""
        self._require(actor_id, "report.read")
        rows = self.connection.execute(
            "SELECT p.*,e.decision AS last_decision,e.evaluated_at AS last_evaluated_at,"
            "e.budget_version AS last_budget_version FROM projects p "
            "LEFT JOIN (SELECT project_id,MAX(evaluation_id) AS max_id FROM project_evaluations GROUP BY project_id) m "
            "ON m.project_id=p.project_id "
            "LEFT JOIN project_evaluations e ON e.evaluation_id=m.max_id "
            "WHERE p.state='submitted' ORDER BY CAST(p.priority_score AS REAL) DESC,p.submitted_at,p.project_id"
        ).fetchall()
        items = []
        for row in rows:
            items.append({
                "project_id": row["project_id"],
                "household_id": row["household_id"],
                "township_id": row["township_id"],
                "risk_level": row["risk_level"],
                "appraisal_grade": row["appraisal_grade"],
                "priority_score": row["priority_score"],
                "submitted_at": row["submitted_at"],
                "last_decision": row["last_decision"],
                "last_budget_version": row["last_budget_version"],
                "last_evaluated_at": row["last_evaluated_at"],
            })
        return {"pending": items, "count": len(items), "recovered_at": self._now()}

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM fund_audit_events ORDER BY event_id").fetchall()
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
            calculated = digest(body)
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
