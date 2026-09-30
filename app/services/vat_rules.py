"""染缸状态与浸染时刻业务规则。

时刻规则（新建与更新两条写路径共用同一套规则）
------------------------------------------------
1. 浸染时刻必须落在缸处于「还原中 reducing」或「可染色 ready」的语义时段内；
   闲置 idle 缸拒绝一切浸染登记与改笔。
2. 同一缸内浸染时刻必须**严格递增、互不相等**：
   - 新建：新时刻必须严格晚于该缸已有最晚浸染时刻（t > 最晚笔）；
   - 更新：改旧笔时只能在其时间序上的直接前驱（不含）与直接后继（不含）之间移动，
     即不允许早于/等于前驱、不允许晚于/等于后继，因此**绝不打乱递增序**；
   尾笔（无后继）只能往更晚改且仍须严格晚于前驱；头笔（无前驱）只受后继下界约束。
3. 同缸连交两笔相同时刻，第二笔必被业务规则拒绝；另设 ``(vat_id, dippedAt)``
   唯一约束（见 ``app/main.py`` 的幂等建索引）作为并发/历史数据兜底——
   双成功的唯一可能是一成功一完整回滚，绝不残留半截记录。
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from app.models import DipLot, Vat

# 允许浸染的缸状态：还原中 / 可染色
DIP_ALLOWED_STATUSES = frozenset({Vat.STATUS_REDUCING, Vat.STATUS_READY})


def _as_utc(value: datetime) -> datetime:
    """统一时刻口径：库内时刻一律按 UTC 存储。

    PostgreSQL 经 ``DateTime(timezone=True)`` 读回为带时区时间；SQLite 等会丢掉
    时区信息，此时按 UTC 解释，避免带/不带时区时间无法比较。
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class VatRuleError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def assert_can_dip(vat: Vat) -> None:
    """缸必须处于还原中/可染色语义时段，闲置缸拒绝登记/修改浸染。"""
    if vat.status not in DIP_ALLOWED_STATUSES:
        raise VatRuleError(
            "闲置中的染缸不能登记浸染：请先把缸转为还原中或可染色。"
        )


def assert_lot_create_order(vat_id: int, dipped_at: datetime, db: Session) -> None:
    """新建浸染：时刻必须严格晚于该缸已有最晚浸染（t > max）。

    相同时刻（相等）一律拒绝，因此同缸连交两笔相同时刻时至多一笔入库。
    """
    latest = (
        db.query(DipLot.dippedAt)
        .filter(DipLot.vat_id == vat_id)
        .order_by(DipLot.dippedAt.desc(), DipLot.id.desc())
        .first()
    )
    if latest is not None and dipped_at <= _as_utc(latest[0]):
        raise VatRuleError(
            "浸染时刻必须严格递增：新时刻须晚于本缸已有最晚浸染"
            f"（{latest[0]:%Y-%m-%d %H:%M}），不得早于或等于它。"
        )


def assert_lot_update_order(
    existing_lot: DipLot,
    vat_id: int,
    new_time: datetime,
    db: Session,
) -> None:
    """改旧笔的时刻只能留在其当前时间序位置上的直接前驱与直接后继之间。

    邻笔按该笔**改动前**在 ``(dippedAt, id)`` 序中的位置定位（而非按新时刻的插入
    点），因此新时刻必须严格大于前一笔、严格小于后一笔——改笔既不能越过邻笔，也
    不能与邻笔相等，绝不打乱递增序。头笔无前驱、尾笔无后继，相应一侧不设限；
    新时刻与原时刻相同（无实际改动）天然落在区间内，允许提交。
    """
    # 前一笔：排序键严格位于本笔（旧时刻）之前的最晚一笔
    predecessor = (
        db.query(DipLot)
        .filter(
            DipLot.vat_id == vat_id,
            DipLot.id != existing_lot.id,
            DipLot.dippedAt <= existing_lot.dippedAt,
        )
        .order_by(DipLot.dippedAt.desc(), DipLot.id.desc())
        .first()
    )
    # 后一笔：排序键严格位于本笔（旧时刻）之后的最早一笔
    successor = (
        db.query(DipLot)
        .filter(
            DipLot.vat_id == vat_id,
            DipLot.id != existing_lot.id,
            DipLot.dippedAt >= existing_lot.dippedAt,
        )
        .order_by(DipLot.dippedAt.asc(), DipLot.id.asc())
        .first()
    )
    new_time = _as_utc(new_time)
    if predecessor is not None and new_time <= _as_utc(predecessor.dippedAt):
        raise VatRuleError(
            "改笔不能打乱递增序：新时刻须严格晚于前一笔"
            f"（{predecessor.dippedAt:%Y-%m-%d %H:%M}），不得早于或等于它。"
        )
    if successor is not None and new_time >= _as_utc(successor.dippedAt):
        raise VatRuleError(
            "改笔不能打乱递增序：新时刻须严格早于后一笔"
            f"（{successor.dippedAt:%Y-%m-%d %H:%M}），不得晚于或等于它。"
        )


def validate_lot_upsert(
    vat: Vat,
    dipped_at: datetime,
    db: Session,
    existing_lot: Optional[DipLot] = None,
) -> None:
    """浸染时刻规则的唯一入口，新建与更新两条写路径共用。

    :param vat: 目标染缸
    :param dipped_at: 本次写入的时刻（已归一化为带时区的 ``datetime``）
    :param db: 数据库会话
    :param existing_lot: 新建传 ``None``；更新传被改的那一笔。
    """
    assert_can_dip(vat)
    if existing_lot is None:
        assert_lot_create_order(vat.id, dipped_at, db)
    else:
        assert_lot_update_order(existing_lot, vat.id, dipped_at, db)


def assert_can_mark_ready(latest: Optional[DipLot]) -> None:
    """不能将染缸标为 ready，除非最新浸染批次 redoxMv 已填且 <= -500。"""
    if latest is None or latest.redoxMv is None or Decimal(latest.redoxMv) > Decimal("-500"):
        raise VatRuleError(
            "无法设为可染色：最新浸染批次的氧化还原电位为空或高于 -500 mV。"
        )


def validate_vat_status_change(vat: Vat, new_status: str, latest: Optional[DipLot]) -> None:
    if new_status == Vat.STATUS_READY:
        assert_can_mark_ready(latest)
