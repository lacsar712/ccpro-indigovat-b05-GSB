import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from app.db import Base, SessionLocal, engine
from app.routers import auth, pages
from app.seed import ensure_seed_data

logger = logging.getLogger("indigovat")


def _ensure_lot_time_unique_index() -> None:
    """给老库卷幂等补上「同缸浸染时刻唯一」索引（create_all 不会改既有表）。

    若历史数据里已存在同缸相同时刻的重复笔，只告警不建索引、不阻断启动；
    业务规则仍会拦住一切新的重复/乱序写入。
    """
    with engine.begin() as conn:
        duplicated = conn.execute(
            text(
                'SELECT 1 FROM dip_lots '
                'GROUP BY vat_id, "dippedAt" HAVING count(*) > 1 LIMIT 1'
            )
        ).first()
        if duplicated is not None:
            logger.warning(
                "dip_lots 已存在同缸相同时刻的历史记录，跳过唯一索引；"
                "请先清理重复笔后手动建立 uniq_lot_time_per_vat。"
            )
            return
        conn.execute(
            text(
                'CREATE UNIQUE INDEX IF NOT EXISTS uniq_lot_time_per_vat '
                'ON dip_lots (vat_id, "dippedAt")'
            )
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    _ensure_lot_time_unique_index()
    db = SessionLocal()
    try:
        ensure_seed_data(db)
    finally:
        db.close()
    yield


app = FastAPI(title="IndigoVat 染缸还原台", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET", "dev-indigovat-session-secret"),
    session_cookie="indigovat_session",
    same_site="lax",
    https_only=False,
)

static_dir = Path(__file__).resolve().parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(auth.router)
app.include_router(pages.router)
