"""SQLite 数据访问层（P0 全量）。

设计要点：
1. 每次操作打开一个短连接，保证「后台改完知识库立刻生效」，无需缓存刷新。
2. 建表 + 轻量迁移（老库自动补列），删库不必要。
3. 不依赖任何 ORM，使用标准库 sqlite3。

表清单：
    knowledge / documents            知识库与文档导入
    chat_session / chat_message      会话与消息
    handoff_log / fallback_log       转人工记录、未解决问题记录
    agent / agent_status_log         客服与在线状态
    canned_response                  常用语
    bot_config / trigger_rule        机器人配置与触发规则
    account                          账号与角色
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from . import config

# ==================================================================== 表结构
SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    question    TEXT    NOT NULL,
    answer      TEXT    NOT NULL,
    category    TEXT    NOT NULL DEFAULT '未分类',
    similar     TEXT    NOT NULL DEFAULT '[]',
    keywords    TEXT    NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    doc_id      INTEGER,
    chunk_index INTEGER,
    source_ref  TEXT    NOT NULL DEFAULT '手工录入',
    slot        TEXT,
    embedding   TEXT,
    embedding_provider TEXT,
    embedding_hash     TEXT,
    embedded_at TEXT,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT    NOT NULL,
    source_name TEXT    NOT NULL DEFAULT '',
    source_type TEXT    NOT NULL DEFAULT 'text',
    status      TEXT    NOT NULL DEFAULT 'draft',
    chunk_count INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_session (
    id             TEXT PRIMARY KEY,
    visitor_id     TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'bot',
    turn_count     INTEGER NOT NULL DEFAULT 0,
    fallback_count INTEGER NOT NULL DEFAULT 0,
    clarify_round  INTEGER NOT NULL DEFAULT 0,
    last_kb_id     INTEGER,
    last_question  TEXT,
    last_category  TEXT,
    pending_slot   TEXT,
    slots          TEXT NOT NULL DEFAULT '{}',
    handoff_reason TEXT,
    handoff_detail TEXT,
    queue_at       TEXT,
    assigned_at    TEXT,
    first_bot_at   TEXT,
    first_reply_at TEXT,
    ended_at       TEXT,
    end_reason     TEXT,
    resolved_by    TEXT,
    agent_id       INTEGER,
    csat_score     INTEGER,
    csat_reason    TEXT,
    csat_comment   TEXT,
    csat_at        TEXT,
    last_user_at   TEXT
);

CREATE TABLE IF NOT EXISTS chat_message (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    role         TEXT NOT NULL,
    content      TEXT NOT NULL,
    kb_id        INTEGER,
    kb_category  TEXT,
    score        REAL,
    answer_type  TEXT,
    context_used INTEGER NOT NULL DEFAULT 0,
    sources      TEXT NOT NULL DEFAULT '[]',
    agent_id     INTEGER,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS handoff_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     TEXT NOT NULL,
    trigger_type   TEXT NOT NULL,
    trigger_detail TEXT NOT NULL DEFAULT '',
    queue_at       TEXT,
    assigned_at    TEXT,
    agent_id       INTEGER,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fallback_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    query      TEXT NOT NULL,
    handled    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    account_id     INTEGER,
    status         TEXT NOT NULL DEFAULT 'offline',
    current_load   INTEGER NOT NULL DEFAULT 0,
    max_concurrent INTEGER NOT NULL DEFAULT 3,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_status_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id   INTEGER NOT NULL,
    from_status TEXT,
    to_status  TEXT NOT NULL,
    changed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS canned_response (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    content    TEXT NOT NULL,
    scope      TEXT NOT NULL DEFAULT 'team',
    owner_id   INTEGER,
    use_count  INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_config (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trigger_rule (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_type  TEXT NOT NULL UNIQUE,
    label      TEXT NOT NULL DEFAULT '',
    threshold  INTEGER NOT NULL DEFAULT 1,
    enabled    INTEGER NOT NULL DEFAULT 1,
    priority   INTEGER NOT NULL DEFAULT 99,
    desc       TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    salt          TEXT NOT NULL,
    name          TEXT NOT NULL DEFAULT '',
    role          TEXT NOT NULL DEFAULT 'agent',
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_message_session ON chat_message(session_id, id);
CREATE INDEX IF NOT EXISTS idx_knowledge_enabled ON knowledge(enabled);
CREATE INDEX IF NOT EXISTS idx_session_status ON chat_session(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_session_visitor ON chat_session(visitor_id);
CREATE INDEX IF NOT EXISTS idx_fallback_session ON fallback_log(session_id);
"""

