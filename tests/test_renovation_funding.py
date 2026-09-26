from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from renovation_funding.api import JsonApplication
from renovation_funding.clock import FrozenClock
from renovation_funding.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from renovation_funding.gating import build_stages, stage_weights
from renovation_funding.models import Milestone, RenovationPlan
from renovation_funding.service import RenovationFundingService


def milestones() -> list[dict[str, str]]:
    return [
        {"code": "foundation", "name": "基础加固", "planned_date": "2026-10-15"},
        {"code": "main-structure", "name": "主体结构", "planned_date": "2026-11-20"},
        {"code": "roof-finish", "name": "屋面竣工", "planned_date": "2026-12-15"},
    ]


def plan_payload(plan_id: str, key: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "plan_id": plan_id,
        "household_id": f"house-{plan_id}",
        "township_id": "township-north",
        "risk_grade": "D",
        "estimated_cost": "100000",
        "latest_move_in_date": "2026-12-20",
        "milestones": milestones(),
        "funding_mix": {
            "central_subsidy": "66500",
            "local_match": "28500",
            "household_self_raise": "5000",
        },
        "household_commitment": "家庭承诺开工前自筹到位",
        "construction_team_id": "team-1",
        "idempotency_key": key,
    }
    payload.update(overrides)
    return payload


class GatingTests(unittest.TestCase):
    def test_stage_weights_sum_to_one_for_every_grade(self) -> None:
        nodes = [Milestone("a", "甲", "2026-10-01"), Milestone("b", "乙", "2026-11-01")]
        for grade in ("A", "B", "C", "D"):
            weights = stage_weights(grade, nodes)
            self.assertEqual(sum(weights, Decimal("0")), Decimal("1"))
            self.assertEqual(len(weights), 3)
        exempt = stage_weights("D", nodes, emergency_exemption=True)
        self.assertEqual(exempt[0], Decimal("0.60"))
        self.assertEqual(sum(exempt, Decimal("0")), Decimal("1"))

    def test_stages_carry_explainable_rationale_and_sum_to_cost(self) -> None:
        nodes = [Milestone("a", "甲", "2026-10-01")]
        stages = build_stages(risk_grade="C", estimated_cost=Decimal("77777"), milestones=nodes)
        self.assertEqual(len(stages), 2)
        self.assertEqual(sum(stage.amount for stage in stages), Decimal("77777"))
        self.assertIn("C 级", stages[0].rationale)
        self.assertEqual(stages[1].trigger, "milestone_verified")
        emergency = build_stages(
            risk_grade="C", estimated_cost=Decimal("1000"), milestones=nodes,
            exemption={"authorized_by": "rev", "reason": "抢险", "expires_at": "2026-10-01T00:00:00Z"},
        )
        self.assertIn("授权人 rev", emergency[0].rationale)


