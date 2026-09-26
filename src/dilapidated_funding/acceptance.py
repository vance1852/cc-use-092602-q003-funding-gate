"""危房改造分期资金门禁的离线验收。

使用临时 SQLite 数据库串联：预算版本发布、乡镇申报与风险分级、门禁评估、
紧急加固豁免、确认时原子锁定、验收/暂停/取消按实结算、延期识别与重启后恢复待确认计划。
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import FundGateService
from .storage import connect


def milestones(start: str = "2026-10-10") -> list[dict[str, str]]:
    return [
        {"code": "foundation", "name": "基础开工", "plan_date": start, "weight_percent": "30"},
        {"code": "topping_out", "name": "主体封顶", "plan_date": "2026-12-20", "weight_percent": "40"},
        {"code": "handover", "name": "竣工验收入住", "plan_date": "2027-03-20", "weight_percent": "30"},
    ]


def run(workspace: Path) -> dict[str, object]:
    database = workspace / "funding_acceptance.sqlite3"
    if database.exists():
        database.unlink()
    clock = FrozenClock(datetime(2026, 9, 26, 2, 0, tzinfo=timezone.utc))
    connection = connect(database)
    service = FundGateService(connection, clock)
    for user_id, role in (
        ("finance-li", "finance"),
        ("township-wang", "township"),
        ("housing-zhao", "housing"),
        ("authority-chen", "authority"),
        ("auditor-sun", "auditor"),
    ):
        service.create_user(user_id, user_id, role)

    # 财政发布带版本和有效期的资金额度。
    budget_v1 = service.publish_budget("finance-li", {
        "budget_id": "county-2026",
        "version": 1,
        "total_amount": "200000.00",
        "valid_from": "2026-09-01T00:00:00Z",
        "valid_to": "2026-12-31T23:59:59Z",
        "note": "2026 年度危房改造中央和省级资金第一批",
    })
    service.register_resource("township-wang", {"township_id": "township-north", "capacity": 2})

    # 高风险 D 级家庭：鉴定等级、风险、节点、最迟入住、资金构成一并申报。
    service.submit_project("township-wang", {
        "project_id": "household-001",
        "household_id": "house-001",
        "township_id": "township-north",
        "budget_id": "county-2026",
        "appraisal_grade": "D",
        "risk_level": "high",
        "latest_movein_date": "2027-03-31",
        "central_amount": "70000.00",
        "local_amount": "10000.00",
        "household_amount": "20000.00",
        "milestones": milestones(),
        "idempotency_key": "apply-001",
    })
    confirmed = service.confirm_project("housing-zhao", "household-001", 1)

    # 极端困难家庭：自筹与配套不足，主管领导授权紧急加固豁免（授权人/理由/失效时间齐备）。
    service.submit_project("township-wang", {
        "project_id": "household-002",
        "household_id": "house-002",
        "township_id": "township-north",
        "budget_id": "county-2026",
        "appraisal_grade": "D",
        "risk_level": "high",
        "latest_movein_date": "2027-03-31",
        "central_amount": "80000.00",
        "local_amount": "0.00",
        "household_amount": "0.00",
        "milestones": milestones(),
        "idempotency_key": "apply-002",
    })
    service.grant_exemption("authority-chen", {
        "project_id": "household-002",
        "grantor_id": "authority-chen",
        "reason": "户主重度残疾、住房濒临倒塌，先紧急开工，自筹和乡镇配套限期补齐",
        "expires_at": "2026-11-30T23:59:59Z",
        "gates": ["household_commitment_gate", "local_match_gate"],
    })
    service.confirm_project("housing-zhao", "household-002", 1)

    # 基础节点验收：按完成量拨付 30%。
    first_payment = service.record_acceptance("housing-zhao", "household-001", "30", note="基础与地梁验收合格")

    # 家庭 002 因材料断供暂停：按已完成 10% 结算，剩余冻结与资源释放。
    suspension = service.suspend_project("housing-zhao", "household-002", "10", note="雨季材料断供")

    # 待确认计划：服务重启前留下一个 submitted 项目。
    service.submit_project("township-wang", {
        "project_id": "household-003",
        "household_id": "house-003",
        "township_id": "township-north",
        "budget_id": "county-2026",
        "appraisal_grade": "C",
        "risk_level": "medium",
        "latest_movein_date": "2027-03-31",
        "central_amount": "50000.00",
        "local_amount": "8000.00",
        "household_amount": "12000.00",
        "milestones": [
            {"code": "foundation", "name": "基础开工", "plan_date": "2026-10-20", "weight_percent": "30"},
            {"code": "topping_out", "name": "主体封顶", "plan_date": "2026-12-25", "weight_percent": "40"},
            {"code": "handover", "name": "竣工验收入住", "plan_date": "2027-03-25", "weight_percent": "30"},
        ],
        "idempotency_key": "apply-003",
    })
    connection.close()

    # 模拟服务重启：从同一 SQLite 文件恢复，待确认计划、审计链和解释均可继续查询。
    restarted_connection = connect(database)
    restarted = FundGateService(restarted_connection, clock)
    pending = restarted.pending_confirmations("auditor-sun")
    explanation = restarted.project_explanation("auditor-sun", "household-002")
    audit = restarted.audit_chain("auditor-sun")
    restarted_connection.close()

    return {
        "status": "ok",
        "budget_v1": {key: budget_v1[key] for key in ("budget_id", "version", "total_amount", "available_amount")},
        "confirmed_subsidy": confirmed["locked_amount"],
        "disbursement_stages": [stage["amount_cny"] for stage in confirmed["stages"]],
        "first_payment": first_payment["newly_paid_amount"],
        "suspension_paid": suspension["paid_amount"],
        "suspension_released": suspension["released_amount"],
        "recovered_pending": [item["project_id"] for item in pending["pending"]],
        "explanation_preview": explanation["explanation"][:3],
        "audit_events": audit["events"],
        "audit_valid": audit["valid"],
        "workspace": workspace.name,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行危房改造分期资金门禁离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
