"""转人工服务：在线时段判定、触发规则、排队与分配、会话生命周期流转。

对应 PRD：
    M4-1 用户主动转人工
    M4-2 规则触发转人工
    M4-3 转人工上下文同步（摘要见 db.session_summary）
    M4-4 人工在线时段与服务状态提示
    M1-2 会话生命周期管理（状态机 / 超时 / 结束归因）
"""
from __future__ import annotations

import time
from typing import Any

from . import config, db, knowledge


# ==================================================================== 实时推送辅助
# 所有推送都包一层 try/except：实时通道故障绝不能影响业务主流程
def emit_status(session: dict[str, Any] | None) -> None:
    if not session:
        return
    try:
        from . import realtime

        realtime.realtime.push_status(session)
    except Exception:  # noqa: BLE001
        pass


def emit_last_message(session_id: str, message_id: int | None = None) -> None:
    """把刚写入的消息推给用户端（用于客服消息、系统消息）。"""
    try:
        from . import realtime

        messages = db.get_messages(session_id)
        if not messages:
            return
        target = None
        if message_id is not None:
            target = next((m for m in messages if m["id"] == message_id), None)
        if target is None:
            target = messages[-1]
        enriched = db.attach_source_details([target])[0]
        realtime.realtime.push_message(session_id, enriched)
        realtime.realtime.to_agents({"type": "message", "session_id": session_id})
    except Exception:  # noqa: BLE001
        pass


def emit_handoff(session_id: str, result: dict[str, Any]) -> None:
    try:
        from . import realtime

        push = {k: v for k, v in result.items() if k != "reply"}
        realtime.realtime.push_handoff(session_id, push)
    except Exception:  # noqa: BLE001
        pass


def emit_csat_request(session_id: str) -> None:
    """会话结束时邀请评价（无论由用户还是客服结束）。"""
    try:
        from . import realtime

        realtime.realtime.push_csat_request(session_id)
    except Exception:  # noqa: BLE001
        pass


# ==================================================================== 服务状态
def service_status() -> dict[str, Any]:
    cfg = db.get_bot_config()
    online = db.is_service_online(cfg.get("service_hours"))
    agents = db.list_agents(online_only=True)
    queueing = db.list_sessions(status="queueing", limit=200)
    return {
        "service_online": online,
        "service_hours_text": cfg.get("service_hours_text", ""),
        "online_agents": len(agents),
        "queue_count": len(queueing),
        "accepting": online and len(agents) > 0,
    }


def queue_position(session_id: str) -> int:
    sessions = db.list_sessions(status="queueing", limit=500)
    ids = [s["id"] for s in sorted(sessions, key=lambda x: x.get("queue_at") or "")]
    return ids.index(session_id) + 1 if session_id in ids else 0


# ==================================================================== 触发规则
def sentiment_hits(text: str) -> list[str]:
    return [w for w in config.SENTIMENT_WORDS if w in text]


def risky_hits(text: str) -> list[str]:
    return [w for w in config.RISKY_WORDS if w in text]


def check_trigger_rules(
    session: dict[str, Any], message: str, next_fallback_count: int = 0
) -> dict[str, Any] | None:
    """按优先级依次检查转人工触发规则，返回第一个命中的规则。"""
    rules = db.get_trigger_thresholds()

    def rule(name: str) -> dict[str, Any] | None:
        r = rules.get(name)
        return r if r and r["enabled"] else None

    r = rule("risky_keyword")
    if r:
        hits = risky_hits(message)
        if len(hits) >= int(r["threshold"]):
            return {"rule_type": "risky_keyword", "detail": "命中高风险词：" + "、".join(hits)}

    r = rule("sentiment")
    if r:
        hits = sentiment_hits(message)
        if len(hits) >= int(r["threshold"]):
            return {"rule_type": "sentiment", "detail": "命中负面情绪词：" + "、".join(hits)}

    r = rule("fallback_count")
    if r and next_fallback_count >= int(r["threshold"]):
        return {"rule_type": "fallback_count",
                "detail": f"连续 {next_fallback_count} 次未匹配到答案"}

    r = rule("turn_limit")
    if r and int(session.get("turn_count") or 0) + 1 > int(r["threshold"]):
        return {"rule_type": "turn_limit",
                "detail": f"会话轮次已超过 {r['threshold']} 轮仍未解决"}

    r = rule("clarify_limit")
    if r and int(session.get("clarify_round") or 0) > int(r["threshold"]):
        return {"rule_type": "clarify_limit",
                "detail": f"澄清已超过 {r['threshold']} 轮仍无法作答"}

    return None


# ==================================================================== 分配
def assign_session(session_id: str, agent_id: int) -> dict[str, Any] | None:
    agent = db.get_agent(agent_id)
    session = db.get_session(session_id)
    if not agent or not session:
        return None
    if session.get("agent_id") == agent_id and session["status"] == "agent":
        return session
    # 接待上限保护：超过 max_concurrent 不再分配（PRD M5-1 分配策略）
    if int(agent["current_load"]) >= int(agent["max_concurrent"]):
        return None
    db.update_session(session_id, status="agent", agent_id=agent_id, assigned_at=db.now())
    db.mark_handoff_assigned(session_id, agent_id)
    db.adjust_agent_load(agent_id, 1)
    db.add_message(
        session_id, "system",
        f"客服「{agent['name']}」已接入会话。",
        answer_type="system",
    )
    refreshed = db.get_agent(agent_id) or {}
    if int(refreshed.get("current_load") or 0) >= int(refreshed.get("max_concurrent") or 3):
        db.set_agent_status(agent_id, "busy")
    updated = db.get_session(session_id)
    emit_status(updated)
    emit_last_message(session_id)
    return updated


