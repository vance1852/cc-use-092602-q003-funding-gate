"""危房改造分期资金门禁的离线验收。

覆盖完整业务链路：财政发布带版本/有效期的额度 → 乡镇申报鉴定等级、
施工节点、最迟入住日期与资金构成 → 系统给出可解释门禁与拨付阶段 →
确认时一次性锁定预算和施工资源 → 验收按实际完成量结算 → 暂停/取消
退款 → 紧急加固豁免记录授权人、理由与失效时间 → 重启恢复待确认计划。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import RenovationFundingService


def plan_payload(plan_id: str, *, risk_grade: str = "D", cost: str = "100000",
                 key: str, latest_move_in: str = "2026-12-20",
                 milestones=None, household_self_raise: str = "5000") -> dict[str, object]:
    if milestones is None:
        milestones = [
            {"code": "foundation", "name": "基础加固", "planned_date": "2026-10-15"},
            {"code": "main-structure", "name": "主体结构", "planned_date": "2026-11-20"},
            {"code": "roof-finish", "name": "屋面竣工", "planned_date": "2026-12-15"},
        ]
    central = str(round((float(cost) - float(household_self_raise)) * 0.7, 2))
    local = str(round((float(cost) - float(household_self_raise)) * 0.3, 2))
    return {
        "plan_id": plan_id,
        "household_id": f"house-{plan_id}",
        "township_id": "township-north",
        "risk_grade": risk_grade,
        "estimated_cost": cost,
        "latest_move_in_date": latest_move_in,
        "milestones": milestones,
        "funding_mix": {
            "central_subsidy": central,
            "local_match": local,
            "household_self_raise": household_self_raise,
        },
        "household_commitment": "家庭承诺开工前自筹到位 5000 元",
        "construction_team_id": "team-1",
        "idempotency_key": key,
    }


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    start = datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)
    clock = FrozenClock(start)
    service = RenovationFundingService(connection, clock)
    for user_id, role in (
        ("fin", "finance"),
        ("town", "township"),
        ("rev", "reviewer"),
        ("audit", "auditor"),
    ):
        service.create_user(user_id, user_id, role)

    # 财政发布年度额度（版本 1，有效期至年底）。
    release = service.publish_release("fin", {
        "release_id": "rel-2026-v1", "fiscal_year": 2026, "total_amount": "300000",
        "effective_from": "2026-01-01T00:00:00Z", "expires_at": "2026-12-31T23:59:59Z",
        "note": "2026 年度危房改造首批资金",
    })
    service.register_resource("town", {
        "resource_id": "res-team-1", "team_id": "team-1", "name": "乡建工班一组",
        "capacity_units": 2,
    })

    # 高风险 D 级家庭申报，门禁应通过。
    approved = service.submit_plan("town", plan_payload("plan-d", key="key-d"))
    # B 级家庭自筹过高，门禁应延期。
    deferred = service.submit_plan("town", plan_payload(
        "plan-b", risk_grade="B", cost="60000", household_self_raise="25000", key="key-b"))
    assert approved["gate"]["decision"] == "approved", approved["gate"]
    assert deferred["gate"]["decision"] == "deferred", deferred["gate"]

    # 迟到的预算调整（缩减额度，发布时即生效）只影响尚未确认的计划。
    service.publish_release("fin", {
        "release_id": "rel-2026-v2", "fiscal_year": 2026, "total_amount": "150000",
        "effective_from": "2026-09-01T00:00:00Z", "expires_at": "2026-12-31T23:59:59Z",
        "note": "调整后年度额度",
    })

    # 确认 D 级计划：一次性锁定预算与施工资源并预拨启动资金。
    confirmed = service.confirm_plan("town", "plan-d", approved["revision"])
    assert confirmed["state"] == "confirmed"
    release_after_confirm = service.release("rel-2026-v2")

    # 两个节点分别验收，第二个只完成 80%，按实际完成量结算。
    service.record_verification("rev", "plan-d", "foundation", "100", "基础验收合格")
    partial = service.record_verification("rev", "plan-d", "main-structure", "80", "主体完成八成")

    # 暂停后取消：尾款阶段跳过，冻结余额退回，资源释放。
    service.pause_plan("town", "plan-d", "进入冬季暂停施工")
    cancelled = service.cancel_plan("town", "plan-d", "家庭迁去集中安置，终止改造")
    assert cancelled["state"] == "cancelled"
    release_after_cancel = service.release("rel-2026-v2")

    # 紧急加固豁免：记录授权人、理由、失效时间，使自筹过高的 B 级计划可确认。
    exemption = service.grant_exemption(
        "rev", "ex-b", "plan-b", "墙体裂缝持续扩大需立即支顶排险",
        "2026-10-31T23:59:59Z",
    )
    plan_b = service.plan_detail("town", "plan-b")
    assert plan_b["gate"]["decision"] == "approved", plan_b["gate"]
    confirmed_b = service.confirm_plan("town", "plan-b", plan_b["revision"])

    # 再留一个待确认计划：其最迟入住日期在重启时点之后不久，用于验证重启恢复刷新。
    service.submit_plan("town", plan_payload(
        "plan-e", risk_grade="C", cost="20000", household_self_raise="1000",
        key="key-e", latest_move_in="2026-10-20",
        milestones=[
            {"code": "foundation", "name": "基础加固", "planned_date": "2026-10-05"},
            {"code": "roof-finish", "name": "屋面竣工", "planned_date": "2026-10-18"},
        ]))

    # 豁免失效：时钟越过失效时间，重新构造服务（模拟重启）恢复待确认计划。
    clock.advance(days=40)
    restarted = RenovationFundingService(connection, clock)
    assert restarted.recovery_summary["expired_plans"] >= 1, restarted.recovery_summary
    expired_exemption = service.exemption("ex-b")

    audit = service.audit_chain("audit")
    result = {
        "status": "ok",
        "release_v1": release["version"],
        "release_v2_available_after_confirm": release_after_confirm["available_amount"],
        "release_v2_available_after_cancel": release_after_cancel["available_amount"],
        "approved_gate_reasons": approved["gate"]["reasons"],
        "deferred_blocking_reasons": deferred["gate"]["blocking_reasons"],
        "stages_for_d": [
            {"seq": stage["seq"], "trigger": stage["trigger"], "planned_amount": stage["planned_amount"]}
            for stage in confirmed["stages"]
        ],
        "partial_verification_disbursed": partial["disbursed_total"],
        "cancelled_settled_amount": cancelled["settled_amount"],
        "exemption": {
            "exemption_id": exemption["exemption_id"],
            "authorized_by": exemption["authorized_by"],
            "reason": exemption["reason"],
            "expires_at": exemption["expires_at"],
            "valid_after_restart": expired_exemption["valid"],
        },
        "plan_b_confirmed_at": confirmed_b["confirmed_at"],
        "recovery": restarted.recovery_summary,
        "audit": audit,
        "workspace": workspace.name,
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行危房改造分期资金门禁离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
