"""浸染批次写路径：新建与更新共用同一套时刻规则。

规则（同一缸 vat 内）：
1. 缸必须处于「还原中 reducing」或「可染色 ready」；闲置 idle 缸一律拒绝登记；
2. 浸染时刻在同缸内严格递增，相同时刻至多一笔入库；
3. 新建：新时刻必须严格晚于该缸已有最晚一笔；
4. 更新：只能在被改那笔的相邻前驱、后继时刻之间挪动，相等或跨越都拒绝。
"""

from datetime import datetime, timezone
from typing import Optional, Sequence

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import DipLot, Vat
from app.services.vat_rules import VatRuleError

# 允许登记浸染的缸状态时段语义
DIPPABLE_STATUSES = (Vat.STATUS_REDUCING, Vat.STATUS_READY)


def normalize_dipped_at(value: datetime) -> datetime:
    """表单 datetime-local 提交的是无时区时间，统一按 UTC 落库；带时区则换算到 UTC。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """比较用：naive 时刻按 UTC 解释，保证两侧都有时区再比大小。"""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def sorted_dip_lots(lots: Sequence[DipLot]) -> list[DipLot]:
    """同缸批次按 (时刻, id) 严格升序；页面行列与复算都用它。"""
    return sorted(lots, key=lambda lot: (_as_utc(lot.dippedAt), lot.id))


def validate_dip_timing(
    vat: Vat,
    dipped_at: datetime,
    siblings: Sequence[DipLot],
    exclude_id: Optional[int] = None,
) -> None:
    """校验同缸浸染时刻。新建与更新两条写路径共用本函数。

    siblings 为该缸全部批次；更新时传 exclude_id 跳过被改的那笔自身。
    """
    if vat.status not in DIPPABLE_STATUSES:
        raise VatRuleError(
            "闲置缸拒绝新浸染：请先把缸位转为还原中或可染色后再登记。"
        )

    if exclude_id is None:
        # 新建：必须严格晚于现有最晚一笔（相等也拒绝）
        latest = sorted_dip_lots(siblings)[-1] if siblings else None
        if latest is not None and dipped_at <= _as_utc(latest.dippedAt):
            raise VatRuleError(
                "浸染时刻必须严格晚于本缸已有最晚一笔，且不能与已有时刻相同。"
            )
        return

    # 更新：在含自身的完整序列里定位相邻两笔，新时刻须落在二者之间
    ordered = sorted_dip_lots(siblings)
    try:
        index = next(i for i, lot in enumerate(ordered) if lot.id == exclude_id)
    except StopIteration:
        raise VatRuleError("待修改的浸染批次不存在。")
    predecessor = ordered[index - 1] if index > 0 else None
    successor = ordered[index + 1] if index + 1 < len(ordered) else None
    if predecessor is not None and dipped_at <= _as_utc(predecessor.dippedAt):
        raise VatRuleError(
            "修改浸染时刻不得早于或等于上一笔，同缸时刻必须严格递增。"
        )
    if successor is not None and dipped_at >= _as_utc(successor.dippedAt):
        raise VatRuleError(
            "修改浸染时刻不得晚于或等于下一笔，不能打乱同缸既有递增次序。"
        )


def _load_siblings(db: Session, vat_id: int) -> list[DipLot]:
    return (
        db.query(DipLot)
        .filter(DipLot.vat_id == vat_id)
        .order_by(DipLot.dippedAt, DipLot.id)
        .all()
    )


def create_dip_lot(
    db: Session,
    vat: Vat,
    dipped_at: datetime,
    cloth_meters,
    redox_mv,
) -> DipLot:
    """新建浸染批次；时刻不合法抛 VatRuleError，失败不残留半笔。"""
    dipped_at = normalize_dipped_at(dipped_at)
    validate_dip_timing(vat, dipped_at, _load_siblings(db, vat.id), exclude_id=None)
    lot = DipLot(
        vat_id=vat.id,
        dippedAt=dipped_at,
        clothMeters=cloth_meters,
        redoxMv=redox_mv,
    )
    db.add(lot)
    try:
        db.flush()  # 让同缸 (vat_id, dippedAt) 唯一约束即时兜底并发同时刻
    except IntegrityError:
        db.rollback()
        raise VatRuleError("同缸相同时刻至多允许一笔，该时刻已有记录。")
    return lot


def update_dip_lot(
    db: Session,
    vat: Vat,
    lot: DipLot,
    dipped_at: datetime,
    cloth_meters,
    redox_mv,
) -> DipLot:
    """更新已有浸染批次；与新建共用 validate_dip_timing，越序/相等一律拒绝。"""
    dipped_at = normalize_dipped_at(dipped_at)
    validate_dip_timing(
        vat, dipped_at, _load_siblings(db, vat.id), exclude_id=lot.id
    )
    lot.dippedAt = dipped_at
    lot.clothMeters = cloth_meters
    lot.redoxMv = redox_mv
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise VatRuleError("同缸相同时刻至多允许一笔，该时刻已有记录。")
    return lot
