"""危房改造资金门禁的输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .clock import parse_utc
from .errors import ValidationFailed
from .numeric import HUNDRED, ZERO, money, percent


IDENTIFIER = re.compile(r"^[A-Za-z0-9一-鿿][A-Za-z0-9_.::-一-鿿]{0,63}$")

APPRAISAL_GRADES = ("A", "B", "C", "D")
RISK_LEVELS = ("high", "medium", "low")
# 可被紧急加固豁免放宽的门禁；资金能力、施工资源和鉴定门槛不允许豁免。
EXEMPTIBLE_GATES = (
    "household_commitment_gate",
    "local_match_gate",
    "schedule_gate",
    "budget_window_gate",
)
GRADE_SCORES = {"D": Decimal("100"), "C": Decimal("70"), "B": Decimal("30"), "A": ZERO}
RISK_BONUS = {"high": Decimal("20"), "medium": Decimal("8"), "low": ZERO}
# 家庭自筹承诺最低占总投资比例；地方配套不低于中央补助的比例。
MIN_HOUSEHOLD_SHARE = Decimal("5")
MIN_LOCAL_MATCH_RATE = Decimal("10")


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def decimal_value(value: object, field: str, *, minimum: Decimal = ZERO) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    return result


def positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationFailed(f"{field} 必须是正整数")
    return value


def date_text(value: object, field: str) -> str:
    result = required_text(value, field, 10)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValidationFailed(f"{field} 必须是 YYYY-MM-DD 日期") from exc


def timestamp_text(value: object, field: str) -> str:
    result = required_text(value, field, 40)
    try:
        parse_utc(result, field)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc
    return result


@dataclass(frozen=True, slots=True)
class BudgetVersionInput:
    budget_id: str
    version: int
    total_amount: Decimal
    valid_from: str
    valid_to: str
    note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "BudgetVersionInput":
        valid_from = timestamp_text(raw.get("valid_from"), "valid_from")
        valid_to = timestamp_text(raw.get("valid_to"), "valid_to")
        if parse_utc(valid_from) >= parse_utc(valid_to):
            raise ValidationFailed("valid_to 必须晚于 valid_from")
        return cls(
            budget_id=identifier(raw.get("budget_id"), "budget_id"),
            version=positive_integer(raw.get("version"), "version"),
            total_amount=money(decimal_value(raw.get("total_amount"), "total_amount")),
            valid_from=valid_from,
            valid_to=valid_to,
            note=required_text(raw.get("note", ""), "note", 512) if str(raw.get("note", "")).strip() else "",
        )


@dataclass(frozen=True, slots=True)
class MilestoneInput:
    code: str
    name: str
    plan_date: str
    weight_percent: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], index: int) -> "MilestoneInput":
        field = f"milestones[{index}]"
        return cls(
            code=identifier(raw.get("code"), f"{field}.code"),
            name=required_text(raw.get("name"), f"{field}.name", 64),
            plan_date=date_text(raw.get("plan_date"), f"{field}.plan_date"),
            weight_percent=percent(
                decimal_value(raw.get("weight_percent"), f"{field}.weight_percent", minimum=Decimal("0.01"))
            ),
        )


@dataclass(frozen=True, slots=True)
class ProjectApplication:
    project_id: str
    household_id: str
    township_id: str
    budget_id: str
    appraisal_grade: str
    risk_level: str
    latest_movein_date: str
    central_amount: Decimal
    local_amount: Decimal
    household_amount: Decimal
    milestones: tuple[MilestoneInput, ...]
    idempotency_key: str

    @property
    def subsidy_amount(self) -> Decimal:
        return money(self.central_amount + self.local_amount)

    @property
    def total_cost(self) -> Decimal:
        return money(self.central_amount + self.local_amount + self.household_amount)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ProjectApplication":
        grade = required_text(raw.get("appraisal_grade"), "appraisal_grade", 2).upper()
        if grade not in APPRAISAL_GRADES:
            raise ValidationFailed("appraisal_grade 必须是 A、B、C、D")
        risk = required_text(raw.get("risk_level"), "risk_level", 8).lower()
        if risk not in RISK_LEVELS:
            raise ValidationFailed("risk_level 必须是 high、medium、low")
        rows = raw.get("milestones")
        if not isinstance(rows, list) or len(rows) < 2:
            raise ValidationFailed("milestones 至少包含两个施工节点")
        milestones = tuple(MilestoneInput.from_dict(item, index) for index, item in enumerate(rows))
        if len({item.code for item in milestones}) != len(milestones):
            raise ValidationFailed("施工节点编号不能重复")
        total_weight = sum((item.weight_percent for item in milestones), ZERO)
        if total_weight != HUNDRED:
            raise ValidationFailed(f"施工节点权重之和必须为 100（当前 {total_weight}）")
        ordered = sorted(milestones, key=lambda item: item.plan_date)
        if len({item.plan_date for item in ordered}) != len(ordered):
            raise ValidationFailed("施工节点计划日期不能相同")
        if tuple(ordered) != tuple(milestones):
            raise ValidationFailed("施工节点必须按计划日期升序排列")
        movein = date_text(raw.get("latest_movein_date"), "latest_movein_date")
        if ordered[-1].plan_date > movein:
            raise ValidationFailed("最后一个施工节点不得晚于最迟入住日期")
        central = money(decimal_value(raw.get("central_amount"), "central_amount"))
        local = money(decimal_value(raw.get("local_amount"), "local_amount"))
        household = money(decimal_value(raw.get("household_amount"), "household_amount"))
        if central + local + household <= ZERO:
            raise ValidationFailed("资金构成总额必须大于零")
        return cls(
            project_id=identifier(raw.get("project_id"), "project_id"),
            household_id=identifier(raw.get("household_id"), "household_id"),
            township_id=identifier(raw.get("township_id"), "township_id"),
            budget_id=identifier(raw.get("budget_id"), "budget_id"),
            appraisal_grade=grade,
            risk_level=risk,
            latest_movein_date=movein,
            central_amount=central,
            local_amount=local,
            household_amount=household,
            milestones=milestones,
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class EmergencyExemptionInput:
    project_id: str
    grantor_id: str
    reason: str
    expires_at: str
    gates: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "EmergencyExemptionInput":
        gates_raw = raw.get("gates", list(EXEMPTIBLE_GATES))
        if not isinstance(gates_raw, list) or not gates_raw:
            raise ValidationFailed("gates 必须是非空数组")
        gates = tuple(required_text(item, "gates[]", 48) for item in gates_raw)
        unknown = [gate for gate in gates if gate not in EXEMPTIBLE_GATES]
        if unknown:
            raise ValidationFailed(f"以下门禁不可豁免: {', '.join(sorted(set(unknown)))}")
        if len(set(gates)) != len(gates):
            raise ValidationFailed("gates 不能重复")
        expires_at = timestamp_text(raw.get("expires_at"), "expires_at")
        return cls(
            project_id=identifier(raw.get("project_id"), "project_id"),
            grantor_id=required_text(raw.get("grantor_id"), "grantor_id", 64),
            reason=required_text(raw.get("reason"), "reason", 512),
            expires_at=expires_at,
            gates=tuple(sorted(gates)),
        )