class ModelTests(unittest.TestCase):
    def test_funding_mix_must_match_estimated_cost(self) -> None:
        payload = plan_payload("p1", "k1", funding_mix={
            "central_subsidy": "60000", "local_match": "20000", "household_self_raise": "5000"})
        with self.assertRaises(ValidationFailed):
            RenovationPlan.from_dict(payload)

    def test_milestones_must_be_ordered_and_before_move_in(self) -> None:
        shuffled = milestones()
        shuffled[0], shuffled[1] = shuffled[1], shuffled[0]
        with self.assertRaises(ValidationFailed):
            RenovationPlan.from_dict(plan_payload("p1", "k1", milestones=shuffled))
        late = [{"code": "only", "name": "竣工", "planned_date": "2027-01-01"}]
        with self.assertRaises(ValidationFailed):
            RenovationPlan.from_dict(plan_payload("p1", "k1", milestones=late))

    def test_self_raise_requires_commitment_text(self) -> None:
        with self.assertRaises(ValidationFailed):
            RenovationPlan.from_dict(plan_payload("p1", "k1", household_commitment=""))


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = RenovationFundingService(self.connection, self.clock)
        for user_id, role in (("fin", "finance"), ("town", "township"), ("rev", "reviewer"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)
        self.service.register_resource("town", {
            "resource_id": "res-1", "team_id": "team-1", "name": "乡建工班一组", "capacity_units": 1})

    def tearDown(self) -> None:
        self.connection.close()

    def publish(self, release_id: str = "rel-1", total: str = "300000",
                effective_from: str = "2026-01-01T00:00:00Z",
                expires_at: str = "2026-12-31T23:59:59Z") -> dict[str, object]:
        return self.service.publish_release("fin", {
            "release_id": release_id, "fiscal_year": 2026, "total_amount": total,
            "effective_from": effective_from, "expires_at": expires_at, "note": "年度资金"})

    def test_release_versions_supersede_and_keep_history(self) -> None:
        first = self.publish()
        second = self.publish("rel-2", "200000")
        self.assertEqual(first["version"], 1)
        self.assertEqual(second["version"], 2)
        self.assertEqual(second["supersedes_release_id"], "rel-1")
        self.assertEqual(self.service.release("rel-1")["state"], "superseded")
        self.assertEqual(len(self.service.list_releases(2026)), 2)

    def test_release_requires_valid_window_and_finance_role(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.publish_release("town", {"release_id": "x", "fiscal_year": 2026,
                                                  "total_amount": "1", "effective_from": "2026-01-01T00:00:00Z",
                                                  "expires_at": "2026-12-31T00:00:00Z", "note": ""})
        with self.assertRaises(ValidationFailed):
            self.publish(expires_at="2025-12-31T00:00:00Z")

    def test_gate_defers_when_no_active_release(self) -> None:
        submitted = self.service.submit_plan("town", plan_payload("p1", "k1"))
        self.assertEqual(submitted["gate"]["decision"], "deferred")
        self.assertIn("没有在有效期内的资金额度版本", submitted["gate"]["blocking_reasons"][0])

    def test_gate_defers_when_budget_insufficient(self) -> None:
        self.publish(total="50000")
        submitted = self.service.submit_plan("town", plan_payload("p1", "k1"))
        self.assertEqual(submitted["gate"]["decision"], "deferred")
        self.assertIn("额度余额", submitted["gate"]["blocking_reasons"][0])

    def test_late_budget_adjustment_only_affects_pending_plans(self) -> None:
        self.publish(total="300000")
        pending = self.service.submit_plan("town", plan_payload("p-pending", "k-pending"))
        confirmed_seed = self.service.submit_plan("town", plan_payload("p-confirmed", "k-confirmed"))
        # 先确认一个计划，再发布迟到调整（缩减额度）。
        self.service.confirm_plan("town", "p-confirmed", confirmed_seed["revision"])
        locked_before = self.service.plan_detail("town", "p-confirmed")["locked_budget"]
        self.publish("rel-2", total="120000", effective_from="2026-09-01T00:00:00Z")
        # 已确认计划仍锁定在原版本，金额不变。
        confirmed_after = self.service.plan_detail("town", "p-confirmed")
        self.assertEqual(confirmed_after["locked_budget"], locked_before)
        self.assertEqual(confirmed_after["fund_release"]["release_id"], "rel-1")
        # 待确认计划被重新评估到 rel-2：余额不足时转为延期。
        refreshed = self.service.plan_detail("town", "p-pending")
        self.assertEqual(refreshed["fund_release"]["release_id"], "rel-2")
        self.assertEqual(refreshed["gate"]["decision"], "deferred")
        self.assertEqual(refreshed["state"], "pending")

    def test_confirm_locks_budget_and_resource_atomically(self) -> None:
        self.publish(total="300000")
        submitted = self.service.submit_plan("town", plan_payload("p1", "k1"))
        confirmed = self.service.confirm_plan("town", "p1", submitted["revision"])
        self.assertEqual(confirmed["state"], "confirmed")
        self.assertEqual(confirmed["locked_budget"], "100000")
        self.assertEqual(confirmed["locked_resource_id"], "res-1")
        release = self.service.release("rel-1")
        # 冻结 100000，其中启动阶段 40% 已预拨。
        self.assertEqual(Decimal(release["frozen_amount"]), Decimal("60000.00"))
        self.assertEqual(Decimal(release["disbursed_amount"]), Decimal("40000.00"))
        resource = self.connection.execute(
            "SELECT booked_units FROM construction_resources WHERE resource_id='res-1'").fetchone()
        self.assertEqual(resource["booked_units"], "1")

    def test_confirm_failure_leaves_no_partial_freeze(self) -> None:
        self.publish(total="300000")
        # 占满唯一的施工档期。
        first = self.service.submit_plan("town", plan_payload("p1", "k1"))
        self.service.confirm_plan("town", "p1", first["revision"])
        second = self.service.submit_plan("town", plan_payload("p2", "k2"))
        with self.assertRaises(Conflict):
            self.service.confirm_plan("town", "p2", second["revision"])
        # 失败不得留下部分冻结：预算与资源占用与失败前一致。
        release = self.service.release("rel-1")
        self.assertEqual(Decimal(release["frozen_amount"]), Decimal("60000.00"))
        self.assertEqual(Decimal(release["disbursed_amount"]), Decimal("40000.00"))
        resource = self.connection.execute(
            "SELECT booked_units FROM construction_resources WHERE resource_id='res-1'").fetchone()
        self.assertEqual(resource["booked_units"], "1")
        self.assertEqual(self.service.plan_detail("town", "p2")["state"], "pending")

    def test_confirm_requires_matching_revision(self) -> None:
        self.publish()
        submitted = self.service.submit_plan("town", plan_payload("p1", "k1"))
        with self.assertRaises(Conflict):
            self.service.confirm_plan("town", "p1", submitted["revision"] + 1)

    def test_verification_settles_by_actual_completion(self) -> None:
        self.publish()
        submitted = self.service.submit_plan("town", plan_payload("p1", "k1"))
        self.service.confirm_plan("town", "p1", submitted["revision"])
        partial = self.service.record_verification("rev", "p1", "foundation", "80", "完成八成")
        # 节点计划 20000，按 80% 实结 16000。
        self.assertEqual(partial["disbursed_total"], "56000.00")
        verification = partial["verifications"][0]
        self.assertEqual(verification["completion_percent"], "80")
        with self.assertRaises(Conflict):
            self.service.record_verification("rev", "p1", "foundation", "100", "重复验收")

    def test_pause_and_cancel_settle_and_refund(self) -> None:
        self.publish()
        submitted = self.service.submit_plan("town", plan_payload("p1", "k1"))
        self.service.confirm_plan("town", "p1", submitted["revision"])
        self.service.record_verification("rev", "p1", "foundation", "100", "基础完成")
        paused = self.service.pause_plan("town", "p1", "冬季停工")
        self.assertEqual(paused["state"], "paused")
        cancelled = self.service.cancel_plan("town", "p1", "迁入安置房")
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(cancelled["settled_amount"], cancelled["disbursed_total"])
        # 已结算 40000+20000=60000，冻结全部退回，资源释放。
        release = self.service.release("rel-1")
        self.assertEqual(Decimal(release["frozen_amount"]), Decimal("0"))
        self.assertEqual(Decimal(release["disbursed_amount"]), Decimal("60000.00"))
        resource = self.connection.execute(
            "SELECT booked_units FROM construction_resources WHERE resource_id='res-1'").fetchone()
        self.assertEqual(resource["booked_units"], "0")
        skipped = [s for s in cancelled["stages"] if s["state"] == "skipped"]
        self.assertEqual(len(skipped), 2)

    def test_complete_requires_all_milestones(self) -> None:
        self.publish()
        submitted = self.service.submit_plan("town", plan_payload("p1", "k1"))
        self.service.confirm_plan("town", "p1", submitted["revision"])
        with self.assertRaises(InvalidState):
            self.service.complete_plan("town", "p1")
        for code in ("foundation", "main-structure", "roof-finish"):
            self.service.record_verification("rev", "p1", code, "100", "合格")
        completed = self.service.complete_plan("town", "p1")
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(completed["settled_amount"], "100000.00")
        release = self.service.release("rel-1")
        self.assertEqual(Decimal(release["frozen_amount"]), Decimal("0"))

    def test_exemption_records_authorizer_reason_and_expiry(self) -> None:
        self.publish()
        submitted = self.service.submit_plan("town", plan_payload(
            "p1", "k1", risk_grade="B",
            funding_mix={"central_subsidy": "35000", "local_match": "15000", "household_self_raise": "50000"}))
        self.assertEqual(submitted["gate"]["decision"], "deferred")
        exemption = self.service.grant_exemption(
            "rev", "ex-1", "p1", "墙体开裂需立即支顶", "2026-10-31T23:59:59Z")
        self.assertEqual(exemption["authorized_by"], "rev")
        self.assertEqual(exemption["reason"], "墙体开裂需立即支顶")
        self.assertEqual(exemption["expires_at"], "2026-10-31T23:59:59Z")
        plan = self.service.plan_detail("town", "p1")
        self.assertEqual(plan["gate"]["decision"], "approved")
        self.assertEqual(plan["exemption"]["exemption_id"], "ex-1")
        # 豁免理由出现在后台说明中。
        explanation = self.service.plan_explanation("audit", "p1")
        self.assertIn("豁免", explanation["headline"])
        self.assertIn("rev", explanation["headline"])
        # 豁免失效后不再有效。
        self.clock.advance(days=40)
        restarted = RenovationFundingService(self.connection, self.clock)
        self.assertFalse(restarted.exemption("ex-1")["valid"])
        self.assertGreaterEqual(restarted.recovery_summary["expired_exemptions"], 1)

    def test_exemption_requires_future_expiry_and_reason(self) -> None:
        self.publish()
        self.service.submit_plan("town", plan_payload("p1", "k1"))
        with self.assertRaises(ValidationFailed):
            self.service.grant_exemption("rev", "ex-1", "p1", "", "2026-10-31T00:00:00Z")
        with self.assertRaises(ValidationFailed):
            self.service.grant_exemption("rev", "ex-1", "p1", "抢险", "2026-09-01T00:00:00Z")
        with self.assertRaises(Forbidden):
            self.service.grant_exemption("town", "ex-1", "p1", "抢险", "2026-10-31T00:00:00Z")

    def test_restart_recovers_pending_plans(self) -> None:
        self.publish()
        self.service.submit_plan("town", plan_payload("p1", "k1"))
        self.service.submit_plan("town", plan_payload("p2", "k2"))
        restarted = RenovationFundingService(self.connection, self.clock)
        self.assertEqual(restarted.recovery_summary["recovered_pending"], 2)
        self.assertEqual(restarted.recovery_summary["approved"], 2)
        # 额度过期后重启：待确认计划转为延期。
        self.clock.advance(days=200)
        restarted2 = RenovationFundingService(self.connection, self.clock)
        self.assertEqual(restarted2.recovery_summary["deferred"], 2)
        plan = restarted2.plan_detail("town", "p1")
        self.assertEqual(plan["gate"]["decision"], "deferred")

    def test_submit_is_idempotent(self) -> None:
        self.publish()
        payload = plan_payload("p1", "k1")
        first = self.service.submit_plan("town", payload)
        second = self.service.submit_plan("town", payload)
        self.assertEqual(first, second)
        with self.assertRaises(Conflict):
            self.service.submit_plan("town", plan_payload("p1b", "k1"))

    def test_explanation_answers_why(self) -> None:
        self.publish()
        self.service.submit_plan("town", plan_payload("p1", "k1"))
        explanation = self.service.plan_explanation("audit", "p1")
        self.assertIn("获批", explanation["headline"])
        self.assertTrue(explanation["timeline"])
        deferred = self.service.submit_plan("town", plan_payload(
            "p2", "k2", estimated_cost="999999",
            funding_mix={"central_subsidy": "699999.30", "local_match": "299999.70", "household_self_raise": "0"}))
        self.assertEqual(deferred["gate"]["decision"], "deferred")
        explanation2 = self.service.plan_explanation("audit", "p2")
        self.assertIn("延期", explanation2["headline"])

    def test_audit_chain_detects_tampering(self) -> None:
        self.publish()
        self.service.submit_plan("town", plan_payload("p1", "k1"))
        self.assertTrue(self.service.audit_chain("audit")["valid"])
        self.connection.execute("UPDATE funding_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("audit")["valid"])

    def test_permissions(self) -> None:
        self.publish()
        with self.assertRaises(Forbidden):
            self.service.submit_plan("fin", plan_payload("p1", "k1"))
        with self.assertRaises(Forbidden):
            self.service.audit_chain("town")
        with self.assertRaises(NotFound):
            self.service.plan_detail("town", "missing")


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = RenovationFundingService(self.connection, clock)
        for user_id, role in (("fin", "finance"), ("town", "township"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)
        self.app = JsonApplication(self.service)

    def tearDown(self) -> None:
        self.connection.close()

    def post(self, path: str, actor: str, payload: dict[str, object]):
        import json
        return self.app.handle("POST", path, {"X-Actor-Id": actor}, json.dumps(payload).encode("utf-8"))

    def test_health_and_actor_boundary(self) -> None:
        self.assertEqual(self.app.handle("GET", "/health").status, 200)
        response = self.app.handle("GET", "/plans")
        self.assertEqual(response.status, 422)
        response = self.app.handle("GET", "/plans", {"X-Actor-Id": "ghost"})
        self.assertEqual(response.status, 404)

    def test_full_http_flow(self) -> None:
        response = self.post("/funds/releases", "fin", {
            "release_id": "rel-1", "fiscal_year": 2026, "total_amount": "300000",
            "effective_from": "2026-01-01T00:00:00Z", "expires_at": "2026-12-31T23:59:59Z", "note": "首批"})
        self.assertEqual(response.status, 201)
        response = self.post("/resources", "town", {
            "resource_id": "res-1", "team_id": "team-1", "name": "工班", "capacity_units": 1})
        self.assertEqual(response.status, 201)
        response = self.post("/plans", "town", plan_payload("p1", "k1"))
        self.assertEqual(response.status, 201)
        revision = response.body["revision"]
        response = self.post("/plans/p1/confirm", "town", {"expected_revision": revision})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["state"], "confirmed")
        response = self.post("/plans/p1/verifications", "town", {
            "milestone_code": "foundation", "completion_percent": "100", "note": "合格"})
        # 乡镇无权登记验收。
        self.assertEqual(response.status, 403)
        response = self.app.handle("GET", "/plans/p1/explanation", {"X-Actor-Id": "audit"})
        self.assertEqual(response.status, 200)
        self.assertIn("headline", response.body)
        response = self.app.handle("GET", "/audit/chain", {"X-Actor-Id": "audit"})
        self.assertEqual(response.status, 200)
        self.assertTrue(response.body["valid"])
        response = self.app.handle("GET", "/recovery", {"X-Actor-Id": "audit"})
        self.assertEqual(response.status, 200)


if __name__ == "__main__":
    unittest.main()
