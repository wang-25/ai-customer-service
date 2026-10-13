"""WebSocket 路由：用户端会话通道 + 客服工作台通道。

客户端协议（文本 JSON）：

用户端 `/ws/chat/{session_id}`
    上行：{"type":"ping"}                      → 下行 {"type":"pong"}
          {"type":"sync","after":<last_id>}    → 下行 {"type":"sync", messages, status...}
          {"type":"status"}                    → 下行 {"type":"status", ...}
    下行：message / status / handoff / csat_request / ping / pong / sync

客服端 `/ws/agent?agent_id=N`
    上行：{"type":"ping"} / {"type":"status"}
    下行：message / session_updated / handoff / agent_status_changed / ping / pong

连接建立时服务端会主动下发一次 `sync`，用于刷新前端状态（重连后不会丢状态）。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .. import config, db, handoff, realtime

router = APIRouter(tags=["实时通道"])


def _visitor_sync_payload(session_id: str, after: int = 0) -> dict:
    session = db.get_session(session_id)
    if not session:
        return {"type": "error", "message": "会话不存在"}
    messages = db.attach_source_details(db.get_messages(session_id, after_id=after))
    payload = {
        "type": "sync",
        "session_id": session_id,
        "status": session["status"],
        "status_label": session["status_label"],
        "messages": messages,
        "csat_score": session.get("csat_score"),
        "handoff_reason": session.get("handoff_reason"),
        "agent_id": session.get("agent_id"),
    }
    if session["status"] == "queueing":
        payload["queue_position"] = handoff.queue_position(session_id)
    rule = (next((r for r in config.SLOT_RULES if r["id"] == session["pending_slot"]), None)
            if session.get("pending_slot") else None)
    if rule:
        payload["clarify"] = {
            "slot_id": rule["id"], "slot_name": rule["name"],
            "question": rule["question"],
            "options": [{"label": o["label"]} for o in rule["options"]],
        }
    return payload


@router.websocket("/ws/chat/{session_id}")
async def chat_socket(websocket: WebSocket, session_id: str) -> None:
    if not config.WS_ENABLED:
        await websocket.close(code=1013)      # try again later
        return
    if not db.get_session(session_id):
        await websocket.close(code=1008)      # policy violation
        return

    await realtime.realtime.connect_visitor(session_id, websocket)
    try:
        # 首帧也走带锁的发送，避免与广播并发写同一个连接
        await realtime.realtime.send_to(websocket, _visitor_sync_payload(session_id))
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            kind = msg.get("type")
            if kind == "ping":
                await realtime.realtime.send_to(websocket, {"type": "pong"})
            elif kind == "sync":
                await realtime.realtime.send_to(
                    websocket, _visitor_sync_payload(session_id, int(msg.get("after") or 0)))
            elif kind == "status":
                session = db.get_session(session_id)
                if session:
                    await realtime.realtime.send_to(websocket, {
                        "type": "status", "status": session["status"],
                        "status_label": session["status_label"],
                        "agent_id": session.get("agent_id"),
                    })
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 - 任何异常都不应影响其他连接
        pass
    finally:
        realtime.realtime.disconnect_visitor(session_id, websocket)


@router.websocket("/ws/agent")
async def agent_socket(websocket: WebSocket) -> None:
    if not config.WS_ENABLED:
        await websocket.close(code=1013)
        return

    await realtime.realtime.connect_agent(websocket)
    try:
        def agent_snapshot() -> dict:
            return {
                "type": "sync",
                "service": handoff.service_status(),
                "agents": handoff.working_agents_summary(),
            }

        await realtime.realtime.send_to(websocket, agent_snapshot())
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if msg.get("type") == "ping":
                await realtime.realtime.send_to(websocket, {"type": "pong"})
            elif msg.get("type") == "sync":
                await realtime.realtime.send_to(websocket, agent_snapshot())
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        realtime.realtime.disconnect_agent(websocket)