# 老库自动补列（列名 -> 列定义）
_MIGRATIONS: dict[str, dict[str, str]] = {
    "knowledge": {
        "doc_id": "INTEGER", "chunk_index": "INTEGER",
        "source_ref": "TEXT NOT NULL DEFAULT '手工录入'", "slot": "TEXT",
        "embedding": "TEXT", "embedding_provider": "TEXT",
        "embedding_hash": "TEXT", "embedded_at": "TEXT",
    },
    "chat_session": {
        "status": "TEXT NOT NULL DEFAULT 'bot'", "clarify_round": "INTEGER NOT NULL DEFAULT 0",
        "pending_slot": "TEXT", "slots": "TEXT NOT NULL DEFAULT '{}'",
        "handoff_reason": "TEXT", "handoff_detail": "TEXT", "queue_at": "TEXT",
        "assigned_at": "TEXT", "first_bot_at": "TEXT", "first_reply_at": "TEXT",
        "ended_at": "TEXT", "end_reason": "TEXT", "resolved_by": "TEXT",
        "agent_id": "INTEGER", "csat_score": "INTEGER", "csat_reason": "TEXT",
        "csat_comment": "TEXT", "csat_at": "TEXT", "last_user_at": "TEXT",
        "visitor_id": "TEXT NOT NULL DEFAULT ''",
    },
    "chat_message": {
        "kb_category": "TEXT", "sources": "TEXT NOT NULL DEFAULT '[]'", "agent_id": "INTEGER",
    },
}


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def ts_ago(seconds: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - seconds))


