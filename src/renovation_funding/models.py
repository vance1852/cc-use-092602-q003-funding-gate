"""危房改造资金门禁的输入契约与解析。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .clock import parse_utc
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")

# 鉴定等级：A 安全 / B 基本安全 / C 局部危险 / D 整体危险
RISK_GRADES = {"A", "B", "C", "D"}
GRADE_RANK = {"A": 1, "B": 2, "C": 3, "D": 4}
FUND_COMPONENTS = ("central_subsidy", "local_match", "household_self_raise")
PLAN_STATES = ("pending", "confirmed", "active", "paused", "completed", "cancelled", "expired")


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


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def date_text(value: object, field: str) -> str:
    result = required_text(value, field, 10)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValidationFailed(f"{field} 必须是 YYYY-MM-DD 日期") from exc


@dataclass(frozen=True, slots=True)
class FundRelease:
    """财政人员发布的年度资金额度版本。"""

    release_id: str
    fiscal_year: int
    total_amount: Decimal
    effective_from: str
    expires_at: str
    note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "FundRelease":
        fiscal_year = raw.get("fiscal_year")
        if isinstance(fiscal_year, bool) or not isinstance(fiscal_year, int) or not 2000 <= fiscal_year <= 2100:
            raise ValidationFailed("fiscal_year 必须是 2000 到 2100 的整数")
        effective_from = required_text(raw.get("effective_from"), "effective_from", 40)
        expires_at = required_text(raw.get("expires_at"), "expires_at", 40)
        try:
            start = parse_utc(effective_from, "effective_from")
            end = parse_utc(expires_at, "expires_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if end <= start:
            raise ValidationFailed("expires_at 必须晚于 effective_from")
        return cls(
            release_id=identifier(raw.get("release_id"), "release_id"),
            fiscal_year=fiscal_year,
            total_amount=decimal_value(raw.get("total_amount"), "total_amount", minimum=Decimal("0.01")),
            effective_from=effective_from,
            expires_at=expires_at,
            note=required_text(raw.get("note"), "note", 512),
        )


@dataclass(frozen=True, slots=True)
class Milestone:
    code: str
    name: str
    planned_date: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], index: int) -> "Milestone":
        code = identifier(raw.get("code"), f"milestones[{index}].code")
        return cls(
            code=code,
            name=required_text(raw.get("name"), f"milestones[{index}].name", 128),
            planned_date=date_text(raw.get("planned_date"), f"milestones[{index}].planned_date"),
        )


@dataclass(frozen=True, slots=True)
class FundingMix:
    central_subsidy: Decimal
    local_match: Decimal
    household_self_raise: Decimal

    @property
    def total(self) -> Decimal:
        return self.central_subsidy + self.local_match + self.household_self_raise

    def as_dict(self) -> dict[str, str]:
        return {
            "central_subsidy": format(self.central_subsidy, "f"),
            "local_match": format(self.local_match, "f"),
            "household_self_raise": format(self.household_self_raise, "f"),
        }


@dataclass(frozen=True, slots=True)
class RenovationPlan:
    """乡镇为改造项目申报的风险、节点与资金构成。"""

    plan_id: str
    household_id: str
    township_id: str
    risk_grade: str
    estimated_cost: Decimal
    latest_move_in_date: str
    milestones: tuple[Milestone, ...]
    funding_mix: FundingMix
    construction_team_id: str
    household_commitment: str
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RenovationPlan":
        risk_grade = required_text(raw.get("risk_grade"), "risk_grade", 4).upper()
        if risk_grade not in RISK_GRADES:
            raise ValidationFailed("risk_grade 必须是 A、B、C 或 D")
        estimated_cost = decimal_value(raw.get("estimated_cost"), "estimated_cost", minimum=Decimal("0.01"))
        raw_milestones = raw.get("milestones")
        if not isinstance(raw_milestones, list) or not raw_milestones:
            raise ValidationFailed("milestones 至少包含一个施工节点")
        if len(raw_milestones) > 32:
            raise ValidationFailed("milestones 不能超过 32 个节点")
        milestones = tuple(Milestone.from_dict(item, index) for index, item in enumerate(raw_milestones))
        codes = [item.code for item in milestones]
        if len(set(codes)) != len(codes):
            raise ValidationFailed("施工节点编号不能重复")
        previous: str | None = None
        for item in milestones:
            if previous is not None and item.planned_date < previous:
                raise ValidationFailed("施工节点日期必须按计划顺序递增")
            previous = item.planned_date
        latest_move_in_date = date_text(raw.get("latest_move_in_date"), "latest_move_in_date")
        if milestones[-1].planned_date > latest_move_in_date:
            raise ValidationFailed("最后一个施工节点不能晚于最迟入住日期")
        mix_raw = raw.get("funding_mix")
        if not isinstance(mix_raw, Mapping):
            raise ValidationFailed("funding_mix 必须是对象")
        funding_mix = FundingMix(
            central_subsidy=decimal_value(mix_raw.get("central_subsidy"), "funding_mix.central_subsidy", minimum=Decimal("0")),
            local_match=decimal_value(mix_raw.get("local_match"), "funding_mix.local_match", minimum=Decimal("0")),
            household_self_raise=decimal_value(
                mix_raw.get("household_self_raise"), "funding_mix.household_self_raise", minimum=Decimal("0")
            ),
        )
        if funding_mix.total <= 0:
            raise ValidationFailed("资金构成总额必须大于 0")
        if funding_mix.total != estimated_cost:
            raise ValidationFailed("资金构成合计必须与 estimated_cost 一致")
        if funding_mix.household_self_raise > 0 and not str(raw.get("household_commitment", "")).strip():
            raise ValidationFailed("存在家庭自筹时必须登记 household_commitment 自筹承诺")
        return cls(
            plan_id=identifier(raw.get("plan_id"), "plan_id"),
            household_id=identifier(raw.get("household_id"), "household_id"),
            township_id=identifier(raw.get("township_id"), "township_id"),
            risk_grade=risk_grade,
            estimated_cost=estimated_cost,
            latest_move_in_date=latest_move_in_date,
            milestones=milestones,
            funding_mix=funding_mix,
            construction_team_id=identifier(raw.get("construction_team_id"), "construction_team_id"),
            household_commitment=required_text(raw.get("household_commitment"), "household_commitment", 512)
            if funding_mix.household_self_raise > 0
            else str(raw.get("household_commitment", "")).strip(),
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )
