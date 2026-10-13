"""数据看板聚合（PRD M6-3：解决率 / 转人工率看板）。

口径全部在此文件定义并随接口返回，避免团队各人理解不一致。
不使用物化表，直接实时聚合（MVP 数据量下 10 万级会话仍在毫秒级）。
"""
from __future__ import annotations

import time
from typing import Any

from . import db


def _range(days: int) -> tuple[str, str]:
    until = db.now()
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - days * 86400))
    return since, until


def _scalar(conn, sql: str, args: tuple = ()) -> float:
    row = conn.execute(sql, args).fetchone()
    value = row[0] if row else 0
    return float(value or 0)


def overview(days: int = 7) -> dict[str, Any]:
    since, until = _range(days)
    args = (since, until)

    with db.connect() as conn:
        total = _scalar(conn, "SELECT COUNT(*) FROM chat_session WHERE created_at BETWEEN ? AND ?", args)
        closed = _scalar(
            conn,
            "SELECT COUNT(*) FROM chat_session WHERE created_at BETWEEN ? AND ?"
            " AND status IN ('closed','timeout')",
            args,
        )
        resolved_bot = _scalar(
            conn,
            "SELECT COUNT(*) FROM chat_session WHERE created_at BETWEEN ? AND ? AND resolved_by = 'bot'",
            args,
        )
        resolved_agent = _scalar(
            conn,
            "SELECT COUNT(*) FROM chat_session WHERE created_at BETWEEN ? AND ? AND resolved_by = 'agent'",
            args,
        )
        unresolved = _scalar(
            conn,
            "SELECT COUNT(*) FROM chat_session WHERE created_at BETWEEN ? AND ? AND resolved_by = 'unresolved'",
            args,
        )
        handoff = _scalar(
            conn,
            "SELECT COUNT(*) FROM chat_session WHERE created_at BETWEEN ? AND ?"
            " AND handoff_reason IS NOT NULL",
            args,
        )
        csat_good = _scalar(
            conn,
            "SELECT COUNT(*) FROM chat_session WHERE created_at BETWEEN ? AND ? AND csat_score = 1",
            args,
        )
        csat_bad = _scalar(
            conn,
            "SELECT COUNT(*) FROM chat_session WHERE created_at BETWEEN ? AND ? AND csat_score = -1",
            args,
        )
        csat_total = csat_good + csat_bad
        fallback_times = _scalar(
            conn,
            "SELECT COUNT(*) FROM fallback_log WHERE created_at BETWEEN ? AND ?",
            args,
        )
        avg_turns = _scalar(
            conn,
            "SELECT AVG(turn_count) FROM chat_session WHERE created_at BETWEEN ? AND ?",
            args,
        )
        avg_first_reply = _scalar(
            conn,
            "SELECT AVG((julianday(m.first_at) - julianday(s.created_at)) * 86400)"
            " FROM chat_session s JOIN ("
            "   SELECT session_id, MIN(created_at) AS first_at FROM chat_message"
            "   WHERE role = 'bot' GROUP BY session_id) m ON m.session_id = s.id"
            " WHERE s.created_at BETWEEN ? AND ?",
            args,
        )
        avg_agent_first_reply = _scalar(
            conn,
            "SELECT AVG((julianday(first_reply_at) - julianday(queue_at)) * 86400)"
            " FROM chat_session WHERE created_at BETWEEN ? AND ?"
            " AND queue_at IS NOT NULL AND first_reply_at IS NOT NULL",
            args,
        )
        avg_queue = _scalar(
            conn,
            "SELECT AVG((julianday(assigned_at) - julianday(queue_at)) * 86400)"
            " FROM chat_session WHERE created_at BETWEEN ? AND ?"
            " AND queue_at IS NOT NULL AND assigned_at IS NOT NULL",
            args,
        )
        daily = [
            dict(r)
            for r in conn.execute(
                "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS sessions,"
                " SUM(CASE WHEN resolved_by = 'bot' THEN 1 ELSE 0 END) AS bot_resolved,"
                " SUM(CASE WHEN handoff_reason IS NOT NULL THEN 1 ELSE 0 END) AS handoff"
                " FROM chat_session WHERE created_at BETWEEN ? AND ?"
                " GROUP BY day ORDER BY day",
                args,
            ).fetchall()
        ]
        intents = [
            dict(r)
            for r in conn.execute(
                "SELECT kb_category AS category, COUNT(*) AS times FROM chat_message"
                " WHERE kb_category IS NOT NULL AND role = 'bot' AND created_at BETWEEN ? AND ?"
                " GROUP BY kb_category ORDER BY times DESC",
                args,
            ).fetchall()
        ]
        handoff_reasons = [
            dict(r)
            for r in conn.execute(
                "SELECT trigger_type, COUNT(*) AS times FROM handoff_log"
                " WHERE created_at BETWEEN ? AND ? GROUP BY trigger_type ORDER BY times DESC",
                args,
            ).fetchall()
        ]

    top_unresolved = db.top_unresolved_questions(limit=10, since=since)

    def pct(a: float, b: float) -> float:
        return round(a / b * 100, 1) if b else 0.0

    return {
        "range": {"since": since, "until": until, "days": days},
        "totals": {
            "sessions": int(total),
            "closed": int(closed),
            "active": int(total - closed),
            "fallback_times": int(fallback_times),
            "avg_turns": round(avg_turns, 2),
        },
        "resolve": {
            "resolved_by_bot": int(resolved_bot),
            "resolved_by_agent": int(resolved_agent),
            "unresolved": int(unresolved),
            # 分母用「已完成会话」：未结束的会话不计入解决率
            "containment_rate": pct(resolved_bot, closed),
            "handoff_sessions": int(handoff),
            "handoff_rate": pct(handoff, total),
        },
        "csat": {
            "good": int(csat_good),
            "bad": int(csat_bad),
            "total": int(csat_total),
            "good_rate": pct(csat_good, csat_total),
            "collect_rate": pct(csat_total, total),
        },
        "performance": {
            "avg_bot_first_reply_seconds": round(avg_first_reply, 2),
            "avg_queue_seconds": round(avg_queue, 2),
            "avg_agent_first_reply_seconds": round(avg_agent_first_reply, 2),
        },
        "daily": daily,
        "intent_distribution": intents,
        "handoff_reasons": handoff_reasons,
        "top_unresolved": top_unresolved,
        "metric_notes": {
            "containment_rate": "机器人独立解决率 = resolved_by='bot' 的会话 ÷ 期间已完成会话数（未结束的会话不计入分母）",
            "handoff_rate": "转人工率 = 存在转人工记录的会话 ÷ 期间会话总数",
            "csat_good_rate": "好评率 = 好评数 ÷ 参评总数",
            "csat_collect_rate": "好评回收率 = 参评会话数 ÷ 期间会话总数（衡量评价触达效果）",
            "avg_bot_first_reply_seconds": "机器人首次响应 = 首条机器人消息时间 − 会话创建时间",
            "avg_queue_seconds": "平均排队时长 = 分配时间 − 进入队列时间（仅统计已分配的会话）",
            "avg_agent_first_reply_seconds": "客服首次响应 = 客服首条回复时间 − 进入队列时间（仅统计已分配的会话）",
            "sessions": "会话总数 = 期间内创建的全部会话（含进行中与已结束）",
            "closed": "已完成会话 = 状态为 closed / timeout 的会话",
            "active": "进行中会话 = 会话总数 − 已完成会话",
            "avg_turns": "平均轮次 = 期间会话的平均用户发言轮次（AVG(turn_count)）",
            "fallback_times": "兜底次数 = 期间内进入兜底话术的日志条数（fallback_log）",
        },
    }


def session_detail(session_id: str) -> dict[str, Any]:
    """会话完整详情：消息流 + 溯源 + 转人工记录 + 评价（M6-1）。"""
    session = db.get_session(session_id)
    if not session:
        return {}
    messages = db.attach_source_details(db.get_messages(session_id))
    for m in messages:
        m["agent"] = None
        if m.get("agent_id"):
            agent = db.get_agent(m["agent_id"])
            m["agent"] = agent["name"] if agent else None
    return {
        "session": session,
        "messages": messages,
        "handoff": db.list_handoff(session_id),
        "agent": db.get_agent(session["agent_id"]) if session.get("agent_id") else None,
    }