def parse_ts(value: str | None) -> float:
    """把 'YYYY-MM-DD HH:MM:SS' 转成时间戳，失败返回 0。"""
    if not value:
        return 0.0
    try:
        return time.mktime(time.strptime(value[:19], "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return 0.0


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_columns(conn: sqlite3.Connection) -> None:
    for table, columns in _MIGRATIONS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if not existing:
            continue
        for col, ddl in columns.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def init_db(seed: bool = True) -> int:
    """建表 + 迁移 + 首次灌入种子数据，返回知识条目数。"""
    with connect() as conn:
        conn.executescript(SCHEMA)
        _ensure_columns(conn)
    _ensure_config()
    _ensure_accounts()
    _ensure_demo_agents()
    _ensure_canned()
    count = count_knowledge()
    if seed and count == 0:
        from .seed_data import SEED_KNOWLEDGE

        for item in SEED_KNOWLEDGE:
            insert_knowledge(item)
        count = count_knowledge()
    return count


# ==================================================================== 知识库
def _row_to_knowledge(row: sqlite3.Row) -> dict[str, Any]:
    keys = row.keys()
    return {
        "id": row["id"],
        "question": row["question"],
        "answer": row["answer"],
        "category": row["category"],
        "similar": json.loads(row["similar"] or "[]"),
        "keywords": [k for k in (row["keywords"] or "").split(",") if k.strip()],
        "enabled": bool(row["enabled"]),
        "doc_id": row["doc_id"],
        "chunk_index": row["chunk_index"],
        "source_ref": row["source_ref"] or "手工录入",
        "slot": row["slot"],
        # 向量本体不通过列表接口返回（体积大且对前端无意义），只给出索引状态
        "has_embedding": bool(row["embedding"]) if "embedding" in keys else False,
        "embedded_at": row["embedded_at"] if "embedded_at" in keys else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_knowledge(
    q: str = "", category: str = "", enabled: int | None = None, doc_id: int | None = None
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM knowledge WHERE 1=1"
    args: list[Any] = []
    if q:
        sql += " AND (question LIKE ? OR answer LIKE ? OR similar LIKE ? OR keywords LIKE ?)"
        like = f"%{q}%"
        args += [like, like, like, like]
    if category:
        sql += " AND category = ?"
        args.append(category)
    if enabled is not None:
        sql += " AND enabled = ?"
        args.append(int(enabled))
    if doc_id is not None:
        sql += " AND doc_id = ?"
        args.append(doc_id)
    sql += " ORDER BY category, id"
    with connect() as conn:
        return [_row_to_knowledge(r) for r in conn.execute(sql, args).fetchall()]


def get_knowledge(kid: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM knowledge WHERE id = ?", (kid,)).fetchone()
    return _row_to_knowledge(row) if row else None


def knowledge_by_ids(ids: list[int]) -> list[dict[str, Any]]:
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    with connect() as conn:
        rows = conn.execute(f"SELECT * FROM knowledge WHERE id IN ({marks})", ids).fetchall()
    return [_row_to_knowledge(r) for r in rows]


def _norm_keywords(keywords: Any) -> list[str]:
    if isinstance(keywords, str):
        return [k.strip() for k in keywords.replace("，", ",").split(",") if k.strip()]
    return [str(k).strip() for k in (keywords or []) if str(k).strip()]


def insert_knowledge(item: dict[str, Any]) -> int:
    ts = now()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO knowledge (question, answer, category, similar, keywords, enabled,"
            " doc_id, chunk_index, source_ref, slot, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(item["question"]).strip(),
                str(item["answer"]).strip(),
                (item.get("category") or "未分类").strip(),
                json.dumps(item.get("similar") or [], ensure_ascii=False),
                ",".join(_norm_keywords(item.get("keywords"))),
                1 if item.get("enabled", True) else 0,
                item.get("doc_id"),
                item.get("chunk_index"),
                item.get("source_ref") or "手工录入",
                item.get("slot"),
                ts,
                ts,
            ),
        )
        kid = int(cur.lastrowid)
    _mark_embedding_dirty()
    return kid


def update_knowledge(kid: int, item: dict[str, Any]) -> bool:
    old = get_knowledge(kid)
    if not old:
        return False
    kwargs = {k: v for k, v in item.items() if v is not None}
    merged = {**old, **kwargs}
    # slot 允许显式传 null 清空（其他字段为 None 表示「不修改」）
    if "slot" in item:
        merged["slot"] = item["slot"]
    with connect() as conn:
        conn.execute(
            "UPDATE knowledge SET question=?, answer=?, category=?, similar=?, keywords=?,"
            " enabled=?, slot=?, updated_at=? WHERE id=?",
            (
                str(merged["question"]).strip(),
                str(merged["answer"]).strip(),
                (merged.get("category") or "未分类").strip(),
                json.dumps(merged.get("similar") or [], ensure_ascii=False),
                ",".join(_norm_keywords(merged.get("keywords"))),
                1 if merged.get("enabled", True) else 0,
                merged.get("slot"),
                now(),
                kid,
            ),
        )
    _mark_embedding_dirty()
    return True


def delete_knowledge(kid: int) -> bool:
    with connect() as conn:
        ok = conn.execute("DELETE FROM knowledge WHERE id = ?", (kid,)).rowcount > 0
    if ok:
        _mark_embedding_dirty()
    return ok


def count_knowledge(doc_id: int | None = None) -> int:
    sql, args = "SELECT COUNT(*) FROM knowledge", ()
    if doc_id is not None:
        sql, args = "SELECT COUNT(*) FROM knowledge WHERE doc_id = ?", (doc_id,)
    with connect() as conn:
        return int(conn.execute(sql, args).fetchone()[0])


def list_categories() -> list[str]:
    with connect() as conn:
        rows = conn.execute("SELECT DISTINCT category FROM knowledge ORDER BY category").fetchall()
    return [r["category"] for r in rows]


def clear_knowledge() -> None:
    with connect() as conn:
        conn.execute("DELETE FROM knowledge")
        conn.execute("DELETE FROM documents")
    _mark_embedding_dirty()


# ==================================================================== 向量索引（Embedding）
def _mark_embedding_dirty() -> None:
    """知识库变化后标记向量索引需要重建（延迟导入避免循环依赖）。"""
    try:
        from . import embedding

        embedding.mark_dirty()
    except Exception:  # noqa: BLE001 - 向量索引失败不能影响知识库写入
        pass


def save_embeddings(rows: list[tuple[int, str, str, str]], provider: str = "") -> int:
    """rows: [(knowledge_id, vector_json, provider, content_hash)]"""
    if not rows:
        return 0
    ts = now()
    with connect() as conn:
        conn.executemany(
            "UPDATE knowledge SET embedding = ?, embedding_provider = ?,"
            " embedding_hash = ?, embedded_at = ? WHERE id = ?",
            [(vec, prov or provider, h, ts, kid) for kid, vec, prov, h in rows],
        )
    return len(rows)


def embedding_map() -> dict[int, str]:
    """返回 {knowledge_id: 向量 JSON}，供检索一次性加载，避免污染列表接口。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, embedding FROM knowledge WHERE embedding IS NOT NULL AND enabled = 1"
        ).fetchall()
    return {r["id"]: r["embedding"] for r in rows}


def embedding_of(kid: int) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT embedding FROM knowledge WHERE id = ?", (kid,)).fetchone()
    return row["embedding"] if row else None


def embedding_hash_of(kid: int) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT embedding_hash FROM knowledge WHERE id = ?", (kid,)).fetchone()
    return row["embedding_hash"] if row else None


def embedding_provider_of(kid: int) -> str | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT embedding_provider FROM knowledge WHERE id = ?", (kid,)).fetchone()
    return row["embedding_provider"] if row else None


def embedding_stats() -> dict[str, Any]:
    with connect() as conn:
        total = int(conn.execute(
            "SELECT COUNT(*) FROM knowledge WHERE enabled = 1").fetchone()[0])
        indexed = int(conn.execute(
            "SELECT COUNT(*) FROM knowledge WHERE enabled = 1 AND embedding IS NOT NULL"
        ).fetchone()[0])
        fresh = int(conn.execute(
            "SELECT COUNT(*) FROM knowledge WHERE enabled = 1 AND embedding IS NOT NULL"
            " AND embedded_at IS NOT NULL").fetchone()[0])
        last = conn.execute("SELECT MAX(embedded_at) FROM knowledge").fetchone()[0]
    return {"total": total, "indexed": indexed, "fresh": fresh,
            "stale": max(0, total - indexed), "built_at": last or ""}


# ==================================================================== 文档导入（M3-2）
def create_document(title: str, source_name: str, source_type: str, status: str = "draft") -> int:
    ts = now()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO documents (title, source_name, source_type, status, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (title, source_name, source_type, status, ts, ts),
        )
        return int(cur.lastrowid)


def update_document(doc_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE documents SET {cols} WHERE id = ?", (*fields.values(), doc_id))


def get_document(doc_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if not row:
            return None
        doc = dict(row)
        doc["published_count"] = int(
            conn.execute(
                "SELECT COUNT(*) FROM knowledge WHERE doc_id = ? AND enabled = 1 AND source_ref != '手工录入'",
                (doc_id,),
            ).fetchone()[0]
        )
    return doc


def list_documents() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM documents ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def list_document_chunks(doc_id: int) -> list[dict[str, Any]]:
    return list_knowledge(doc_id=doc_id)


def delete_document(doc_id: int, delete_chunks: bool = True) -> bool:
    with connect() as conn:
        if delete_chunks:
            conn.execute("DELETE FROM knowledge WHERE doc_id = ?", (doc_id,))
        ok = conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,)).rowcount > 0
    if ok:
        _mark_embedding_dirty()
    return ok


def publish_document(doc_id: int, enabled: bool = True) -> int:
    """把该文档的所有草稿切片发布（置为可检索）。返回受影响条数。"""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE knowledge SET enabled = ?, updated_at = ? WHERE doc_id = ?",
            (1 if enabled else 0, now(), doc_id),
        )
        count = cur.rowcount
    update_document(doc_id, status="published" if enabled else "disabled",
                    chunk_count=count_knowledge(doc_id))
    _mark_embedding_dirty()
    return count


# ==================================================================== 会话（M1-2）
def create_session(visitor_id: str = "") -> dict[str, Any]:
    sid = uuid.uuid4().hex[:16]
    ts = now()
    with connect() as conn:
        conn.execute(
            "INSERT INTO chat_session (id, visitor_id, created_at, updated_at, status, last_user_at)"
            " VALUES (?,?,?,?,?,?)",
            (sid, visitor_id or "", ts, ts, "bot", ts),
        )
    return get_session(sid)  # type: ignore[return-value]


def get_session(sid: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM chat_session WHERE id = ?", (sid,)).fetchone()
    if not row:
        return None
    session = dict(row)
    try:
        session["slots"] = json.loads(session.get("slots") or "{}")
    except (TypeError, ValueError):
        session["slots"] = {}
    session["status_label"] = config.SESSION_STATUS.get(session["status"], session["status"])
    session["service_online"] = is_service_online()
    return session


def update_session(sid: str, **fields: Any) -> None:
    if not fields:
        return
    if "slots" in fields and not isinstance(fields["slots"], str):
        fields["slots"] = json.dumps(fields["slots"], ensure_ascii=False)
    fields["updated_at"] = now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE chat_session SET {cols} WHERE id = ?", (*fields.values(), sid))


SESSION_FILTER_FIELDS = (
    "id", "visitor_id", "status", "turn_count", "fallback_count", "clarify_round",
    "last_question", "last_category", "pending_slot", "handoff_reason", "handoff_detail",
    "queue_at", "assigned_at", "ended_at", "end_reason", "resolved_by", "agent_id",
    "csat_score", "csat_reason", "csat_comment", "created_at", "updated_at",
)


def _session_where(
    status: str = "",
    status_in: tuple[str, ...] | None = None,
    agent_id: int | None = None,
    visitor_id: str = "",
    handoff: bool | None = None,
    csat: int | None = None,
    since: str = "",
    until: str = "",
    keyword: str = "",
    category: str = "",
) -> tuple[str, list[Any]]:
    """构造会话筛选条件，列表与计数共用，保证「命中条数」与列表一致。"""
    sql = " WHERE 1=1"
    args: list[Any] = []
    if status:
        sql += " AND status = ?"
        args.append(status)
    if status_in:
        sql += f" AND status IN ({','.join('?' * len(status_in))})"
        args += list(status_in)
    if agent_id is not None:
        sql += " AND agent_id = ?"
        args.append(agent_id)
    if visitor_id:
        sql += " AND visitor_id = ?"
        args.append(visitor_id)
    if handoff is True:
        sql += " AND handoff_reason IS NOT NULL"
    if handoff is False:
        sql += " AND handoff_reason IS NULL"
    if csat is not None:
        if csat == 0:
            sql += " AND csat_score = -1"
        else:
            sql += " AND csat_score = ?"
            args.append(csat)
    if since:
        sql += " AND created_at >= ?"
        args.append(since)
    if until:
        sql += " AND created_at <= ?"
        args.append(until)
    if category:
        sql += (" AND (last_category = ? OR id IN"
                " (SELECT session_id FROM chat_message WHERE kb_category = ?))")
        args += [category, category]
    if keyword:
        sql += (" AND (id IN (SELECT session_id FROM chat_message WHERE content LIKE ?)"
                " OR last_question LIKE ?)")
        like = f"%{keyword}%"
        args += [like, like]
    return sql, args


def list_sessions(
    status: str = "",
    status_in: tuple[str, ...] | None = None,
    agent_id: int | None = None,
    visitor_id: str = "",
    handoff: bool | None = None,
    csat: int | None = None,
    since: str = "",
    until: str = "",
    keyword: str = "",
    category: str = "",
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    where, args = _session_where(status, status_in, agent_id, visitor_id, handoff, csat,
                                 since, until, keyword, category)
    sql = f"SELECT * FROM chat_session{where} ORDER BY updated_at DESC LIMIT ? OFFSET ?"
    args = [*args, limit, offset]
    with connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["status_label"] = config.SESSION_STATUS.get(item["status"], item["status"])
        item["message_count"] = count_messages(item["id"])
        try:
            item["slots"] = json.loads(item.get("slots") or "{}")
        except (TypeError, ValueError):
            item["slots"] = {}
        result.append(item)
    return result


def count_sessions(
    status: str = "",
    status_in: tuple[str, ...] | None = None,
    agent_id: int | None = None,
    visitor_id: str = "",
    handoff: bool | None = None,
    csat: int | None = None,
    since: str = "",
    until: str = "",
    keyword: str = "",
    category: str = "",
) -> int:
    """按与 list_sessions 完全相同的条件计数。"""
    where, args = _session_where(status, status_in, agent_id, visitor_id, handoff, csat,
                                 since, until, keyword, category)
    with connect() as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM chat_session{where}", args).fetchone()[0])


def scan_timeouts() -> list[dict[str, Any]]:
    """扫描超时会话：活跃会话idle超阈值 → 自动结束（超时关闭）。返回被关闭的会话。"""
    limit_ts = ts_ago(config.SESSION_TIMEOUT_SECONDS)
    closed: list[dict[str, Any]] = []
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM chat_session WHERE status IN ('bot','queueing','agent')"
            " AND COALESCE(last_user_at, updated_at) < ?",
            (limit_ts,),
        ).fetchall()
    for row in rows:
        session = dict(row)
        finish_session(session["id"], end_reason="timeout")
        closed.append(session)
    return closed


def finish_session(sid: str, end_reason: str = "user") -> dict[str, Any] | None:
    """结束会话并按 PRD 口径做归因（resolved_by）。"""
    session = get_session(sid)
    if not session:
        return None
    if session["status"] in ("closed", "timeout"):
        return session

    messages = get_messages(sid)
    agent_replied = any(m["role"] == "agent" for m in messages)
    bot_matched = any(m["role"] == "bot" and m["kb_id"] for m in messages)
    if agent_replied:
        resolved = "agent"
    elif bot_matched:
        resolved = "bot"
    else:
        resolved = "unresolved"

    status = "timeout" if end_reason == "timeout" else "closed"
    update_session(sid, status=status, ended_at=now(), end_reason=end_reason, resolved_by=resolved)
    # 释放客服负载
    if session.get("agent_id"):
        adjust_agent_load(session["agent_id"], -1)
        update_session(sid, agent_id=session["agent_id"])
    return get_session(sid)


def set_csat(sid: str, score: int, reason: str = "", comment: str = "") -> bool:
    """满意度评价（M6-2）。同一会话只记一次。"""
    session = get_session(sid)
    if not session:
        return False
    if session.get("csat_at"):
        return False
    update_session(sid, csat_score=score, csat_reason=reason or "",
                   csat_comment=comment or "", csat_at=now())
    return True


# ==================================================================== 消息
def add_message(
    session_id: str,
    role: str,
    content: str,
    kb_id: int | None = None,
    kb_category: str | None = None,
    score: float | None = None,
    answer_type: str | None = None,
    context_used: bool = False,
    sources: list[int] | None = None,
    agent_id: int | None = None,
) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO chat_message (session_id, role, content, kb_id, kb_category, score,"
            " answer_type, context_used, sources, agent_id, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                session_id, role, content, kb_id, kb_category, score, answer_type,
                1 if context_used else 0,
                json.dumps(sources or [], ensure_ascii=False),
                agent_id, now(),
            ),
        )
        return int(cur.lastrowid)


def _row_to_message(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    try:
        item["sources"] = json.loads(item.get("sources") or "[]")
    except (TypeError, ValueError):
        item["sources"] = []
    item["context_used"] = bool(item.get("context_used"))
    return item


def get_messages(session_id: str, after_id: int = 0, limit: int = 500) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM chat_message WHERE session_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
            (session_id, after_id, limit),
        ).fetchall()
    return [_row_to_message(r) for r in rows]


def get_recent_user_messages(session_id: str, limit: int = 10) -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT content FROM chat_message WHERE session_id = ? AND role = 'user'"
            " ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    return [r["content"] for r in reversed(rows)]


def count_messages(session_id: str) -> int:
    with connect() as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM chat_message WHERE session_id = ?", (session_id,)
            ).fetchone()[0]
        )


def attach_source_details(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """给消息补上答案溯源信息（PRD M3-3：回答可查率 100%）。"""
    ids: set[int] = set()
    for m in messages:
        if m.get("kb_id"):
            ids.add(m["kb_id"])
        for kid in m.get("sources") or []:
            ids.add(kid)
    items = {k["id"]: k for k in knowledge_by_ids(sorted(ids))}
    for m in messages:
        source_ids = m.get("sources") or ([m["kb_id"]] if m.get("kb_id") else [])
        m["source_details"] = [
            {
                "id": kid,
                "question": items.get(kid, {}).get("question", f"#{kid}"),
                "source_ref": items.get(kid, {}).get("source_ref", ""),
                "category": items.get(kid, {}).get("category", ""),
            }
            for kid in source_ids
        ]
    return messages


def session_summary(session_id: str) -> dict[str, Any]:
    """客服工作台用的上下文摘要卡片（M4-3 转人工上下文同步）。"""
    session = get_session(session_id)
    if not session:
        return {}
    messages = get_messages(session_id)
    sources: list[dict[str, Any]] = []
    seen: set[int] = set()
    for m in messages:
        for kid in m.get("sources") or []:
            if kid not in seen:
                seen.add(kid)
    if seen:
        sources = [
            {"id": k["id"], "question": k["question"], "category": k["category"]}
            for k in knowledge_by_ids(sorted(seen))
        ]
    handoffs = list_handoff(session_id)
    # 转人工时机器人可能没有命中任何条目（如高风险词直接转接），
    # 此时用客户第一句话作为意图摘要，避免客服端看到「未识别」
    intent = session.get("last_question")
    if not intent:
        first_user = next((m["content"] for m in messages if m["role"] == "user"), "")
        intent = (first_user[:30] + "…") if len(first_user) > 30 else first_user
    return {
        "session": session,
        "message_count": len(messages),
        "intent": intent or "（未识别）",
        "category": session.get("last_category") or "（未识别）",
        "slots": session.get("slots") or {},
        "pending_slot": session.get("pending_slot"),
        "handoff": handoffs[-1] if handoffs else None,
        "referenced_knowledge": sources,
        "waiting_seconds": int(time.time() - parse_ts(session.get("queue_at"))) if session.get("queue_at") else 0,
    }


# ==================================================================== 转人工 / 未解决
def add_handoff(session_id: str, trigger_type: str, trigger_detail: str = "") -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO handoff_log (session_id, trigger_type, trigger_detail, queue_at, created_at)"
            " VALUES (?,?,?,?,?)",
            (session_id, trigger_type, trigger_detail, now(), now()),
        )
        return int(cur.lastrowid)


def mark_handoff_assigned(session_id: str, agent_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE handoff_log SET assigned_at = ?, agent_id = ? WHERE session_id = ?"
            " AND assigned_at IS NULL",
            (now(), agent_id, session_id),
        )


def list_handoff(session_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM handoff_log WHERE session_id = ? ORDER BY id ASC", (session_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def add_fallback(session_id: str, query: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO fallback_log (session_id, query, created_at) VALUES (?,?,?)",
            (session_id, query, now()),
        )
        return int(cur.lastrowid)


def list_fallbacks(limit: int = 50, handled: int | None = None, since: str = "") -> list[dict[str, Any]]:
    sql = "SELECT * FROM fallback_log WHERE 1=1"
    args: list[Any] = []
    if handled is not None:
        sql += " AND handled = ?"
        args.append(handled)
    if since:
        sql += " AND created_at >= ?"
        args.append(since)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def top_unresolved_questions(limit: int = 10, since: str = "") -> list[dict[str, Any]]:
    sql = ("SELECT query, COUNT(*) AS times, MAX(created_at) AS last_at FROM fallback_log"
           " WHERE 1=1")
    args: list[Any] = []
    if since:
        sql += " AND created_at >= ?"
        args.append(since)
    sql += " GROUP BY query ORDER BY times DESC, last_at DESC LIMIT ?"
    args.append(limit)
    with connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def mark_fallback_handled(fid: int, handled: int = 1) -> bool:
    with connect() as conn:
        return conn.execute(
            "UPDATE fallback_log SET handled = ? WHERE id = ?", (handled, fid)
        ).rowcount > 0


# ==================================================================== 客服（M5）
def create_agent(name: str, account_id: int | None = None, status: str = "offline") -> int:
    ts = now()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO agent (name, account_id, status, created_at, updated_at) VALUES (?,?,?,?,?)",
            (name, account_id, status, ts, ts),
        )
        return int(cur.lastrowid)


def list_agents(online_only: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM agent"
    if online_only:
        sql += " WHERE status IN ('online','busy')"
    # 在线优先、负载小的优先，保证工作台默认选中的是一个可接待的客服
    sql += (" ORDER BY CASE status WHEN 'online' THEN 0 WHEN 'busy' THEN 1 ELSE 2 END,"
            " current_load, id")
    with connect() as conn:
        rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def get_agent(agent_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM agent WHERE id = ?", (agent_id,)).fetchone()
    return dict(row) if row else None


def get_agent_by_account(account_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM agent WHERE account_id = ?", (account_id,)).fetchone()
    return dict(row) if row else None


def update_agent(agent_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE agent SET {cols} WHERE id = ?", (*fields.values(), agent_id))


def set_agent_status(agent_id: int, status: str) -> None:
    agent = get_agent(agent_id)
    if not agent or agent["status"] == status:
        return
    with connect() as conn:
        conn.execute(
            "INSERT INTO agent_status_log (agent_id, from_status, to_status, changed_at) VALUES (?,?,?,?)",
            (agent_id, agent["status"], status, now()),
        )
    update_agent(agent_id, status=status)


def adjust_agent_load(agent_id: int, delta: int) -> None:
    agent = get_agent(agent_id)
    if not agent:
        return
    load = max(0, int(agent["current_load"]) + delta)
    update_agent(agent_id, current_load=load)


def pick_agent_for_assign() -> dict[str, Any] | None:
    """空闲优先：在线且负载未满的客服中，选当前负载最小、最早创建的。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM agent WHERE status = 'online' AND current_load < max_concurrent"
            " ORDER BY current_load ASC, id ASC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def online_agent_count() -> int:
    with connect() as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM agent WHERE status IN ('online','busy')"
            ).fetchone()[0]
        )


def is_service_online(hours: dict | None = None) -> bool:
    """是否处于人工服务时间（M4-4）。"""
    cfg = get_bot_config()
    hours = hours or cfg.get("service_hours") or {}
    # Python 的 tm_wday: 周一=0 … 周日=6；配置里 0=周日 … 6=周六，需要转换
    weekday = time.localtime().tm_wday
    key = str((weekday + 1) % 7)
    ranges = hours.get(key) or []
    current = time.strftime("%H:%M")
    for rng in ranges:
        try:
            start, end = rng[0], rng[1]
        except (IndexError, TypeError):
            continue
        if start <= current <= end:
            return True
    return False


# ==================================================================== 常用语（M5-3）
def list_canned(owner_id: int | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM canned_response WHERE scope = 'team'"
    args: list[Any] = []
    if owner_id is not None:
        sql += " OR owner_id = ?"
        args.append(owner_id)
    sql += " ORDER BY scope DESC, use_count DESC, id DESC"
    with connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def create_canned(title: str, content: str, scope: str = "team", owner_id: int | None = None) -> int:
    ts = now()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO canned_response (title, content, scope, owner_id, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (title, content, scope, owner_id, ts, ts),
        )
        return int(cur.lastrowid)


def update_canned(cid: int, **fields: Any) -> bool:
    if not fields:
        return False
    fields["updated_at"] = now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        return conn.execute(
            f"UPDATE canned_response SET {cols} WHERE id = ?", (*fields.values(), cid)
        ).rowcount > 0


def delete_canned(cid: int) -> bool:
    with connect() as conn:
        return conn.execute("DELETE FROM canned_response WHERE id = ?", (cid,)).rowcount > 0


def incr_canned_use(cid: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE canned_response SET use_count = use_count + 1 WHERE id = ?", (cid,))


# ==================================================================== 机器人配置（M7-1）
def get_bot_config() -> dict[str, Any]:
    cfg = json.loads(json.dumps(config.DEFAULT_BOT_CONFIG))
    with connect() as conn:
        row = conn.execute("SELECT value FROM bot_config WHERE key = 'bot'").fetchone()
    if row:
        try:
            stored = json.loads(row["value"])
            if isinstance(stored, dict):
                cfg.update(stored)
        except (TypeError, ValueError):
            pass
    return cfg


def update_bot_config(patch: dict[str, Any]) -> dict[str, Any]:
    cfg = get_bot_config()
    for key, value in (patch or {}).items():
        if key in config.DEFAULT_BOT_CONFIG and value is not None:
            cfg[key] = value
    with connect() as conn:
        conn.execute(
            "INSERT INTO bot_config (key, value, updated_at) VALUES ('bot', ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (json.dumps(cfg, ensure_ascii=False), now()),
        )
    return cfg


def _ensure_config() -> None:
    with connect() as conn:
        has_bot = conn.execute("SELECT 1 FROM bot_config WHERE key = 'bot'").fetchone()
        if not has_bot:
            conn.execute(
                "INSERT INTO bot_config (key, value, updated_at) VALUES ('bot', ?, ?)",
                (json.dumps(config.DEFAULT_BOT_CONFIG, ensure_ascii=False), now()),
            )
        for rule in config.DEFAULT_TRIGGER_RULES:
            conn.execute(
                "INSERT OR IGNORE INTO trigger_rule"
                " (rule_type, label, threshold, enabled, priority, desc, updated_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (rule["rule_type"], rule["label"], rule["threshold"], rule["enabled"],
                 rule["priority"], rule["desc"], now()),
            )


def list_trigger_rules() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM trigger_rule ORDER BY priority").fetchall()
    return [dict(r) for r in rows]


def get_trigger_thresholds() -> dict[str, dict[str, Any]]:
    return {r["rule_type"]: r for r in list_trigger_rules()}


def update_trigger_rule(rule_type: str, threshold: int | None = None,
                        enabled: int | None = None) -> bool:
    fields: dict[str, Any] = {}
    if threshold is not None:
        fields["threshold"] = int(threshold)
    if enabled is not None:
        fields["enabled"] = int(enabled)
    if not fields:
        return False
    fields["updated_at"] = now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        return conn.execute(
            f"UPDATE trigger_rule SET {cols} WHERE rule_type = ?", (*fields.values(), rule_type)
        ).rowcount > 0


# ==================================================================== 账号与权限（M7-2）
def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000).hex()


def create_account(username: str, password: str, name: str, role: str, enabled: bool = True) -> int:
    salt = os.urandom(16).hex()
    ts = now()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO account (username, password_hash, salt, name, role, enabled, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (username, _hash_password(password, salt), salt, name or username, role,
             1 if enabled else 0, ts, ts),
        )
        return int(cur.lastrowid)


def list_accounts() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, username, name, role, enabled, created_at, updated_at FROM account ORDER BY id"
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["role_label"] = config.ROLE_LABELS.get(item["role"], item["role"])
        result.append(item)
    return result


def get_account(account_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM account WHERE id = ?", (account_id,)).fetchone()
    return dict(row) if row else None


def get_account_by_username(username: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM account WHERE username = ?", (username,)).fetchone()
    return dict(row) if row else None


def verify_account(username: str, password: str) -> dict[str, Any] | None:
    account = get_account_by_username(username)
    if not account or not account["enabled"]:
        return None
    expected = account["password_hash"]
    actual = _hash_password(password, account["salt"])
    if not hmac.compare_digest(expected, actual):
        return None
    return account


def update_account(account_id: int, password: str | None = None, name: str | None = None,
                   role: str | None = None, enabled: int | None = None) -> bool:
    fields: dict[str, Any] = {}
    if password:
        salt = os.urandom(16).hex()
        fields["salt"] = salt
        fields["password_hash"] = _hash_password(password, salt)
    if name:
        fields["name"] = name
    if role:
        fields["role"] = role
    if enabled is not None:
        fields["enabled"] = int(enabled)
    if not fields:
        return False
    fields["updated_at"] = now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        return conn.execute(
            f"UPDATE account SET {cols} WHERE id = ?", (*fields.values(), account_id)
        ).rowcount > 0


def delete_account(account_id: int) -> bool:
    with connect() as conn:
        return conn.execute("DELETE FROM account WHERE id = ?", (account_id,)).rowcount > 0


def _ensure_demo_agents() -> None:
    """未启用认证时准备两个演示坐席。

    这样工作台、数据看板、后台三处的客服数据始终一致，
    不会出现「看板显示暂无客服账号、工作台却有客服」的矛盾。
    """
    if config.AUTH_ENABLED or list_agents():
        return
    create_agent("客服小美", status="online")
    create_agent("客服小刚", status="offline")


def _ensure_canned() -> None:
    """首次启动准备几条团队常用语，方便直接演示工作台。"""
    if list_canned():
        return
    for title, content in [
        ("核实中", "您好，我已经在为您核实，请稍等片刻～"),
        ("退货流程", "退货共三步：①「我的订单」→「申请退货」并选择原因；② 24 小时内审核通过后会给出退货地址；③ 商品寄回签收后 1-3 个工作日完成退款。"),
        ("物流查询", "您可以在「我的订单」→ 选择订单 →「查看物流」看到实时轨迹；若物流超过 48 小时未更新，我帮您向快递公司发起查件。"),
        ("退款时效", "退款会在验货通过后 1-3 个工作日发起：微信/支付宝一般当天到账，银行卡 3-7 个工作日到账。"),
        ("服务范围", "很抱歉这个问题超出了我的权限范围，我帮您记录并转交对应同事跟进，会在 1 个工作日内回复您。"),
    ]:
        create_canned(title, content, scope="team")


def _ensure_accounts() -> None:
    """首次启动创建演示账号；同时为 agent 角色自动建客服坐席。"""
    if not config.AUTH_ENABLED:
        return
    if list_accounts():
        return
    for username, password, name, role in [
        ("admin", "admin123", "系统管理员", "admin"),
        ("supervisor", "123456", "客服主管", "supervisor"),
        ("agent1", "123456", "客服小美", "agent"),
        ("agent2", "123456", "客服小刚", "agent"),
        ("editor", "123456", "知识运营", "editor"),
    ]:
        account_id = create_account(username, password, name, role)
        if role == "agent":
            create_agent(name=name, account_id=account_id, status="offline")
