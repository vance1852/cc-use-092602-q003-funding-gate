"""分期资金门禁的确定性策略：门禁判定、优先级、拨付阶段与按实结算。

所有函数都是纯函数，便于单独测试和复算；解释文本随每条结论返回，
后台可据此说明项目为何获批、延期或需要豁免。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Mapping, Sequence

from .models import (
    GRADE_SCORES,
    MIN_HOUSEHOLD_SHARE,
    MIN_LOCAL_MATCH_RATE,
    RISK_BONUS,
    ProjectApplication,
)
from .numeric import HUNDRED, ZERO, decimal_text, money, percent


@dataclass(frozen=True, slots=True)
class GateResult:
    code: str
    name: str
    passed: bool
    exemptible: bool
    actual: str
    expected: str
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "name": self.name,
            "passed": self.passed,
            "exemptible": self.exemptible,
            "actual": self.actual,
            "expected": self.expected,
            "reason": self.reason,
        }


def priority_score(application: ProjectApplication) -> Decimal:
    """风险高、鉴定等级差的家庭得分更高（决定资金优先投向）。"""
    score = GRADE_SCORES[application.appraisal_grade] + RISK_BONUS[application.risk_level]
    if application.appraisal_grade == "D" and application.risk_level == "high":
        score += Decimal("5")
    return score


def _household_share(application: ProjectApplication) -> Decimal:
    if application.total_cost == ZERO:
        return ZERO
    return percent(application.household_amount / application.total_cost * HUNDRED)


def _local_match_rate(application: ProjectApplication) -> Decimal:
    if application.central_amount == ZERO:
        return percent(HUNDRED)
    return percent(application.local_amount / application.central_amount * HUNDRED)


def evaluate_gates(
    application: ProjectApplication,
    *,
    as_of_date: str,
    budget_remaining: Decimal,
    budget_valid: bool,
    budget_valid_to: str,
    resource_available: bool,
    active_exemption_gates: Sequence[str] = (),
    already_started: bool = False,
) -> list[GateResult]:
    """逐条评估门禁；exemptible 门禁在命中生效豁免时按“豁免通过”记录。"""
    exemptions = set(active_exemption_gates)
    gates: list[GateResult] = []

    # 鉴定门槛：只有 C、D 级危房可纳入补助，A、B 不属危房改造范围，且不可豁免。
    grade_ok = application.appraisal_grade in {"C", "D"}
    gates.append(GateResult(
        code="appraisal_gate",
        name="危房鉴定等级",
        passed=grade_ok,
        exemptible=False,
        actual=f"{application.appraisal_grade} 级",
        expected="C 或 D 级",
        reason=("鉴定为危房，符合补助范围" if grade_ok else "鉴定等级未达到 C/D，不属于危房改造补助范围"),
    ))

    # 家庭自筹承诺：自筹不低于总投资 5%，可豁免（极端困难家庭）。
    share = _household_share(application)
    share_ok = share >= MIN_HOUSEHOLD_SHARE
    exempted = "household_commitment_gate" in exemptions
    gates.append(GateResult(
        code="household_commitment_gate",
        name="家庭自筹承诺",
        passed=share_ok or exempted,
        exemptible=True,
        actual=f"自筹占比 {decimal_text(share)}%",
        expected=f"不低于 {decimal_text(MIN_HOUSEHOLD_SHARE)}%",
        reason=(
            "家庭已承诺按比例自筹"
            if share_ok
            else ("自筹比例不足，已由紧急加固豁免覆盖" if exempted else "自筹承诺比例不足，项目暂不可获批")
        ),
    ))

    # 地方配套：不低于中央补助的 10%，可豁免（财政紧张乡镇限时豁免）。
    match = _local_match_rate(application)
    match_ok = match >= MIN_LOCAL_MATCH_RATE
    exempted = "local_match_gate" in exemptions
    gates.append(GateResult(
        code="local_match_gate",
        name="地方配套资金",
        passed=match_ok or exempted,
        exemptible=True,
        actual=f"配套率 {decimal_text(match)}%",
        expected=f"不低于 {decimal_text(MIN_LOCAL_MATCH_RATE)}%",
        reason=(
            "地方配套已落实"
            if match_ok
            else ("地方配套不足，已由紧急加固豁免覆盖" if exempted else "地方配套比例不足，项目暂不可获批")
        ),
    ))

    # 计划节点：新申报项目须在评估日起 30 天内开工；暂停恢复的项目已开工，
    # 只校验剩余工程能否在最迟入住日期前完工。均可豁免。
    first_date = application.milestones[0].plan_date
    last_date = application.milestones[-1].plan_date
    today = date.fromisoformat(as_of_date)
    finish_ok = last_date <= application.latest_movein_date and last_date >= as_of_date
    if already_started:
        soon_start_ok = True
        start_actual = "项目已开工，不再校验开工窗口"
    else:
        window_end = (today + timedelta(days=30)).isoformat()
        soon_start_ok = as_of_date <= first_date <= window_end
        start_actual = f"开工 {first_date}"
    schedule_ok = soon_start_ok and finish_ok
    exempted = "schedule_gate" in exemptions
    if not already_started and not soon_start_ok:
        schedule_reason = f"首个节点 {first_date} 未落在评估日起 30 天开工窗口（{as_of_date} 至 {window_end}）"
    elif not finish_ok:
        schedule_reason = f"完工节点 {last_date} 无法在最迟入住日期 {application.latest_movein_date} 前完成"
    else:
        schedule_reason = f"可在最迟入住日期 {application.latest_movein_date} 前完工"
    expected = (
        f"不晚于最迟入住日期 {application.latest_movein_date} 完工"
        if already_started
        else f"{as_of_date} 至 {today + timedelta(days=30)} 之间开工且不晚于最迟入住日期完工"
    )
    gates.append(GateResult(
        code="schedule_gate",
        name="施工节点与按期完工",
        passed=schedule_ok or exempted,
        exemptible=True,
        actual=f"{start_actual}，完工 {last_date}，最迟入住 {application.latest_movein_date}",
        expected=expected,
        reason=schedule_reason if schedule_ok else (
            f"{schedule_reason}，已由紧急加固豁免覆盖" if exempted else schedule_reason
        ),
    ))

    # 预算有效期：申报时选定的预算版本必须在有效期内，迟到调整不影响已确认计划。
    exempted = "budget_window_gate" in exemptions
    gates.append(GateResult(
        code="budget_window_gate",
        name="预算版本有效期",
        passed=budget_valid or exempted,
        exemptible=True,
        actual="预算版本在有效期内" if budget_valid else f"预算版本在 {as_of_date} 已失效",
        expected=f"评估日 {as_of_date} 处于发布版本有效期内（至 {budget_valid_to}）",
        reason=(
            "使用在有效期内的预算版本进行评估"
            if budget_valid
            else ("预算版本已过有效期，已由紧急加固豁免覆盖" if exempted else "预算版本已过有效期，需等待财政发布新版本")
        ),
    ))

    # 资金能力：剩余可锁定额度必须覆盖补助金额，不可豁免（无资金不得承诺）。
    subsidy = application.subsidy_amount
    enough = budget_remaining >= subsidy
    gates.append(GateResult(
        code="budget_capacity_gate",
        name="预算额度能力",
        passed=enough,
        exemptible=False,
        actual=f"剩余可锁定 {decimal_text(money(budget_remaining))} 元",
        expected=f"不少于补助 {decimal_text(subsidy)} 元",
        reason=(
            "预算余额可覆盖补助金额"
            if enough
            else f"预算缺口 {decimal_text(money(subsidy - budget_remaining))} 元，无法锁定"
        ),
    ))

    # 施工资源：乡镇必须还有可同时开工的施工资源，不可豁免。
    gates.append(GateResult(
        code="construction_resource_gate",
        name="乡镇施工资源",
        passed=resource_available,
        exemptible=False,
        actual="有空闲施工资源" if resource_available else "乡镇施工资源已满",
        expected="至少一个空闲施工资源",
        reason=(
            "乡镇可立即安排施工"
            if resource_available
            else "乡镇在建项目已占满施工资源，需等待资源释放"
        ),
    ))

    return gates


def explain_eligibility(gates: Sequence[GateResult]) -> tuple[bool, list[str]]:
    """汇总门禁结论：全部通过才可获批；解释文本逐条说明判定原因。"""
    blocking = [gate.code for gate in gates if not gate.passed]
    reasons = [gate.reason for gate in gates]
    return (not blocking), reasons


def build_stages(application: ProjectApplication, subsidy_amount: Decimal) -> list[dict[str, object]]:
    """按施工节点权重把补助拆成可解释的拨付阶段，最后一个节点为竣工验收尾款。"""
    stages: list[dict[str, object]] = []
    cumulative = ZERO
    allocated_total = ZERO
    rows = list(application.milestones)
    for index, milestone in enumerate(rows):
        cumulative = money(cumulative + subsidy_amount * milestone.weight_percent / HUNDRED)
        is_final = index == len(rows) - 1
        stages.append({
            "milestone_code": milestone.code,
            "name": milestone.name,
            "plan_date": milestone.plan_date,
            "weight_percent": decimal_text(milestone.weight_percent),
            "cumulative_weight_percent": decimal_text(money(sum(
                (item.weight_percent for item in rows[: index + 1]), ZERO
            ))),
            "cumulative_amount_cny": decimal_text(cumulative),
            "stage_kind": "acceptance_final" if is_final else "progress",
        })
    # 修正累计金额的舍入尾差，使各阶段金额合计严格等于补助金额。
    for index in range(len(rows)):
        prev = ZERO if index == 0 else Decimal(str(stages[index - 1]["cumulative_amount_cny"]))
        cur = Decimal(str(stages[index]["cumulative_amount_cny"]))
        increment = money(cur - prev)
        stages[index]["amount_cny"] = decimal_text(increment)
        allocated_total = money(allocated_total + increment)
    tail = money(subsidy_amount - allocated_total)
    if tail != ZERO:
        last = stages[-1]
        last["amount_cny"] = decimal_text(money(Decimal(str(last["amount_cny"])) + tail))
        last["cumulative_amount_cny"] = decimal_text(
            money(Decimal(str(last["cumulative_amount_cny"])) + tail)
        )
    return stages


def settle_amounts(
    stages: Sequence[Mapping[str, object]],
    completion_percent: Decimal,
) -> dict[str, object]:
    """按实际完成量结算：累计完成比例对应的应付补助。

    已完成整节点的阶段全额计入；当前所处的部分完成节点按完成比例折算；
    未开始节点不拨付。
    """
    completion = money(min(HUNDRED, max(ZERO, completion_percent)))
    settled = ZERO
    rows: list[dict[str, object]] = []
    cumulative_weight = ZERO
    for stage in stages:
        weight = Decimal(str(stage["weight_percent"]))
        amount = Decimal(str(stage["amount_cny"]))
        prev_cumulative = cumulative_weight
        cumulative_weight = money(cumulative_weight + weight)
        if completion >= cumulative_weight:
            payable = amount
            state = "settled"
        elif completion <= prev_cumulative:
            payable = ZERO
            state = "cancelled"
        else:
            portion = (completion - prev_cumulative) / weight
            payable = money(amount * portion)
            state = "partial"
        settled = money(settled + payable)
        rows.append({
            "milestone_code": stage["milestone_code"],
            "state": state,
            "payable_cny": decimal_text(payable),
        })
    return {"completion_percent": decimal_text(completion), "payable_cny": decimal_text(settled), "stages": rows}
