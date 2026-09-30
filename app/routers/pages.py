from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional
import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2.utils import markupsafe
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.auth import get_current_user
from app.db import get_db
from app.models import DipLot, Vat, Workshop
from app.services.vat_rules import VatRuleError, validate_lot_upsert, validate_vat_status_change

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _tojson(value):
    return markupsafe.Markup(json.dumps(value, ensure_ascii=False))


templates.env.filters["tojson"] = _tojson

STATUS_LABELS = {
    Vat.STATUS_IDLE: "闲置",
    Vat.STATUS_REDUCING: "还原中",
    Vat.STATUS_READY: "可染色",
}


def render(request: Request, name: str, context: dict, status_code: int = 200):
    ctx = {k: v for k, v in context.items() if k != "request"}
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def _need_login(request: Request, db: Session):
    return get_current_user(request, db)


def _spark_points(lots: list[DipLot], width: int = 72, height: int = 28) -> list[dict]:
    """把 redox 序列压成 sparkline 坐标（无有效读数则空）。"""
    vals = [float(l.redoxMv) for l in lots if l.redoxMv is not None]
    if not vals:
        return []
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    n = len(vals)
    pts = []
    for i, v in enumerate(vals):
        x = 0 if n == 1 else round(i * (width - 1) / (n - 1), 2)
        y = round(height - 1 - ((v - lo) / span) * (height - 1), 2)
        pts.append({"x": x, "y": y})
    return pts


def _vat_payload(vat: Vat) -> dict:
    lots = sorted(vat.lots, key=lambda x: (x.dippedAt, x.id))
    chronological = lots
    latest = lots[-1] if lots else None
    recent = list(reversed(lots[-8:]))  # 展开区展示近几笔
    return {
        "id": vat.id,
        "code": vat.code,
        "dyeType": vat.dyeType,
        "volumeL": float(vat.volumeL),
        "status": vat.status,
        "statusLabel": STATUS_LABELS.get(vat.status, vat.status),
        "workshopId": vat.workshop_id,
        "workshopName": vat.workshop.name if vat.workshop else "",
        "lastRedox": float(latest.redoxMv) if latest and latest.redoxMv is not None else None,
        "lastMeters": float(latest.clothMeters) if latest else None,
        "lastDippedAt": latest.dippedAt.strftime("%Y-%m-%d %H:%M") if latest else None,
        "spark": _spark_points(chronological),
        "recentLots": [
            {
                "id": l.id,
                "dippedAt": l.dippedAt.strftime("%Y-%m-%d %H:%M"),
                # datetime-local 编辑框需要 YYYY-MM-DDTHH:MM
                "dippedAtInput": l.dippedAt.strftime("%Y-%m-%dT%H:%M"),
                "clothMeters": float(l.clothMeters),
                "redoxMv": float(l.redoxMv) if l.redoxMv is not None else None,
            }
            for l in recent
        ],
    }


def _bay_context(
    request: Request,
    db: Session,
    user,
    workshop_id: Optional[int] = None,
    selected_vat: Optional[int] = None,
    error: Optional[str] = None,
):
    # 始终下发全部缸位；工坊仅作前端 chip 筛选，避免切回「全部」时缺数据
    workshops = db.query(Workshop).order_by(Workshop.name).all()
    vats = (
        db.query(Vat)
        .options(joinedload(Vat.workshop), joinedload(Vat.lots))
        .order_by(Vat.code)
        .all()
    )
    return {
        "request": request,
        "user": user,
        "workshops": [{"id": w.id, "name": w.name, "region": w.region} for w in workshops],
        "vats": [_vat_payload(v) for v in vats],
        "filter_workshop": workshop_id,
        "selected_vat": selected_vat,
        "error": error,
        "status_labels": STATUS_LABELS,
        "active": "bay",
    }


