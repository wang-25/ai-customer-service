"""FastAPI 应用入口：装配路由 + 静态页面 + 后台定时任务 + WebSocket 实时通道。"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, embedding, handoff, realtime
from .routers import admin, agent, auth_api, user, ws

STATIC_DIR = config.BASE_DIR / "static"

SWEEP_INTERVAL = 60  # 秒：扫描超时会话


async def _sweeper() -> None:
    """后台定时任务：结束超时会话（PRD M1-2 超时自动结束）。"""
    while True:
        await asyncio.sleep(SWEEP_INTERVAL)
        try:
            await asyncio.to_thread(handoff.sweep_timeouts)
        except Exception:  # noqa: BLE001 - 定时任务不允许中断服务
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    count = db.init_db(seed=True)
    realtime.realtime.bind_loop(asyncio.get_running_loop())
    task = asyncio.create_task(_sweeper())
    beat = asyncio.create_task(realtime.realtime.heartbeat_forever()) if config.WS_ENABLED else None
    # 启动即构建向量索引（离线 provider 几十毫秒；在线 provider 只补增量）
    index_result = embedding.build_index()
    print("=" * 66)
    print(f"[启动] 数据库      : {config.DB_PATH}")
    print(f"[启动] 知识库      : {count} 条 / 文档 {len(db.list_documents())} 个")
    print(f"[启动] 对话模式    : "
          f"{'知识库检索 + 大模型润色' if config.llm_configured() else '纯离线知识库检索（未配置大模型 Key）'}")
    print(f"[启动] 向量召回    : {embedding.current().label}"
          f"（索引 {index_result.get('indexed', 0)} 条 / {index_result.get('elapsed_ms', 0)} ms）")
    print(f"[启动] 实时通道    : "
          f"{'WebSocket 已启用（心跳 ' + str(config.WS_HEARTBEAT_SECONDS) + 's）' if config.WS_ENABLED else '已关闭（前端退化为轮询）'}")
    print(f"[启动] 权限认证    : {'已启用（四角色）' if config.AUTH_ENABLED else '未启用（演示模式，全部权限）'}")
    print(f"[启动] 人工服务时间: {db.get_bot_config().get('service_hours_text')}"
          f"（当前{'在线' if db.is_service_online() else '离线'}）")
    print("=" * 66)
    try:
        yield
    finally:
        task.cancel()
        if beat:
            beat.cancel()


app = FastAPI(
    title="AI 智能客服 MVP",
    version="2.0.0",
    description=(
        "按 PRD P0 清单实现的 AI 智能客服 MVP：知识库问答、多轮槽位澄清、"
        "转人工全链路、客服工作台、文档导入、数据看板、机器人配置与角色权限。"
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(user.router)
app.include_router(agent.router)
app.include_router(admin.router)
app.include_router(auth_api.router)
app.include_router(ws.router)


# ==================================================================== 页面
@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/admin", include_in_schema=False)
def admin_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "admin.html")


@app.get("/agent", include_in_schema=False)
def agent_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "agent.html")


@app.get("/dashboard", include_in_schema=False)
def dashboard_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "dashboard.html")


@app.get("/login", include_in_schema=False)
def login_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "login.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
