"""可解释的分期拨付门禁计算。

拨付阶段完全由申报材料（鉴定等级、施工节点、最迟入住日期、资金构成、
适用的紧急加固豁免）和在有效期内的资金额度版本推导，结果可复现，
每个阶段都附带比例来源说明，便于后台回答“项目为何获批/延期/需要豁免”。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping, Sequence

from .models import GRADE_RANK, FundingMix, Milestone


ZERO = Decimal("0")
HUNDRED = Decimal("100")
MONEY = Decimal("0.01")

# 鉴定等级 -> 启动阶段（开工前）预拨比例。
# 风险越高的家庭越早拿到钱，优先投向风险高且能按期完工的家庭。
ADVANCE_RATE = {
    "D": Decimal("0.40"),
    "C": Decimal("0.30"),
    "B": Decimal("0.20"),
    "A": Decimal("0.10"),
}

# 各级别要求自筹承诺先到位后才允许确认。
SELF_RAISE_CAP = {
    "D": Decimal("0.10"),
    "C": Decimal("0.20"),
    "B": Decimal("0.30"),
    "A": Decimal("0.50"),
}

# 紧急加固豁免下的启动比例（可立即应急施工）。
EXEMPTION_ADVANCE_RATE = Decimal("0.60")
# 补贴类资金（中央补助 + 地方配套）在总投资中的最低占比。
MIN_SUBSIDY_SHARE = Decimal("0.30")


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


def stage_weights(
    risk_grade: str,
    milestones: Sequence[Milestone],
    *,
    emergency_exemption: bool = False,
) -> list[Decimal]:
    """返回与“启动阶段 + 每个施工节点验收阶段”对应的拨付比例。

    比例之和恒为 1：启动预拨按鉴定等级（或豁免）确定，
    其余比例在各节点之间均分，尾差并入最后一个节点。
    """
    advance = EXEMPTION_ADVANCE_RATE if emergency_exemption else ADVANCE_RATE[risk_grade]
    remaining = HUNDRED / HUNDRED - advance
    count = len(milestones)
    weights: list[Decimal] = [advance]
    if count == 1:
        weights.append(remaining)
        return weights
    each = (remaining / count).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    weights.extend([each] * count)
    # 校正四舍五入尾差，保证合计恰为 1。
    weights[-1] = HUNDRED / HUNDRED - sum(weights[:-1], ZERO)
    return weights


@dataclass(frozen=True, slots=True)
class DisbursementStage:
    seq: int
    trigger: str
    milestone_code: str | None
    milestone_name: str | None
    planned_date: str | None
    weight: Decimal
    amount: Decimal
    rationale: str

    def as_dict(self) -> dict[str, object]:
        return {
            "seq": self.seq,
            "trigger": self.trigger,
            "milestone_code": self.milestone_code,
            "milestone_name": self.milestone_name,
            "planned_date": self.planned_date,
            "weight": format(self.weight.quantize(Decimal("0.0001")), "f"),
            "amount": format(self.amount, "f"),
            "rationale": self.rationale,
        }


def build_stages(
    *,
    risk_grade: str,
    estimated_cost: Decimal,
    milestones: Sequence[Milestone],
    exemption: Mapping[str, object] | None = None,
) -> list[DisbursementStage]:
    """依据申报材料构造拨付阶段，金额合计等于估算投资，尾差并入尾款。"""
    emergency = bool(exemption)
    weights = stage_weights(risk_grade, milestones, emergency_exemption=emergency)
    advance_rate = weights[0]
    stages: list[DisbursementStage] = []
    advance_amount = quantize_money(estimated_cost * advance_rate)
    if emergency:
        rationale = (
            "紧急加固豁免：经授权先拨 60% 用于立即排险，"
            f"授权人 {exemption['authorized_by']}，豁免至 {exemption['expires_at']} 失效"
        )
    else:
        rationale = (
            f"鉴定等级 {risk_grade} 级（{GRADE_RANK[risk_grade]}/4 风险序）"
            f"开工前预拨 {format(advance_rate.quantize(Decimal('0.0001')), 'f')}："
            "风险等级越高预拨越早，引导资金优先投向高风险家庭"
        )
    stages.append(DisbursementStage(1, "plan_confirmed", None, None, None, advance_rate, advance_amount, rationale))
    allocated = advance_amount
    for index, milestone in enumerate(milestones, start=2):
        weight = weights[index - 1]
        if index == len(milestones) + 1:
            amount = estimated_cost - allocated
        else:
            amount = quantize_money(estimated_cost * weight)
            allocated += amount
        stages.append(
            DisbursementStage(
                seq=index,
                trigger="milestone_verified",
                milestone_code=milestone.code,
                milestone_name=milestone.name,
                planned_date=milestone.planned_date,
                weight=weight,
                amount=amount,
                rationale=f"节点 {milestone.name}（{milestone.planned_date}）现场验收通过后按进度拨付",
            )
        )
    return stages


def evaluate_application(
    *,
    risk_grade: str,
    estimated_cost: Decimal,
    funding_mix: FundingMix,
    milestones: Sequence[Milestone],
    latest_move_in_date: str,
    release_active: bool,
    release_expires_at: str | None,
    budget_available: Decimal,
    exemption: Mapping[str, object] | None = None,
    today: str,
) -> dict[str, object]:
    """给出可解释的门禁结论：approved（可确认）或 deferred（延期及原因）。"""
    reasons: list[str] = []
    blocking: list[str] = []

    if not release_active:
        if release_expires_at is not None and release_expires_at[:10] < today:
            blocking.append("当前资金额度版本已过有效期，需由财政人员发布新版本")
        else:
            blocking.append("没有在有效期内的资金额度版本")
    elif budget_available < estimated_cost:
        blocking.append(
            f"额度余额 {format(quantize_money(budget_available), 'f')} "
            f"小于项目估算投资 {format(quantize_money(estimated_cost), 'f')}，需等待预算调整或排队"
        )

    subsidy = funding_mix.central_subsidy + funding_mix.local_match
    subsidy_share = subsidy / estimated_cost if estimated_cost else ZERO
    if subsidy_share < MIN_SUBSIDY_SHARE:
        reasons.append(
            "中央补助与地方配套合计占比 "
            f"{format((subsidy_share * HUNDRED).quantize(Decimal('0.01')), 'f')}% 低于政策下限 30%，需补充配套"
        )
    self_share = funding_mix.household_self_raise / estimated_cost if estimated_cost else ZERO
    cap = SELF_RAISE_CAP[risk_grade]
    if exemption is None and self_share > cap:
        blocking.append(
            f"{risk_grade} 级危房家庭自筹占比 "
            f"{format((self_share * HUNDRED).quantize(Decimal('0.01')), 'f')}% 超出 "
            f"{format((cap * HUNDRED).quantize(Decimal('0.01')), 'f')}% 上限，"
            "需降低自筹或取得紧急加固豁免"
        )

    last_node = milestones[-1].planned_date
    if last_node > latest_move_in_date:
        blocking.append("最末施工节点晚于最迟入住日期，无法按期完工")
    schedule_days = (date.fromisoformat(last_node) - date.fromisoformat(today)).days
    if schedule_days < 0:
        blocking.append("计划完工日期已过，必须重新申报节点")
    elif schedule_days <= 30:
        reasons.append("距最迟入住日期不足 30 天，列入按期完工重点督办")

    if GRADE_RANK[risk_grade] >= GRADE_RANK["C"]:
        reasons.append(f"鉴定等级 {risk_grade} 级属高风险，按优先级优先安排资金")

    if exemption is not None:
        if today > str(exemption["expires_at"])[:10]:
            blocking.append(f"紧急加固豁免已于 {exemption['expires_at']} 失效")
        else:
            reasons.append(
                f"持有效紧急加固豁免（授权人 {exemption['authorized_by']}，"
                f"理由：{exemption['reason']}，{exemption['expires_at']} 前有效），可先行排险施工"
            )

    decision = "approved" if not blocking else "deferred"
    return {
        "decision": decision,
        "reasons": reasons,
        "blocking_reasons": blocking,
        "subsidy_share": format((subsidy_share * HUNDRED).quantize(Decimal("0.01")), "f"),
        "self_raise_share": format((self_share * HUNDRED).quantize(Decimal('0.01')), "f"),
    }