@router.get("/", response_class=HTMLResponse)
async def bay(
    request: Request,
    workshop: Optional[int] = None,
    vat: Optional[int] = None,
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return render(request, "bay.html", _bay_context(request, db, user, workshop, vat))


@router.post("/bay/vats/{pk}/status", response_class=HTMLResponse)
async def bay_vat_status(
    pk: int,
    request: Request,
    status: str = Form(...),
    workshop: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    item = (
        db.query(Vat)
        .options(joinedload(Vat.workshop), joinedload(Vat.lots))
        .filter(Vat.id == pk)
        .first()
    )
    ws = int(workshop) if workshop.strip() else None
    if not item:
        return RedirectResponse("/", status_code=303)
    error = None
    try:
        latest = item.latest_lot()
        validate_vat_status_change(item, status, latest)
        item.status = status
        db.commit()
        return RedirectResponse(f"/?vat={pk}" + (f"&workshop={ws}" if ws else ""), status_code=303)
    except VatRuleError as exc:
        error = exc.message
        db.rollback()
    return render(
        request,
        "bay.html",
        _bay_context(request, db, user, ws, pk, error),
        status_code=400,
    )


def _load_vat(db: Session, pk: int) -> Optional[Vat]:
    return (
        db.query(Vat)
        .options(joinedload(Vat.workshop), joinedload(Vat.lots))
        .filter(Vat.id == pk)
        .first()
    )


def _parse_lot_fields(dipped_at: str, cloth_meters: str, redox_mv: str):
    """解析浸染表单并把时刻统一归一化为 UTC 带时区时间。

    datetime-local 提交的是无时区的本地墙钟时间；统一按 UTC 落库，
    保证与种子时刻及同缸各笔之间的先后比较口径一致。
    """
    try:
        dt = datetime.fromisoformat(dipped_at)
    except ValueError as exc:
        raise ValueError(f"浸染时间格式无效：{exc}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    meters = Decimal(cloth_meters)
    redox = Decimal(redox_mv) if redox_mv.strip() else None
    return dt, meters, redox


def _lot_form_failure(
    request: Request,
    db: Session,
    user,
    pk: int,
    ws: Optional[int],
    error: str,
):
    """任何拒绝路径都先回滚（不留半截记录），再以 400 重开还原台。"""
    db.rollback()
    return render(
        request,
        "bay.html",
        _bay_context(request, db, user, ws, pk, error),
        status_code=400,
    )


@router.post("/bay/vats/{pk}/lots", response_class=HTMLResponse)
async def bay_log_lot(
    pk: int,
    request: Request,
    dippedAt: str = Form(...),
    clothMeters: str = Form(...),
    redoxMv: str = Form(""),
    workshop: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    item = _load_vat(db, pk)
    ws = int(workshop) if workshop.strip() else None
    if not item:
        return RedirectResponse("/", status_code=303)
    try:
        dt, meters, redox = _parse_lot_fields(dippedAt, clothMeters, redoxMv)
        # 与更新共用同一套时刻规则（闲置拒记 / 严格递增 / 时刻唯一）
        validate_lot_upsert(item, dt, db, existing_lot=None)
        # 规则全部通过后才把新笔加入会话，失败时会话里不存在半截对象
        lot = DipLot(vat_id=pk, dippedAt=dt, clothMeters=meters, redoxMv=redox)
        db.add(lot)
        db.commit()
        return RedirectResponse(f"/?vat={pk}" + (f"&workshop={ws}" if ws else ""), status_code=303)
    except (ValueError, InvalidOperation) as exc:
        return _lot_form_failure(request, db, user, pk, ws, f"浸染记录无效：{exc}")
    except VatRuleError as exc:
        return _lot_form_failure(request, db, user, pk, ws, exc.message)
    except IntegrityError:
        # 唯一约束兜底：并发/连交两笔相同时刻，只允许一笔，另一笔完整回滚
        return _lot_form_failure(
            request,
            db,
            user,
            pk,
            ws,
            "该缸已有相同时刻的浸染笔：同缸时刻须严格递增、互不相等，本次登记已取消。",
        )


@router.post("/bay/vats/{pk}/lots/{lot_id}/edit", response_class=HTMLResponse)
async def bay_edit_lot(
    pk: int,
    lot_id: int,
    request: Request,
    dippedAt: str = Form(...),
    clothMeters: str = Form(...),
    redoxMv: str = Form(""),
    workshop: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    item = _load_vat(db, pk)
    ws = int(workshop) if workshop.strip() else None
    if not item:
        return RedirectResponse("/", status_code=303)
    lot = (
        db.query(DipLot)
        .filter(DipLot.id == lot_id, DipLot.vat_id == pk)
        .first()
    )
    if lot is None:
        return RedirectResponse(f"/?vat={pk}" + (f"&workshop={ws}" if ws else ""), status_code=303)
    try:
        dt, meters, redox = _parse_lot_fields(dippedAt, clothMeters, redoxMv)
        # 同一条规则入口：先校验（此时刻本笔尚未被改动，邻笔定位准确），通过后才赋值
        validate_lot_upsert(item, dt, db, existing_lot=lot)
        lot.dippedAt = dt
        lot.clothMeters = meters
        lot.redoxMv = redox
        db.commit()
        return RedirectResponse(f"/?vat={pk}" + (f"&workshop={ws}" if ws else ""), status_code=303)
    except (ValueError, InvalidOperation) as exc:
        return _lot_form_failure(request, db, user, pk, ws, f"浸染记录无效：{exc}")
    except VatRuleError as exc:
        return _lot_form_failure(request, db, user, pk, ws, exc.message)
    except IntegrityError:
        return _lot_form_failure(
            request,
            db,
            user,
            pk,
            ws,
            "该缸已有相同时刻的浸染笔：同缸时刻须严格递增、互不相等，本次改笔已取消。",
        )


# 旧顶栏 CRUD 路径一律回到还原台，避免「换皮表页」残留入口
@router.get("/workshops")
@router.get("/vats")
@router.get("/lots")
@router.get("/home")
async def legacy_redirect():
    return RedirectResponse("/", status_code=303)
