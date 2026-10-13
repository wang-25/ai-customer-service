"""用户侧接口：会话、对话、满意度评价、结束会话。

对应 PRD：M1-1 会话接入、M1-2 会话生命周期、M2 对话引擎、M4 转人工入口、M6-2 满意度
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from .. import config, db, dialog, embedding, handoff, realtime
from ..schemas import ChatRequest, CloseSessionRequest, CsatRequest, SessionCreateRequest

router = APIRouter(tags=["用户侧"])


# ==================================================================== 基础
@router.get("/api/health", summary="健康检查")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "knowledge_count": db.count_knowledge(),
        "document_count": len(db.list_documents()),
        "llm_enabled": config.llm_configured(),
        "auth_enabled": config.AUTH_ENABLED,
        "thresholds": {
            "high_confidence": config.HIGH_CONFIDENCE,
            "mid_confidence": config.MID_CONFIDENCE,
        },
        "embedding": embedding.status(),
        "realtime": realtime.realtime.health(),
        "service": handoff.service_status(),
    }


@router.get("/api/bot/config", summary="机器人公开配置（供挂件渲染）")
def bot_public_config() -> dict[str, Any]:
    cfg = db.get_bot_config()
    return {
        "bot_name": cfg["bot_name"],
        "bot_avatar": cfg["bot_avatar"],
        "welcome": cfg["welcome"],
        "ending": cfg["ending"],
        "quick_questions": cfg["quick_questions"],
        "service": handoff.service_status(),
    }


# ==================================================================== 会话
@router.post("/api/session", summary="新建会话")
def create_session(payload: SessionCreateRequest | None = None) -> dict[str, Any]:
    visitor_id = payload.visitor_id if payload else ""
    session = db.create_session(visitor_id=visitor_id)
    cfg = db.get_bot_config()
    # 通知工作台有新会话进入
    realtime.realtime.to_agents({
        "type": "session_created", "session_id": session["id"],
        "status": session["status"], "visitor_id": visitor_id,
    })
    return {"session": session, "welcome": cfg["welcome"],
            "quick_questions": cfg["quick_questions"], "service": handoff.service_status()}


@router.get("/api/session/{session_id}", summary="查询会话状态")
def get_session(session_id: str) -> dict[str, Any]:
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    payload: dict[str, Any] = {"session": session}
    if session["status"] == "queueing":
        payload["queue_position"] = handoff.queue_position(session_id)
    if session["pending_slot"]:
        rule = next((r for r in config.SLOT_RULES if r["id"] == session["pending_slot"]), None)
        if rule:
            payload["clarify"] = dialog._clarify_payload(rule)
    return payload


@router.get("/api/session/{session_id}/messages", summary="拉取会话消息（支持增量轮询）")
def get_messages(session_id: str, after: int = 0) -> dict[str, Any]:
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {
        "session_id": session_id,
        "status": session["status"],
        "status_label": session["status_label"],
        "messages": db.attach_source_details(db.get_messages(session_id, after_id=after)),
        "csat_score": session.get("csat_score"),
        "pending_slot": session.get("pending_slot"),
        "queue_position": handoff.queue_position(session_id) if session["status"] == "queueing" else 0,
    }


@router.post("/api/session/{session_id}/close", summary="结束会话（用户主动）")
def close_session(session_id: str, payload: CloseSessionRequest) -> dict[str, Any]:
    if not db.get_session(session_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    session = db.finish_session(session_id, end_reason=payload.reason or "user")
    cfg = db.get_bot_config()
    csat_needed = session.get("csat_score") is None
    db.add_message(session_id, "system", cfg["ending"], answer_type="system")
    handoff.emit_status(session)
    handoff.emit_last_message(session_id)
    if csat_needed:
        realtime.realtime.push_csat_request(session_id)
    return {
        "session": session,
        "reply": cfg["ending"],
        "csat_requested": csat_needed,
    }


@router.post("/api/session/{session_id}/csat", summary="提交满意度评价")
def submit_csat(session_id: str, payload: CsatRequest) -> dict[str, Any]:
    if not db.get_session(session_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    ok = db.set_csat(session_id, payload.score, payload.reason, payload.comment)
    if not ok:
        return {"saved": False, "message": "该会话已评价过，感谢您的反馈"}
    handoff.emit_status(db.get_session(session_id))
    return {"saved": True, "message": "感谢您的评价！"}


# ==================================================================== 对话
@router.post("/api/chat", summary="发送消息并获取回复")
def chat(req: ChatRequest) -> dict[str, Any]:
    session_id = req.session_id
    if not session_id:
        session_id = db.create_session(visitor_id=req.visitor_id)["id"]
    elif not db.get_session(session_id):
        raise HTTPException(status_code=404, detail="会话不存在")

    try:
        result = dialog.handle_message(session_id, req.message, visitor_id=req.visitor_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    matched = result.get("matched") or {}
    sources = db.knowledge_by_ids(result.get("sources") or [])
    return {
        "session_id": session_id,
        "reply": result["reply"],
        "answer_type": result["answer_type"],
        "matched": matched or None,
        "suggestions": result.get("suggestions", []),
        "context_used": result.get("context_used", False),
        "sources": [{"id": s["id"], "question": s["question"], "source_ref": s["source_ref"]}
                    for s in sources],
        "clarify": result.get("clarify"),
        "slot_filled": result.get("slot_filled"),
        "handoff": result.get("handoff"),
        "turn": result.get("turn"),
        "fallback_count": result.get("fallback_count", 0),
        "session_status": result.get("session_status", "bot"),
        "csat_requested": result.get("csat_requested", False),
        "source": result.get("source", "kb"),
    }


@router.post("/api/session/{session_id}/handoff", summary="用户主动请求转人工")
def request_handoff(session_id: str) -> dict[str, Any]:
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session["status"] in ("agent", "queueing"):
        return {"handoff": True, "already": True, "reply": "", "session": session}
    marker_id = db.add_message(session_id, "user", "（请求转人工）")
    handoff.emit_last_message(session_id, marker_id)
    h = handoff.start_handoff(session_id, "user_request", "用户点击转人工按钮")
    msg_id = db.add_message(session_id, "bot", h["reply"], answer_type="handoff")
    handoff.emit_last_message(session_id, msg_id)
    handoff.emit_status(db.get_session(session_id))
    return {"handoff": True, "already": False, "reply": h["reply"], **h,
            "session": db.get_session(session_id)}
