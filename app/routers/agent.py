"""客服工作台接口（PRD M5-1 / M5-2 / M5-3 + M4-3 上下文同步）。

未启用认证时（默认），工作台通过 `agent_id` 参数或「客服切换器」选择当前坐席，
方便单人演示；启用认证后自动绑定登录账号对应的客服。
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import auth, config, db, handoff, realtime
from ..schemas import AgentReplyRequest, AgentStatusRequest, CannedIn, CannedUpdate

router = APIRouter(prefix="/api", tags=["客服工作台"])

_can_work = auth.require_role("agent", "supervisor")

# 超时扫描在列表接口上做节流：工作台轮询时不必每次都全表扫描
_last_sweep = 0.0
_SWEEP_MIN_INTERVAL = 30.0


def _sweep_throttled() -> None:
    global _last_sweep
    if time.time() - _last_sweep < _SWEEP_MIN_INTERVAL:
        return
    _last_sweep = time.time()
    handoff.sweep_timeouts()


def ensure_demo_agents() -> None:
    """未启用认证时，自动准备 2 个演示坐席。"""
    if db.list_agents():
        return
    db.create_agent("客服小美", status="online")
    db.create_agent("客服小刚", status="offline")


def resolve_agent(request: Request, account: dict[str, Any]) -> dict[str, Any]:
    """确定当前操作的客服身份。"""
    if config.AUTH_ENABLED:
        agent = db.get_agent_by_account(account["id"])
        if not agent:
            agent_id = db.create_agent(account.get("name") or account["username"],
                                       account_id=account["id"], status="offline")
            agent = db.get_agent(agent_id)
        return agent  # type: ignore[return-value]

    ensure_demo_agents()
    raw = request.query_params.get("agent_id") or request.headers.get("x-agent-id")
    if raw and str(raw).isdigit():
        agent = db.get_agent(int(raw))
        if agent:
            return agent
    return db.list_agents()[0]


# ==================================================================== 客服自身
@router.get("/agent/list", summary="客服列表与负载")
def agent_list(account: dict = Depends(_can_work)) -> dict[str, Any]:
    ensure_demo_agents()
    return {"agents": handoff.working_agents_summary(),
            "auth_enabled": config.AUTH_ENABLED,
            "roles": config.ROLE_LABELS}


@router.get("/agent/me", summary="当前客服身份与状态")
def agent_me(request: Request, account: dict = Depends(_can_work)) -> dict[str, Any]:
    agent = resolve_agent(request, account)
    return {"agent": agent, "account": account, "realtime": realtime.realtime.health()}


@router.put("/agent/status", summary="切换在线 / 忙碌 / 离线")
def set_status(
    payload: AgentStatusRequest,
    request: Request,
    account: dict = Depends(_can_work),
) -> dict[str, Any]:
    agent = resolve_agent(request, account)
    db.set_agent_status(agent["id"], payload.status)
    realtime.realtime.push_agent_status()
    return {"agent": db.get_agent(agent["id"]), "message": "状态已更新"}


# ==================================================================== 会话
@router.get("/agent/sessions", summary="会话列表（待接入 / 我的 / 全部 / 已结束）")
def agent_sessions(
    request: Request,
    scope: str = "pending",
    account: dict = Depends(_can_work),
) -> dict[str, Any]:
    agent = resolve_agent(request, account)
    if scope == "pending":
        sessions = db.list_sessions(status="queueing", limit=100)
    elif scope == "mine":
        sessions = db.list_sessions(status="agent", agent_id=agent["id"], limit=100)
    elif scope == "closed":
        sessions = db.list_sessions(status_in=("closed", "timeout"), limit=100)
    else:
        sessions = db.list_sessions(status_in=config.ACTIVE_STATUS, limit=100)

    items = []
    for s in sessions:
        messages = db.get_messages(s["id"])
        last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        items.append({
            **{k: s[k] for k in (
                "id", "visitor_id", "status", "status_label", "turn_count", "fallback_count",
                "last_question", "last_category", "handoff_reason", "handoff_detail",
                "queue_at", "assigned_at", "agent_id", "created_at", "updated_at",
                "csat_score", "resolved_by", "message_count",
            )},
            "last_message": last_user[:60],
            "waiting_seconds": int(time.time() - db.parse_ts(s.get("queue_at")))
            if s.get("queue_at") and s["status"] == "queueing" else 0,
        })
    _sweep_throttled()
    return {"sessions": items, "scope": scope, "agent": agent, "service": handoff.service_status()}


@router.get("/agent/sessions/{session_id}/context", summary="上下文摘要（转人工上下文同步）")
def session_context(session_id: str, account: dict = Depends(_can_work)) -> dict[str, Any]:
    if not db.get_session(session_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    summary = db.session_summary(session_id)
    summary["trigger_label"] = handoff.describe_trigger(
        (summary.get("handoff") or {}).get("trigger_type", "")
    )
    return summary


@router.get("/agent/sessions/{session_id}/messages", summary="会话消息（增量轮询）")
def session_messages(session_id: str, after: int = 0, account: dict = Depends(_can_work)) -> dict[str, Any]:
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"session_id": session_id, "status": session["status"],
            "status_label": session["status_label"],
            "messages": db.get_messages(session_id, after_id=after)}


@router.post("/agent/sessions/{session_id}/accept", summary="接入会话")
def accept(session_id: str, request: Request, account: dict = Depends(_can_work)) -> dict[str, Any]:
    agent = resolve_agent(request, account)
    if int(agent["current_load"]) >= int(agent["max_concurrent"]):
        raise HTTPException(
            status_code=400,
            detail=f"接待已达上限（{agent['current_load']}/{agent['max_concurrent']}），"
                   f"请先结束部分会话或切换为忙碌/离线",
        )
    session = handoff.accept_session(session_id, agent["id"])
    if not session:
        raise HTTPException(status_code=400, detail="会话不存在、已被接入或不在排队中")
    return {"session": session, "message": f"已接入，当前接待 {db.get_agent(agent['id'])['current_load']} 个会话"}


@router.post("/agent/sessions/{session_id}/reply", summary="发送客服消息")
def reply(
    session_id: str,
    payload: AgentReplyRequest,
    request: Request,
    account: dict = Depends(_can_work),
) -> dict[str, Any]:
    agent = resolve_agent(request, account)
    result = handoff.agent_reply(session_id, agent["id"], payload.content)
    if not result:
        raise HTTPException(status_code=400, detail="会话不存在或状态不允许回复")
    return {"message": "发送成功", **result}


@router.post("/agent/sessions/{session_id}/close", summary="结束会话并归因")
def close(session_id: str, request: Request, account: dict = Depends(_can_work)) -> dict[str, Any]:
    agent = resolve_agent(request, account)
    session = handoff.close_session(session_id, end_reason="agent", agent_id=agent["id"])
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    db.add_message(session_id, "system", "客服已结束本次会话。", answer_type="system")
    return {"session": db.get_session(session_id), "resolved_by": session.get("resolved_by")}


@router.get("/agent/visitors/{visitor_id}/sessions", summary="查看同一访客的历史会话")
def visitor_history(visitor_id: str, account: dict = Depends(_can_work)) -> dict[str, Any]:
    sessions = db.list_sessions(visitor_id=visitor_id, limit=20)
    return {"visitor_id": visitor_id, "sessions": sessions}


# ==================================================================== 常用语
@router.get("/canned-responses", summary="常用语列表")
def list_canned(request: Request, account: dict = Depends(_can_work)) -> dict[str, Any]:
    agent = resolve_agent(request, account)
    return {"items": db.list_canned(owner_id=agent["id"])}


@router.post("/canned-responses", summary="新增常用语")
def create_canned(
    payload: CannedIn, request: Request, account: dict = Depends(_can_work)
) -> dict[str, Any]:
    agent = resolve_agent(request, account)
    cid = db.create_canned(payload.title, payload.content, payload.scope, agent["id"])
    return {"id": cid, "message": "新增成功"}


@router.put("/canned-responses/{cid}", summary="更新常用语")
def update_canned(cid: int, payload: CannedUpdate, account: dict = Depends(_can_work)) -> dict[str, Any]:
    if not db.update_canned(cid, **payload.model_dump(exclude_unset=True)):
        raise HTTPException(status_code=404, detail="常用语不存在")
    return {"message": "更新成功"}


@router.delete("/canned-responses/{cid}", summary="删除常用语")
def delete_canned(cid: int, account: dict = Depends(_can_work)) -> dict[str, Any]:
    if not db.delete_canned(cid):
        raise HTTPException(status_code=404, detail="常用语不存在")
    return {"message": "删除成功"}


@router.post("/canned-responses/{cid}/use", summary="记录常用语使用次数")
def use_canned(cid: int, account: dict = Depends(_can_work)) -> dict[str, Any]:
    db.incr_canned_use(cid)
    return {"message": "ok"}
