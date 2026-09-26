from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from dilapidated_funding.api import JsonApplication
from dilapidated_funding.clock import FrozenClock
from dilapidated_funding.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from dilapidated_funding.policy import build_stages, evaluate_gates, settle_amounts
from dilapidated_funding.service import FundGateService
from dilapidated_funding.storage import connect

from dilapidated_funding.models import ProjectApplication


def make_application(**overrides) -> ProjectApplication:
    raw = {
        "project_id": "P1",
        "household_id": "H1",
        "township_id": "T1",
        "budget_id": "B1",
        "appraisal_grade": "D",
        "risk_level": "high",
        "latest_movein_date": "2027-03-31",
        "central_amount": "70000",
        "local_amount": "10000",
        "household_amount": "20000",
        "milestones": [
            {"code": "m1", "name": "开工", "plan_date": "2026-10-10", "weight_percent": "30"},
            {"code": "m2", "name": "封顶", "plan_date": "2026-12-20", "weight_percent": "40"},
            {"code": "m3", "name": "竣工", "plan_date": "2027-03-20", "weight_percent": "30"},
        ],
        "idempotency_key": "k1",
    }
    raw.update(overrides)
    return ProjectApplication.from_dict(raw)


def project_payload(project_id: str = "P1", **overrides) -> dict:
    payload = {
        "project_id": project_id,
        "household_id": f"H-{project_id}",
        "township_id": "T1",
        "budget_id": "B1",
        "appraisal_grade": "D",
        "risk_level": "high",
        "latest_movein_date": "2027-03-31",
        "central_amount": "70000",
        "local_amount": "10000",
        "household_amount": "20000",
        "milestones": [
            {"code": "m1", "name": "开工", "plan_date": "2026-10-10", "weight_percent": "30"},
            {"code": "m2", "name": "封顶", "plan_date": "2026-12-20", "weight_percent": "40"},
            {"code": "m3", "name": "竣工", "plan_date": "2027-03-20", "weight_percent": "30"},
        ],
        "idempotency_key": f"key-{project_id}",
    }
    payload.update(overrides)
    return payload


class PolicyTests(unittest.TestCase):
    def test_stages_split_subsidy_without_rounding_gap(self) -> None:
        application = make_application()
        stages = build_stages(application, Decimal("80000"))
        self.assertEqual([row["amount_cny"] for row in stages], ["24000.00", "32000.00", "24000.00"])
        self.assertEqual(stages[-1]["stage_kind"], "acceptance_final")
        total = sum((Decimal(row["amount_cny"]) for row in stages), Decimal("0"))
        self.assertEqual(total, Decimal("80000.00"))

    def test_stages_rounding_tail_goes_to_final_stage(self) -> None:
        application = make_application()
        stages = build_stages(application, Decimal("100.00"))
        self.assertEqual([row["amount_cny"] for row in stages], ["30.00", "40.00", "30.00"])
        stages = build_stages(application, Decimal("100.01"))
        self.assertEqual(sum((Decimal(s["amount_cny"]) for s in stages), Decimal("0")), Decimal("100.01"))
        self.assertEqual(stages[-1]["amount_cny"], "30.01")

    def test_settle_by_actual_completion_pro_rates_current_stage(self) -> None:
        application = make_application()
        stages = build_stages(application, Decimal("80000"))
        result = settle_amounts(stages, Decimal("50"))
        # 30% 节点全额，40% 节点完成一半 (32000*0.5=16000)，尾款节点未开工
        self.assertEqual(result["payable_cny"], "40000.00")
        self.assertEqual([row["state"] for row in result["stages"]], ["settled", "partial", "cancelled"])

    def test_settle_zero_and_full(self) -> None:
        application = make_application()
        stages = build_stages(application, Decimal("80000"))
        self.assertEqual(settle_amounts(stages, Decimal("0"))["payable_cny"], "0.00")
        full = settle_amounts(stages, Decimal("100"))
        self.assertEqual(full["payable_cny"], "80000.00")
        self.assertTrue(all(row["state"] == "settled" for row in full["stages"]))

    def test_gates_detect_non_dilapidated_and_unwaivable_capacity(self) -> None:
        application = make_application(appraisal_grade="B")
        gates = evaluate_gates(
            application, as_of_date="2026-09-26", budget_remaining=Decimal("100000"),
            budget_valid=True, budget_valid_to="2027-12-31T23:59:59Z", resource_available=True,
        )
        by_code = {gate.code: gate for gate in gates}
        self.assertFalse(by_code["appraisal_gate"].passed)
        self.assertFalse(by_code["appraisal_gate"].exemptible)
        self.assertTrue(all(gate.reason for gate in gates))

    def test_exemption_only_covers_exemptible_gates(self) -> None:
        application = make_application(household_amount="0", central_amount="80000", local_amount="20000")
        gates = evaluate_gates(
            application, as_of_date="2026-09-26", budget_remaining=Decimal("10"),
            budget_valid=True, budget_valid_to="2027-12-31T23:59:59Z", resource_available=True,
            active_exemption_gates=["household_commitment_gate"],
        )
        by_code = {gate.code: gate for gate in gates}
        self.assertTrue(by_code["household_commitment_gate"].passed)
        self.assertIn("豁免", by_code["household_commitment_gate"].reason)
        # 资金能力门禁不可豁免，余额不足依然阻断
        self.assertFalse(by_code["budget_capacity_gate"].passed)
        self.assertFalse(by_code["budget_capacity_gate"].exemptible)


class FundGateServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 26, 2, 0, tzinfo=timezone.utc))
        self.service = FundGateService(self.connection, self.clock)
        for user_id, role in (
            ("fin", "finance"),
            ("town", "township"),
            ("house", "housing"),
            ("auth", "authority"),
            ("aud", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.publish_budget("fin", {
            "budget_id": "B1", "version": 1, "total_amount": "100000",
            "valid_from": "2026-09-01T00:00:00Z", "valid_to": "2026-12-31T23:59:59Z",
            "note": "年度危房改造资金",
        })
        self.service.register_resource("town", {"township_id": "T1", "capacity": 2})

    def tearDown(self) -> None:
        self.connection.close()

    def _submit(self, project_id: str = "P1", **overrides) -> dict:
        return self.service.submit_project("town", project_payload(project_id, **overrides))

    def test_budget_publish_requires_version_and_window(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.publish_budget("fin", {
                "budget_id": "B9", "version": 1, "total_amount": "10",
                "valid_from": "2026-12-31T00:00:00Z", "valid_to": "2026-01-01T00:00:00Z",
            })
        with self.assertRaises(Forbidden):
            self.service.publish_budget("town", {
                "budget_id": "B1", "version": 2, "total_amount": "1",
                "valid_from": "2027-01-01T00:00:00Z", "valid_to": "2027-12-31T23:59:59Z",
            })

    def test_late_budget_version_is_rejected_but_confirmed_plan_keeps_old(self) -> None:
        self._submit("P1")
        self.service.confirm_project("house", "P1", 1)
        with self.assertRaises(Conflict):
            self.service.publish_budget("fin", {
                "budget_id": "B1", "version": 1, "total_amount": "999",
                "valid_from": "2026-10-01T00:00:00Z", "valid_to": "2026-12-31T23:59:59Z",
            })
        self.service.publish_budget("fin", {
            "budget_id": "B1", "version": 2, "total_amount": "999",
            "valid_from": "2027-01-01T00:00:00Z", "valid_to": "2027-12-31T23:59:59Z",
        })
        project = self.service.project_explanation("aud", "P1")
        self.assertEqual(project["funding"]["budget_version"], 1)

    def test_overlapping_budget_windows_pick_highest_valid_version(self) -> None:
        self.service.publish_budget("fin", {
            "budget_id": "B1", "version": 2, "total_amount": "999",
            "valid_from": "2026-10-15T00:00:00Z", "valid_to": "2027-12-31T23:59:59Z",
        })
        self._submit("P1")
        self.assertEqual(self.service.evaluate_project("house", "P1")["budget_version"], 1)
        self.clock.advance(days=20)
        self._submit("P2", milestones=[
            {"code": "m1", "name": "开工", "plan_date": "2026-11-20", "weight_percent": "30"},
            {"code": "m2", "name": "封顶", "plan_date": "2027-01-20", "weight_percent": "40"},
            {"code": "m3", "name": "竣工", "plan_date": "2027-03-20", "weight_percent": "30"},
        ])
        self.assertEqual(self.service.evaluate_project("house", "P2")["budget_version"], 2)

    def test_confirm_locks_budget_and_resource_then_settles_by_completion(self) -> None:
        self._submit("P1")
        confirmed = self.service.confirm_project("house", "P1", 1)
        self.assertEqual(confirmed["locked_amount"], "80000.00")
        budget = self.service.budget_version("B1", 1)
        self.assertEqual(budget["held_amount"], "80000.00")
        self.assertEqual(budget["available_amount"], "20000.00")
        locks = self.connection.execute(
            "SELECT COUNT(*) c FROM project_resource_locks WHERE project_id='P1' AND state='held'"
        ).fetchone()["c"]
        self.assertEqual(locks, 1)
        partial = self.service.record_acceptance("house", "P1", "30", note="基础完工")
        self.assertEqual(partial["newly_paid_amount"], "24000.00")
        full = self.service.record_acceptance("house", "P1", "100")
        self.assertEqual(full["paid_amount"], "80000.00")
        self.assertEqual(full["state"], "completed")
        budget = self.service.budget_version("B1", 1)
        self.assertEqual(budget["held_amount"], "0.00")
        self.assertEqual(budget["paid_amount"], "80000.00")

    def test_failed_confirmation_leaves_no_partial_freeze(self) -> None:
        # 自筹和配套都为 0 -> 两条可豁免门禁失败；预算/资源充足。
        self._submit("P1", central_amount="80000", local_amount="0", household_amount="0")
        with self.assertRaises(InvalidState):
            self.service.confirm_project("house", "P1", 1)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) c FROM budget_reservations").fetchone()["c"], 0
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) c FROM project_resource_locks").fetchone()["c"], 0
        )
        budget = self.service.budget_version("B1", 1)
        self.assertEqual(budget["held_amount"], "0.00")
        # 失败原因仍可在后台解释中看到
        explanation = self.service.project_explanation("aud", "P1")
        self.assertTrue(any("自筹" in line for line in explanation["explanation"]))

    def test_budget_capacity_failure_at_lock_leaves_no_partial_freeze(self) -> None:
        # P1 锁定 80000 后，预算只剩 20000；P2 需要 80000，评估即阻断。
        self._submit("P1")
        self.service.confirm_project("house", "P1", 1)
        self._submit("P2")
        with self.assertRaises(InvalidState):
            self.service.confirm_project("house", "P2", 1)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) c FROM budget_reservations WHERE project_id='P2'"
            ).fetchone()["c"],
            0,
        )

    def test_exemption_requires_grantor_reason_and_expiry(self) -> None:
        self._submit("P1", central_amount="80000", local_amount="0", household_amount="0")
        with self.assertRaises(Forbidden):
            self.service.grant_exemption("town", {
                "project_id": "P1", "grantor_id": "town", "reason": "困难",
                "expires_at": "2026-12-01T00:00:00Z",
            })
        with self.assertRaises(ValidationFailed):
            # 资金能力门禁不在可豁免清单内
            self.service.grant_exemption("auth", {
                "project_id": "P1", "grantor_id": "auth", "reason": "困难",
                "expires_at": "2026-12-01T00:00:00Z", "gates": ["budget_capacity_gate"],
            })
        with self.assertRaises(Forbidden):
            # 授权人必须等于签发账号
            self.service.grant_exemption("auth", {
                "project_id": "P1", "grantor_id": "someone-else", "reason": "困难",
                "expires_at": "2026-12-01T00:00:00Z",
            })
        self.service.grant_exemption("auth", {
            "project_id": "P1", "grantor_id": "auth", "reason": "户主重度残疾，紧急开工",
            "expires_at": "2026-10-01T00:00:00Z",
            "gates": ["household_commitment_gate", "local_match_gate"],
        })
        self.assertEqual(self.service.evaluate_project("house", "P1")["decision"], "eligible")

    def test_expired_exemption_stops_waiving(self) -> None:
        self._submit("P1", central_amount="80000", local_amount="0", household_amount="0")
        self.service.grant_exemption("auth", {
            "project_id": "P1", "grantor_id": "auth", "reason": "紧急加固",
            "expires_at": "2026-10-01T00:00:00Z",
            "gates": ["household_commitment_gate", "local_match_gate"],
        })
        self.service.confirm_project("house", "P1", 1)
        self.clock.advance(days=10)
        # 到期状态在下次业务动作（延期巡查/评估等）时惰性落库
        self.service.sweep_delays("aud")
        self.assertEqual(
            self.connection.execute(
                "SELECT state FROM exemptions WHERE project_id='P1'"
            ).fetchone()["state"],
            "expired",
        )

    def test_suspend_settles_completed_work_and_releases_rest(self) -> None:
        self._submit("P1")
        self.service.confirm_project("house", "P1", 1)
        result = self.service.suspend_project("house", "P1", "45", note="雨季")
        self.assertEqual(result["paid_amount"], "36000.00")
        self.assertEqual(result["released_amount"], "44000.00")
        budget = self.service.budget_version("B1", 1)
        self.assertEqual(budget["held_amount"], "0.00")
        self.assertEqual(budget["paid_amount"], "36000.00")
        # 资源已释放；剩余预算 64000 可容纳补助 60000 的新项目
        self._submit("P2", central_amount="50000", local_amount="10000", household_amount="10000")
        self.assertEqual(self.service.evaluate_project("house", "P2")["decision"], "eligible")

    def test_cancel_unconfirmed_project_has_no_settlement(self) -> None:
        self._submit("P1")
        result = self.service.cancel_project("auth", "P1", "0", note="家庭放弃")
        self.assertEqual(result["paid_amount"], "0.00")
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) c FROM budget_reservations").fetchone()["c"], 0
        )

    def test_resume_re_evaluates_against_latest_budget(self) -> None:
        self._submit("P1")
        self.service.confirm_project("house", "P1", 1)
        self.service.suspend_project("house", "P1", "30")
        self.clock.advance(days=100)
        # 旧预算窗口已过且无新版本 -> 恢复失败且不产生冻结
        with self.assertRaises(InvalidState):
            self.service.resume_project("house", "P1")
        held = self.connection.execute(
            "SELECT COUNT(*) c FROM budget_reservations WHERE state='held'"
        ).fetchone()["c"]
        self.assertEqual(held, 0)
        self.service.publish_budget("fin", {
            "budget_id": "B1", "version": 2, "total_amount": "200000",
            "valid_from": "2027-01-01T00:00:00Z", "valid_to": "2027-12-31T23:59:59Z",
        })
        resumed = self.service.resume_project("house", "P1")
        self.assertEqual(resumed["budget_version"], 2)
        self.service.record_acceptance("house", "P1", "100")
        v1 = self.service.budget_version("B1", 1)
        v2 = self.service.budget_version("B1", 2)
        self.assertEqual((v1["held_amount"], v1["paid_amount"]), ("0.00", "24000.00"))
        self.assertEqual((v2["held_amount"], v2["paid_amount"]), ("0.00", "56000.00"))

    def test_delay_sweep_marks_overdue_projects(self) -> None:
        self._submit("P1")
        self.service.confirm_project("house", "P1", 1)
        self.assertEqual(self.service.sweep_delays("aud")["delayed_projects"], [])
        self.clock.advance(days=20)
        delayed = self.service.sweep_delays("aud")["delayed_projects"]
        self.assertEqual(delayed, ["P1"])
        explanation = self.service.project_explanation("aud", "P1")
        self.assertIn("延期", "".join(explanation["explanation"]))

    def test_pending_confirmations_survive_restart(self) -> None:
        self._submit("P1")
        self._submit("P2")
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "fund.sqlite3"
            first = connect(database)
            service = FundGateService(first, self.clock)
            for user_id, role in (
                ("fin", "finance"), ("town", "township"), ("house", "housing"), ("aud", "auditor"),
            ):
                service.create_user(user_id, user_id, role)
            service.publish_budget("fin", {
                "budget_id": "B2", "version": 1, "total_amount": "200000",
                "valid_from": "2026-09-01T00:00:00Z", "valid_to": "2026-12-31T23:59:59Z",
            })
            service.register_resource("town", {"township_id": "T9", "capacity": 1})
            service.submit_project("town", project_payload(
                "RX1", township_id="T9", budget_id="B2", idempotency_key="rx-key"))
            first.close()
            second = connect(database)
            restarted = FundGateService(second, self.clock)
            pending = restarted.pending_confirmations("aud")
            self.assertEqual([item["project_id"] for item in pending["pending"]], ["RX1"])
            self.assertEqual(pending["pending"][0]["last_decision"], "eligible")
            # 重启后仍可完成确认与结算
            restarted.confirm_project("house", "RX1", 1)
            self.assertEqual(restarted.record_acceptance("house", "RX1", "100")["state"], "completed")
            second.close()

    def test_audit_chain_detects_tampering(self) -> None:
        self._submit("P1")
        self.assertTrue(self.service.audit_chain("aud")["valid"])
        self.connection.execute("UPDATE fund_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("aud")["valid"])

    def test_submit_is_idempotent(self) -> None:
        payload = project_payload("P1")
        first = self.service.submit_project("town", payload)
        second = self.service.submit_project("town", payload)
        self.assertEqual(first, second)
        changed = dict(payload, central_amount="70001")
        with self.assertRaises(Conflict):
            self.service.submit_project("town", changed)

    def test_priority_favours_high_risk_dilapidated(self) -> None:
        high = self._submit("P1")["evaluation"]["priority_score"]
        low = self._submit(
            "P2", appraisal_grade="C", risk_level="low", idempotency_key="key-P2",
            household_id="H-P2",
        )["evaluation"]["priority_score"]
        self.assertGreater(Decimal(high), Decimal(low))
        pending = self.service.pending_confirmations("aud")["pending"]
        self.assertEqual([item["project_id"] for item in pending], ["P1", "P2"])


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 26, 2, 0, tzinfo=timezone.utc))
        self.service = FundGateService(self.connection, self.clock)
        self.app = JsonApplication(self.service)
        for user_id, role in (
            ("fin", "finance"), ("town", "township"), ("house", "housing"),
            ("auth", "authority"), ("aud", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)

    def tearDown(self) -> None:
        self.connection.close()

    def test_health_is_open(self) -> None:
        self.assertEqual(self.app.handle("GET", "/health").status, 200)

    def test_end_to_end_dispatch(self) -> None:
        headers = {"X-Actor-Id": "fin"}
        response = self.app.handle("POST", "/budgets", headers, body=__import__("json").dumps({
            "budget_id": "B1", "version": 1, "total_amount": "100000",
            "valid_from": "2026-09-01T00:00:00Z", "valid_to": "2026-12-31T23:59:59Z",
        }).encode())
        self.assertEqual(response.status, 201, response.body)
        response = self.app.handle("POST", "/resources", {"X-Actor-Id": "town"},
                                   body=__import__("json").dumps({"township_id": "T1", "capacity": 1}).encode())
        self.assertEqual(response.status, 201)
        response = self.app.handle("POST", "/projects", {"X-Actor-Id": "town"},
                                   body=__import__("json").dumps(project_payload()).encode())
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["evaluation"]["decision"], "eligible")
        response = self.app.handle("POST", "/projects/P1/confirm", {"X-Actor-Id": "house"},
                                   body=b'{"expected_revision": 1}')
        self.assertEqual(response.status, 200, response.body)
        response = self.app.handle("GET", "/projects/P1/explanation", {"X-Actor-Id": "aud"})
        self.assertEqual(response.status, 200)
        self.assertTrue(response.body["explanation"])
        response = self.app.handle("GET", "/projects/pending", {"X-Actor-Id": "aud"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["count"], 0)

    def test_forbidden_maps_to_403(self) -> None:
        response = self.app.handle("POST", "/budgets", {"X-Actor-Id": "town"}, body=b'{}')
        self.assertEqual(response.status, 403)
        self.assertEqual(response.body["error"]["code"], "forbidden")


if __name__ == "__main__":
    unittest.main()
