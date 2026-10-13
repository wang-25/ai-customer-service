"""WebSocket 实时推送。

设计要点：
1. 两条通道：
     /ws/chat/{session_id}  用户端 —— 接收客服消息、系统消息、会话状态变化、评价邀请
     /ws/agent?agent_id=N   客服工作台 —— 接收新排队会话、会话分配、新消息、客服状态变化
2. 路由处理器是同步函数（FastAPI 会放到线程池执行），因此广播通过
   `asyncio.run_coroutine_threadsafe` 投递回主事件循环，不需要把整条链路改成 async。
3. 服务端每 WS_HEARTBEAT_SECONDS 秒发一次 ping 保活；客户端断开自动清理。
4. 任何广播失败都不会影响业务主流程（消息已经落库，前端重连后会重新同步）。
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from typing import Any

from fastapi import WebSocket

from . import config


class Realtime:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.visitors: dict[str, set[WebSocket]] = defaultdict(set)
        self.agents: set[WebSocket] = set()
        # 每个连接一把锁：心跳广播与业务广播可能并发写同一个 WebSocket
        self._locks: dict[int, asyncio.Lock] = {}
        self.stats = {"visitor_conns": 0, "agent_conns": 0,
                      "events_sent": 0, "send_errors": 0, "started_at": time.time()}

    def _lock_for(self, ws: WebSocket) -> asyncio.Lock:
        lock = self._locks.get(id(ws))
        if lock is None:
            lock = asyncio.Lock()
            self._locks[id(ws)] = lock
        return lock

    def _drop_lock(self, ws: WebSocket) -> None:
        self._locks.pop(id(ws), None)

    # ------------------------------------------------------------ 生命周期
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    async def _send(self, ws: WebSocket, payload: dict[str, Any]) -> bool:
        try:
            async with self._lock_for(ws):
                await ws.send_text(json.dumps(payload, ensure_ascii=False))
            self.stats["events_sent"] += 1
            return True
        except Exception:  # noqa: BLE001 - 连接已断开
            self.stats["send_errors"] += 1
            return False

    def _schedule(self, coro) -> None:
        """把协程投递到主事件循环；不在事件循环内或未启用时直接丢弃。"""
        if not config.WS_ENABLED or self.loop is None or self.loop.is_closed():
            coro.close()
            return
        try:
            asyncio.run_coroutine_threadsafe(coro, self.loop)
        except RuntimeError:
            coro.close()

    async def send_to(self, ws: WebSocket, payload: dict[str, Any]) -> bool:
        """带锁的单连接发送（连接建立时的首帧也走这里，避免与广播并发写）。"""
        return await self._send(ws, payload)

    # ------------------------------------------------------------ 连接管理
    async def connect_visitor(self, session_id: str, ws: WebSocket) -> None:
        await ws.accept()
        self.visitors[session_id].add(ws)
        self.stats["visitor_conns"] = sum(len(v) for v in self.visitors.values())

    def disconnect_visitor(self, session_id: str, ws: WebSocket) -> None:
        self.visitors.get(session_id, set()).discard(ws)
        if session_id in self.visitors and not self.visitors[session_id]:
            self.visitors.pop(session_id, None)
        self._drop_lock(ws)
        self.stats["visitor_conns"] = sum(len(v) for v in self.visitors.values())

    async def connect_agent(self, ws: WebSocket) -> None:
        await ws.accept()
        self.agents.add(ws)
        self.stats["agent_conns"] = len(self.agents)

    def disconnect_agent(self, ws: WebSocket) -> None:
        self.agents.discard(ws)
        self._drop_lock(ws)
        self.stats["agent_conns"] = len(self.agents)

    # ------------------------------------------------------------ 广播
    async def _broadcast(self, conns: list[WebSocket], payload: dict[str, Any],
                         drop: Any = None) -> None:
        dead = [ws for ws in list(conns) if not await self._send(ws, payload)]
        for ws in dead:
            if drop:
                drop(ws)

    def to_session(self, session_id: str, payload: dict[str, Any]) -> None:
        conns = list(self.visitors.get(session_id, set()))
        if not conns:
            return
        self._schedule(self._broadcast(
            conns, payload, drop=lambda ws: self.disconnect_visitor(session_id, ws)))

    def to_agents(self, payload: dict[str, Any]) -> None:
        if not self.agents:
            return
        self._schedule(self._broadcast(list(self.agents), payload,
                                       drop=self.disconnect_agent))

    # ------------------------------------------------------------ 业务事件
    def push_message(self, session_id: str, message: dict[str, Any]) -> None:
        """推一条消息给该会话的用户端。"""
        self.to_session(session_id, {"type": "message", "message": message})

    def push_status(self, session: dict[str, Any]) -> None:
        if not session:
            return
        self.to_session(session["id"], {
            "type": "status",
            "status": session.get("status"),
            "status_label": session.get("status_label"),
            "agent_id": session.get("agent_id"),
            "handoff_reason": session.get("handoff_reason"),
            "csat_score": session.get("csat_score"),
        })
        self.to_agents({"type": "session_updated", "session_id": session["id"],
                        "status": session.get("status"),
                        "status_label": session.get("status_label"),
                        "agent_id": session.get("agent_id")})

    def push_handoff(self, session_id: str, result: dict[str, Any]) -> None:
        """转人工结果（在线分配 / 排队 / 离线）同时通知用户端与工作台。"""
        self.to_session(session_id, {"type": "handoff", **result})
        self.to_agents({"type": "handoff", "session_id": session_id, **result})

    def push_csat_request(self, session_id: str) -> None:
        self.to_session(session_id, {"type": "csat_request"})

    def push_agent_status(self) -> None:
        self.to_agents({"type": "agent_status_changed"})

    def health(self) -> dict[str, Any]:
        return {
            "enabled": config.WS_ENABLED,
            "visitor_connections": self.stats["visitor_conns"],
            "agent_connections": self.stats["agent_conns"],
            "events_sent": self.stats["events_sent"],
            "send_errors": self.stats["send_errors"],
            "heartbeat_seconds": config.WS_HEARTBEAT_SECONDS,
        }

    # ------------------------------------------------------------ 心跳
    async def heartbeat_forever(self) -> None:
        """周期性 ping，保持连接活跃并清理死连接。"""
        while True:
            await asyncio.sleep(max(5, config.WS_HEARTBEAT_SECONDS))
            payload = {"type": "ping", "ts": int(time.time())}
            for session_id in list(self.visitors.keys()):
                conns = list(self.visitors.get(session_id, set()))
                await self._broadcast(
                    conns, payload, drop=lambda ws, sid=session_id: self.disconnect_visitor(sid, ws))
            await self._broadcast(list(self.agents), payload, drop=self.disconnect_agent)


realtime = Realtime()