def auto_assign(session_id: str) -> dict[str, Any] | None:
    agent = db.pick_agent_for_assign()
    if not agent:
        return None
    return assign_session(session_id, agent["id"])


def start_handoff(
    session_id: str,
    trigger_type: str,
    detail: str = "",
    prefix_reply: str = "",
) -> dict[str, Any]:
    """把会话切换到转人工流程：在线则排队并尝试自动分配，离线则提示服务时段。"""
    cfg = db.get_bot_config()
    status = service_status()
    session = db.get_session(session_id)
    if not session:
        raise ValueError("会话不存在")

    db.add_handoff(session_id, trigger_type, detail)

    if not status["accepting"]:
        # 人工离线 / 无人在线：不进入排队，给出离线提示（PRD M4-4）
        reply = prefix_reply + cfg["handoff_offline"].format(hours=status["service_hours_text"])
        db.update_session(session_id, status="bot", handoff_reason=trigger_type,
                          handoff_detail=detail, last_user_at=db.now())
        result = {
            "handoff": True,
            "assigned": False,
            "offline": True,
            "reply": reply,
            "trigger_type": trigger_type,
            "detail": detail,
        }
        # 离线分支同样要推送：用户端需要立刻看到"当前非服务时间"的提示
        emit_status(db.get_session(session_id))
        emit_handoff(session_id, result)
        return result

    db.update_session(session_id, status="queueing", queue_at=db.now(),
                      handoff_reason=trigger_type, handoff_detail=detail)

    assigned_session = auto_assign(session_id)
    if assigned_session:
        agent = db.get_agent(assigned_session.get("agent_id") or 0) or {}
        reply = prefix_reply + f"已为您接入人工客服「{agent.get('name', '')}」，请描述您的问题。"
        result = {
            "handoff": True, "assigned": True, "offline": False, "reply": reply,
            "trigger_type": trigger_type, "detail": detail,
            "agent_id": agent.get("id"), "agent_name": agent.get("name", ""),
        }
        emit_handoff(session_id, result)
        return result

    position = queue_position(session_id)
    reply = prefix_reply + cfg["handoff"] + f"（当前排在第 {position} 位，客服稍后接入）"
    result = {
        "handoff": True, "assigned": False, "offline": False, "reply": reply,
        "trigger_type": trigger_type, "detail": detail, "queue_position": position,
    }
    emit_status(db.get_session(session_id))
    emit_handoff(session_id, result)
    return result


def accept_session(session_id: str, agent_id: int) -> dict[str, Any] | None:
    """客服工作台手动接入。"""
    session = db.get_session(session_id)
    if not session or session["status"] not in ("queueing", "bot"):
        return None
    return assign_session(session_id, agent_id)


def agent_reply(session_id: str, agent_id: int, content: str) -> dict[str, Any] | None:
    session = db.get_session(session_id)
    if not session:
        return None
    if session["status"] != "agent":
        # 客服直接回复未接入的会话 → 自动认领
        session = accept_session(session_id, agent_id) or session
    first = not session.get("first_reply_at")
    msg_id = db.add_message(session_id, "agent", content, answer_type="agent", agent_id=agent_id)
    fields: dict[str, Any] = {"last_user_at": db.now()}
    if first:
        fields["first_reply_at"] = db.now()
    db.update_session(session_id, **fields)
    emit_last_message(session_id, msg_id)
    emit_status(db.get_session(session_id))
    return {"message_id": msg_id, "first_reply": first}


def close_session(session_id: str, end_reason: str = "agent", agent_id: int | None = None) -> dict[str, Any] | None:
    session = db.get_session(session_id)
    if not session:
        return None
    closed = db.finish_session(session_id, end_reason=end_reason)
    emit_status(closed)
    if closed and closed.get("csat_score") is None:
        emit_csat_request(session_id)
    return closed


def sweep_timeouts() -> list[dict[str, Any]]:
    """定时任务：结束超时会话，并给排队超时的会话打标记。"""
    closed = db.scan_timeouts()
    for session in closed:
        db.add_message(
            session["id"], "system",
            "会话因长时间无消息已自动结束，如需继续咨询请重新发起。",
            answer_type="system",
        )
        emit_status(db.get_session(session["id"]))
        emit_last_message(session["id"])
    return closed


def working_agents_summary() -> list[dict[str, Any]]:
    agents = db.list_agents()
    for a in agents:
        a["status_label"] = {"online": "在线", "busy": "忙碌", "offline": "离线"}.get(
            a["status"], a["status"]
        )
        a["active_sessions"] = len(
            db.list_sessions(status="agent", agent_id=a["id"], limit=100)
        )
    return agents


# ==================================================================== 说明性工具
def describe_trigger(trigger_type: str) -> str:
    mapping = {
        "user_request": "用户主动要求转人工",
        "fallback_count": "连续兜底未解决",
        "sentiment": "负面情绪触发",
        "risky_keyword": "高风险关键词触发",
        "turn_limit": "会话轮次超限",
        "clarify_limit": "澄清轮次超限",
        "agent_timeout": "客服长时间未回复",
    }
    return mapping.get(trigger_type, trigger_type)


def elapsed_text(since: str | None) -> str:
    seconds = int(time.time() - db.parse_ts(since))
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分钟"
    return f"{seconds // 3600} 小时"


def matched_category_of(session_id: str) -> str:
    """取会话中最近一次命中的知识分类。"""
    for m in reversed(db.get_messages(session_id)):
        if m.get("kb_category"):
            return m["kb_category"]
    return ""


def normalize_text(text: str) -> str:
    return knowledge.normalize(text)
