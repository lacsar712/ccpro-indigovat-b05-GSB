# IndigoVat-01 · 染缸还原台

FastAPI + PostgreSQL + Jinja2：主界面是横向**缸位条**（Alpine 反应式），不是工坊/染缸/批次三表导航。Session Cookie 登录；规则在 `app/services/vat_rules.py`。

## 技术栈

- FastAPI、SQLAlchemy 2、PostgreSQL
- 启动时 `create_all` + 幂等种子（蓝靛湾一号坊 / 清水江二号坊）
- Session Cookie 认证（Starlette SessionMiddleware）
- Jinja2 + Alpine.js + Pico（叠靛蓝水墨自定义样式）
- Docker Compose：`web` + `db`

## 端口与数据库

| 服务 | 端口 |
|------|------|
| Web  | **4720** |
| Postgres | **6120**（容器内 5432） |

数据库账号：`indigovat` / `indigovat` / 库名 `indigovat`

## 快速启动

```bash
cd IndigoVat/IndigoVat-01
docker compose up --build -d
```

浏览器打开：http://localhost:4720

演示账号（登录页已预填）：

- `admin` / `123456`
- `worker` / `123456`

## 交互（信息架构）

1. **染缸还原台**：横滑缸位条，每缸显示状态、最近电位与 redox sparkline
2. **工坊 chip**：仅作缸位筛选，无独立工坊 CRUD 页
3. **点缸展开**：同页内登记浸染批次、改状态、看近几笔；无平行「染缸表 / 批次表」

**业务规则**：

- 状态改为 `ready`（可染色）时，最新批次 `redoxMv` 须已填且 ≤ -500（见 `vat_rules.py`）。
- **浸染时刻规则**（新建与更新共用同一套校验，入口 `validate_lot_upsert`）：
  1. 浸染时刻必须落在缸处于 `reducing`（还原中）或 `ready`（可染色）的语义时段内；`idle`（闲置）缸拒绝登记新浸染、也拒绝改旧笔。
  2. 同缸浸染时刻**严格递增、互不相等**：
     - 新建：`新时刻 > 本缸已有最晚浸染时刻`，早于或等于最晚笔一律拒绝；
     - 更新（近几笔里点「改」）：被改笔的新时刻必须严格晚于其前一笔、严格早于其后一笔——只能在原序位的相邻两笔之间移动，不能越过或等于邻笔，因此改旧笔不会打乱递增序；尾笔只能往更晚改、头笔只受后笔约束。
  3. 「严格递增」的含义是相邻两笔满足 `t₁ < t₂ < t₃ …`，相等不算递增。同缸连交两笔相同时刻，第二笔在业务层即被拒；数据库另有 `uniq_lot_time_per_vat (vat_id, dippedAt)` 唯一索引兜底并发，重复时刻至多一笔入库，被拒那笔完整回滚、不留半截记录。
  4. 按缸列出浸染时一律按 `(dippedAt, id)` 排序后复算（最近笔、电位、近几笔列表、sparkline 同源），页面行/列顺序与该排序一致；样例缸 V-01 预置五笔严格递增的浸染序列。

## 本地开发（可选）

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
set POSTGRES_HOST=localhost
set POSTGRES_PORT=6120
uvicorn app.main:app --host 0.0.0.0 --port 4720 --reload
```

## 业务模型

1. **Workshop**：`name`、`region`、`notes`（UI 上仅为筛选片）
2. **Vat**：归属工坊、`code`、`dyeType`、`volumeL`、状态 `idle|reducing|ready`
3. **DipLot**：归属染缸、`dippedAt`、`clothMeters`、`redoxMv`（可空）

## 目录结构

```
IndigoVat-01/
  Dockerfile
  entrypoint.sh
  docker-compose.yml
  requirements.txt
  app/
    main.py
    db.py
    models.py
    schemas.py
    auth.py
    seed.py
    routers/
    services/vat_rules.py
    templates/   # base / bay / login
```
