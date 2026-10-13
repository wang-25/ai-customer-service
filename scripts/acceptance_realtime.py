"""WebSocket 实时推送验收自测。

验证「短轮询 → WebSocket 推送」是否真正生效：
    1. 用户端连接后立即收到 sync 快照
    2. 客户端 ping ↔ 服务端 pong
    3. 客服在工作台接入并回复 → 用户端**无需任何 HTTP 轮询**即可收到该消息
    4. 用户结束会话 → 用户端收到 status=closed 与 csat_request
    5. 客服端能收到 session_created / handoff / session_updated 事件
    6. 断开后重连能拿到完整增量（不丢消息）

用法：
    python run.py --port 8020
    python scripts/acceptance_realtime.py --base-url http://127.0.0.1:8020
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.request
from typing import Any

try:
    import websockets
except ImportError:  # pragma: no cover
    print("需要 websockets 库：pip install 'uvicorn[standard]' 或 pip install websockets")
    sys.exit(1)

BASE = "http://127.0.0.1:8020"
WS_BASE = "ws://127.0.0.1:8020"
results: list[tuple[str, bool]] = []


def call(method: str, path: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{BASE}{path}", data=data,
        headers={"Content-Type": "application/json"} if data else {}, method=method)
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode()
    return json.loads(raw) if raw.strip().startswith(("{", "[")) else {"raw": raw}


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok))
    print(f"{'✅ PASS' if ok else '❌ FAIL'}  {name}" + (f"\n          {detail}" if detail else ""))


async def recv_until(ws, predicate, timeout: float = 8.0) -> dict | None:
    """在超时时间内等待满足条件的消息。"""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remain = deadline - asyncio.get_event_loop().time()
        if remain <= 0:
            return None
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remain)
        except asyncio.TimeoutError:
            return None
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if msg.get("type") == "ping":       # 心跳直接忽略
            continue
        if predicate(msg):
            return msg


async def main() -> int:
    global BASE, WS_BASE
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=BASE)
    args = parser.parse_args()
    BASE = args.base_url.rstrip("/")
    WS_BASE = BASE.replace("https://", "wss://").replace("http://", "ws://")

    print("=" * 80)
    print(f"WebSocket 实时推送验收  ->  {WS_BASE}")
    print("=" * 80)

    health = call("GET", "/api/health")
    rt = health.get("realtime", {})
    if not rt.get("enabled"):
        print("❌ 服务端未启用 WebSocket（WS_ENABLED=0）")
        return 1
    check("服务端 WebSocket 已启用", rt.get("enabled") is True,
          f"心跳 {rt.get('heartbeat_seconds')}s")

    call("GET", "/api/agent/list")          # 确保演示客服存在
    # 确保至少有一位客服在线，否则转人工会走「非服务时间」分支
    for agent in call("GET", "/api/agent/list")["agents"]:
        if agent["status"] != "online":
            call("PUT", f"/api/agent/status?agent_id={agent['id']}", {"status": "online"})
    session = call("POST", "/api/session", {"visitor_id": "ws-acceptance"})["session"]
    sid = session["id"]

    # ---------------------------------------------------------- 用户端连接
    async with websockets.connect(f"{WS_BASE}/ws/chat/{sid}") as visitor:
        first = json.loads(await asyncio.wait_for(visitor.recv(), timeout=5))
        check("用户端连接后立即收到 sync 快照",
              first.get("type") == "sync" and first.get("session_id") == sid,
              f"status={first.get('status')} messages={len(first.get('messages') or [])}")

        await visitor.send(json.dumps({"type": "ping"}))
        pong = await recv_until(visitor, lambda m: m.get("type") == "pong", timeout=5)
        check("客户端 ping ↔ 服务端 pong", pong is not None, "心跳链路正常")

        # 用户提问（HTTP），自己的消息必须回显 + 机器人回复推送
        call("POST", "/api/chat", {"session_id": sid, "message": "我想退货，怎么操作"})
        own_msg = await recv_until(visitor, lambda m: m.get("type") == "message"
                                   and m["message"]["role"] == "user", timeout=8)
        check("访客自己的消息回显到访客通道（否则页面上看不到自己的记录）",
              own_msg is not None and own_msg["message"]["content"] == "我想退货，怎么操作",
              f"role=user id={own_msg['message']['id'] if own_msg else '未收到'}")
        bot_msg = await recv_until(visitor, lambda m: m.get("type") == "message"
                                   and m["message"]["role"] == "bot", timeout=8)
        check("机器人回复通过 WebSocket 推送（不依赖轮询）",
              bot_msg is not None and "退货" in bot_msg["message"]["content"],
              (bot_msg or {}).get("message", {}).get("content", "")[:42])

        # ------------------------------------------------------ 客服接入 + 回复
        # 注意：必须对本连接的会话 sid 触发转人工，否则收不到它的状态事件
        agents = call("GET", "/api/agent/list")["agents"]
        aid = agents[0]["id"]
        call("POST", f"/api/session/{sid}/handoff")

        status_evt = await recv_until(
            visitor, lambda m: m.get("type") == "status"
            and m.get("status") in ("agent", "queueing"), timeout=8)
        check("转人工后用户端实时收到状态变更",
              status_evt is not None,
              f"status={status_evt.get('status_label') if status_evt else '未收到'}")

        try:
            call("POST", f"/api/agent/sessions/{sid}/accept?agent_id={aid}")
        except Exception:  # noqa: BLE001 - 已被自动分配，属正常
            pass

        # 客服回复 → 用户端应通过 WS 收到 agent 消息
        call("POST", f"/api/agent/sessions/{sid}/reply?agent_id={aid}",
             {"content": "您好，这里是人工客服，退货我来帮您处理。"})
        agent_msg = await recv_until(
            visitor, lambda m: m.get("type") == "message"
            and m["message"]["role"] == "agent", timeout=8)
        check("客服消息实时推送到用户端（关键验收点）",
              agent_msg is not None and "人工客服" in agent_msg["message"]["content"],
              (agent_msg or {}).get("message", {}).get("content", "")[:44])
        if agent_msg:
            check("推送消息带完整字段（含答案溯源字段）",
                  "id" in agent_msg["message"] and "source_details" in agent_msg["message"],
                  f"message.id={agent_msg['message']['id']} answer_type="
                  f"{agent_msg['message'].get('answer_type')}")

        # 系统消息：客服接入时会写入一条 system 消息
        call("POST", f"/api/agent/sessions/{sid}/close?agent_id={aid}")
        closed_evt = await recv_until(
            visitor, lambda m: m.get("type") == "status" and m.get("status") == "closed",
            timeout=8)
        check("会话结束状态实时推送", closed_evt is not None,
              f"status={closed_evt.get('status') if closed_evt else '未收到'}")

        csat_evt = await recv_until(visitor, lambda m: m.get("type") == "csat_request", timeout=5)
        check("结束会话后触发满意度评价邀请", csat_evt is not None, "csat_request 事件已推送")

    # ---------------------------------------------------------- 重连补数据
    async with websockets.connect(f"{WS_BASE}/ws/chat/{sid}") as again:
        snap = json.loads(await asyncio.wait_for(again.recv(), timeout=5))
        check("断开重连后自动补齐完整会话（含离线期间的消息）",
              snap.get("type") == "sync" and len(snap.get("messages") or []) >= 4,
              f"重连拿到 {len(snap.get('messages') or [])} 条消息，状态={snap.get('status_label')}")
        await again.send(json.dumps({"type": "sync", "after": 0}))
        manual = await recv_until(again, lambda m: m.get("type") == "sync", timeout=5)
        check("客户端可主动请求增量同步（after=0 全量）",
              manual is not None and len(manual.get("messages") or []) >= 4,
              f"{len((manual or {}).get('messages') or [])} 条")

    # ---------------------------------------------------------- 客服端通道
    async with websockets.connect(f"{WS_BASE}/ws/agent") as agent_ws:
        snap = json.loads(await asyncio.wait_for(agent_ws.recv(), timeout=5))
        check("客服端连接后收到服务状态与坐席负载快照",
              snap.get("type") == "sync" and "service" in snap and "agents" in snap,
              f"在线客服 {len(snap.get('agents') or [])} 位，"
              f"服务{'在线' if snap['service'].get('service_online') else '离线'}")

        new_sid = call("POST", "/api/session", {"visitor_id": "ws-agent-check"})["session"]["id"]
        created = await recv_until(
            agent_ws, lambda m: m.get("type") == "session_created"
            and m.get("session_id") == new_sid, timeout=8)
        check("新会话创建实时通知工作台", created is not None,
              f"session_id={new_sid[:8]}")

        # 访客在页面发言 → 工作台应实时收到 message 事件（此前不会推送）
        call("POST", "/api/chat", {"session_id": new_sid, "message": "在吗？我想问个问题"})
        visitor_msg = await recv_until(
            agent_ws, lambda m: m.get("type") == "message"
            and m.get("session_id") == new_sid and m.get("role") == "user", timeout=8)
        check("访客发言实时通知工作台（此前该事件不推送）",
              visitor_msg is not None,
              "工作台无需轮询即可看到客户开口")

        call("POST", f"/api/session/{new_sid}/handoff")
        h = await recv_until(
            agent_ws, lambda m: m.get("type") == "handoff" and m.get("session_id") == new_sid,
            timeout=8)
        check("转人工事件实时通知工作台（含触发原因）",
              h is not None, f"trigger={h.get('trigger_type') if h else '未收到'} "
                             f"assigned={h.get('assigned') if h else '—'}")

        call("POST", f"/api/agent/sessions/{new_sid}/reply?agent_id={aid}",
             {"content": "收到，我来看一下。"})
        msg_evt = await recv_until(
            agent_ws, lambda m: m.get("type") == "message" and m.get("session_id") == new_sid,
            timeout=8)
        check("客服端能收到会话新消息事件", msg_evt is not None, "用于列表刷新与红点提示")

    # ---------------------------------------------------------- 非服务时间分支
    for agent in call("GET", "/api/agent/list")["agents"]:
        call("PUT", f"/api/agent/status?agent_id={agent['id']}", {"status": "offline"})
    off_sid = call("POST", "/api/session", {"visitor_id": "ws-offline"})["session"]["id"]
    async with websockets.connect(f"{WS_BASE}/ws/chat/{off_sid}") as off_ws:
        await asyncio.wait_for(off_ws.recv(), timeout=5)          # sync
        call("POST", f"/api/session/{off_sid}/handoff")
        off_evt = await recv_until(
            off_ws, lambda m: m.get("type") == "handoff" and m.get("offline") is True, timeout=8)
        check("无人在线时转人工也会实时推送离线提示（此前该分支不推送）",
              off_evt is not None,
              "offline=True，用户端立即看到「当前非人工服务时间」")
    for agent in call("GET", "/api/agent/list")["agents"]:
        call("PUT", f"/api/agent/status?agent_id={agent['id']}", {"status": "online"})

    # ---------------------------------------------------------- 汇总
    final = call("GET", "/api/health")["realtime"]
    print()
    print(f"实时通道统计：累计推送 {final['events_sent']} 条事件，"
          f"发送失败 {final['send_errors']} 次，当前用户连接 {final['visitor_connections']}、"
          f"客服连接 {final['agent_connections']}")
    passed = sum(1 for _, ok in results if ok)
    print("=" * 80)
    print(f"结果：{passed}/{len(results)} 项通过")
    failed = [n for n, ok in results if not ok]
    if failed:
        print("未通过：")
        for n in failed:
            print(f"  - {n}")
    print("=" * 80)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
