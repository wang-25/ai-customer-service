"""对话引擎：会话状态机 + 固定意图 + 槽位澄清 + 上下文改写 + 兜底与转人工决策。

完整链路（对应 PRD M2 全部 P0）：

    用户消息
      → ① 会话状态判定（排队 / 人工接待中 / 已结束 → 走不同分支）
      → ② 槽位澄清未完成 → 先处理槽位回答
      → ③ 固定意图（寒暄 / 致谢 / 结束 / 转人工）
      → ④ 当前消息单独检索
      → ⑤ 不够可信且存在上文 → 聚焦检索（限定上一话题分类、排除已答条目）
      → ⑥ 命中条目需要槽位 → 发起澄清（一轮一问，最多 N 轮）
      → ⑦ 置信度分档：直接作答 / 作答+确认 / 兜底
      → ⑧ 转人工触发规则检查（高风险词 / 情绪词 / 兜底次数 / 轮次）
      → ⑨ 落库（含答案溯源）+ 更新会话上下文
"""
from __future__ import annotations

from typing import Any

from . import config, db, handoff, knowledge, llm

# ==================================================================== 词表
GREETING_WORDS = ["你好", "您好", "hi", "hello", "在吗", "在么", "有人吗", "哈喽", "早上好", "晚上好"]
THANKS_WORDS = ["谢谢", "多谢", "感谢", "thanks", "3q", "辛苦了"]
BYE_WORDS = ["再见", "拜拜", "bye", "没事了", "没有了", "不用了"]
HANDOFF_WORDS = ["转人工", "人工客服", "找人工", "转客服", "真人", "人工", "客服电话"]
AFFIRM_WORDS = ["是的", "对", "对的", "嗯", "好的", "好", "可以", "行", "没错", "是", "ok"]

ANAPHORA_WORDS = ["它", "他", "她", "这个", "那个", "这", "那", "该", "此", "上述", "刚才", "还有呢", "呢"]


def _norm(text: str) -> str:
    return knowledge.normalize(text)


def _is_pure(text: str, words: list[str], max_len: int = 10) -> bool:
    t = _norm(text)
    if not t or len(t) > max_len:
        return False
    return any(_norm(w) in t for w in words)


def _is_affirmative(text: str) -> bool:
    t = _norm(text)
    return bool(t) and len(t) <= 4 and t in {_norm(w) for w in AFFIRM_WORDS}


def _needs_context(text: str) -> bool:
    t = _norm(text)
    if not t:
        return False
    if len(t) <= 6:
        return True
    return any(w in t for w in ANAPHORA_WORDS) and len(t) <= 16


def _is_handoff_request(text: str) -> bool:
    t = _norm(text)
    return any(_norm(w) in t for w in HANDOFF_WORDS)


# ==================================================================== 槽位
def _mentions_option(text: str, rule: dict[str, Any]) -> bool:
    """用户这句话里是否已经说了槽位的前提（例如已经说明「质量问题」）。

    判断依据是**用户原话**而不是命中的条目：条目本身可能是细分条目
    （如「商品质量问题退货的运费」），但用户问的是泛化的「运费谁出」，
    这时仍然需要澄清。
    """
    haystack = _norm(text)
    if not haystack:
        return False
    for option in rule["options"]:
        if any(_norm(k) in haystack for k in option["keywords"]):
            return True
        if _norm(option["label"]) in haystack:
            return True
    return False


def find_slot_rule(entry: dict[str, Any], query: str = "") -> dict[str, Any] | None:
    """判断命中的知识条目是否需要先澄清某个槽位。

    Args:
        entry: 命中的知识条目
        query: 用户这一轮的原话；若已包含某个选项关键词，说明前提已明确，不必再问
    """
    rules = {r["id"]: r for r in config.SLOT_RULES}
    rule: dict[str, Any] | None = None
    if entry.get("slot") and entry["slot"] in rules:
        rule = rules[entry["slot"]]
    else:
        haystack = _norm(
            " ".join(
                [entry.get("question", "")]
                + list(entry.get("similar") or [])
                + list(entry.get("keywords") or [])
            )
        )
        for candidate in config.SLOT_RULES:
            if candidate.get("when_category") and entry.get("category") != candidate["when_category"]:
                continue
            if any(_norm(k) in haystack for k in candidate["when_keywords"]):
                rule = candidate
                break
    if not rule:
        return None
    # 用户已经把前提说清楚了 → 直接作答，不要再追问
    if _mentions_option(query, rule):
        return None
    return rule


def match_slot_option(rule: dict[str, Any], message: str) -> dict[str, Any] | None:
    """把用户回答匹配到槽位选项（支持点选项按钮，也支持直接打字）。"""
    t = _norm(message)
    if not t:
        return None
    for option in rule["options"]:
        if _norm(option["label"]) in t or t in _norm(option["label"]):
            return option
        if any(_norm(k) in t for k in option["keywords"]):
            return option
    # 宽松匹配：只输入了「质量」「个人」「银卡」这类关键词片段
    for option in rule["options"]:
        for kw in option["keywords"]:
            k = _norm(kw)
            if len(t) >= 2 and (t in k or k in t):
                return option
    return None


def _clarify_payload(rule: dict[str, Any]) -> dict[str, Any]:
    return {
        "slot_id": rule["id"],
        "slot_name": rule["name"],
        "question": rule["question"],
        "options": [{"label": o["label"]} for o in rule["options"]],
    }


# ==================================================================== 主入口
def handle_message(
    session_id: str, message: str, visitor_id: str = ""
) -> dict[str, Any]:
    message = (message or "").strip()
    if not message:
        raise ValueError("message 不能为空")

    session = db.get_session(session_id)
    if session is None:
        raise ValueError(f"会话不存在: {session_id}")

    cfg = db.get_bot_config()
    user_msg_id = db.add_message(session_id, "user", message)
    db.update_session(session_id, last_user_at=db.now())
    # 双向实时同步：
    #   ① 访客自己的消息也要推回访客通道 —— 前端在实时模式下完全以 WS 流为准，
    #      不推的话用户发完消息看不到自己的气泡（刷新走全量 sync 才补上）；
    #      多标签页/多设备同时打开时也需要这个回显。
    #   ② 同时通知工作台，避免「客户开口了但坐席不知道」。
    try:
        from . import realtime

        realtime.realtime.push_message(session_id, db.attach_source_details([{
            "id": user_msg_id, "session_id": session_id, "role": "user", "content": message,
            "kb_id": None, "kb_category": None, "score": None, "answer_type": None,
            "context_used": False, "sources": [], "agent_id": None,
            "created_at": db.now(),
        }])[0])
        realtime.realtime.to_agents({
            "type": "message", "session_id": session_id, "role": "user",
        })
    except Exception:  # noqa: BLE001 - 推送失败不影响对话
        pass

    # ---------------------------------------------------- ① 会话状态分支
    status = session["status"]
    if status == "queueing":
        return _waiting_reply(session, message, cfg, "排队中")
    if status == "agent":
        return _waiting_reply(session, message, cfg, "人工接待中")
    if status in ("closed", "timeout"):
        return {
            "reply": "",
            "answer_type": "session_closed",
            "matched": None,
            "suggestions": [],
            "context_used": False,
            "sources": [],
            "turn": session["turn_count"],
            "fallback_count": session["fallback_count"],
        }

    # ---------------------------------------------------- ② 槽位回答
    slot_result = _handle_pending_slot(session, message, cfg)
    if slot_result is not None:
        return _finalize(session_id, session, slot_result)

    # ---------------------------------------------------- ③ 固定意图
    fixed = _fixed_intent(session, message, cfg)
    if fixed is not None:
        result = fixed
        if result.get("end_session"):
            db.update_session(session_id, turn_count=int(session["turn_count"]) + 1)
            db.finish_session(session_id, end_reason="user")
            result["session_status"] = "closed"
            result["csat_requested"] = True
            result.pop("end_session", None)
        return _finalize(session_id, session, result)

    # ---------------------------------------------------- ④⑤⑥⑦ 检索决策
    result = _retrieve_and_decide(session, message, cfg)

    # ---------------------------------------------------- ⑧ 触发规则
    next_fallback = int(session["fallback_count"]) + 1 if result["answer_type"].startswith("fallback") else 0
    trigger = handoff.check_trigger_rules(session, message, next_fallback)
    if trigger and result["answer_type"] != "handoff":
        h = handoff.start_handoff(
            session_id, trigger["rule_type"], trigger["detail"],
            prefix_reply=cfg["handoff_trigger_prefix"] + "\n",
        )
        result["reply"] = h["reply"]
        result["answer_type"] = "handoff_offline" if h["offline"] else "handoff_trigger"
        result["handoff"] = h
        result["sources"] = []
        result["matched"] = None
        result["suggestions"] = []
        result["csat_requested"] = False

    return _finalize(session_id, session, result)


def _waiting_reply(session: dict[str, Any], message: str, cfg: dict[str, Any], phase: str) -> dict[str, Any]:
    """排队或人工接待中：机器人静默，消息已同步给客服（PRD M4-3）。"""
    if session["status"] == "queueing":
        position = handoff.queue_position(session["id"])
        reply = f"已收到您的消息，客服正在赶来（当前排在第 {position} 位），请稍候～"
    else:
        reply = ""
    return {
        "reply": reply,
        "answer_type": "waiting_agent" if session["status"] == "agent" else "queueing",
        "matched": None,
        "suggestions": [],
        "context_used": False,
        "sources": [],
        "turn": session["turn_count"],
        "fallback_count": session["fallback_count"],
        "session_status": session["status"],
    }


# ==================================================================== 固定意图
def _fixed_intent(session: dict[str, Any], message: str, cfg: dict[str, Any]) -> dict[str, Any] | None:
    if _is_handoff_request(message):
        h = handoff.start_handoff(session["id"], "user_request", "用户主动要求转人工")
        return {
            "reply": h["reply"],
            "answer_type": "handoff_offline" if h["offline"] else "handoff",
            "matched": None,
            "suggestions": [],
            "context_used": False,
            "sources": [],
            "handoff": h,
        }

    if _is_pure(message, GREETING_WORDS):
        quick = [{"id": e["id"], "question": e["question"], "score": None}
                 for e in _hot_questions(db.list_knowledge(enabled=1))]
        return {
            "reply": cfg["welcome"],
            "answer_type": "greeting",
            "matched": None,
            "suggestions": quick,
            "context_used": False,
            "sources": [],
        }

    if _is_pure(message, THANKS_WORDS, max_len=8):
        return {"reply": "不客气～还有其他问题随时问我。", "answer_type": "smalltalk",
                "matched": None, "suggestions": [], "context_used": False, "sources": []}

    if _is_pure(message, BYE_WORDS, max_len=8):
        return {
            "reply": cfg["ending"] + "\n\n如果方便，请为本次服务点个评价，帮助我们做得更好。",
            "answer_type": "ending",
            "matched": None,
            "suggestions": [],
            "context_used": False,
            "sources": [],
            "end_session": True,
        }

    if _is_affirmative(message) and session.get("last_question"):
        same_cat = [
            {"id": e["id"], "question": e["question"], "score": None}
            for e in db.list_knowledge(enabled=1)
            if e["category"] == session.get("last_category")
        ][:3]
        return {
            "reply": f"好的。关于「{session['last_question']}」，您还可以继续追问细节。",
            "answer_type": "confirm_context",
            "matched": None,
            "suggestions": same_cat,
            "context_used": True,
            "sources": [],
        }
    return None


def _hot_questions(entries: list[dict[str, Any]], limit: int = 4) -> list[dict[str, Any]]:
    seen: set[str] = set()
    picked: list[dict[str, Any]] = []
    for e in entries:
        if e["category"] in seen:
            continue
        seen.add(e["category"])
        picked.append(e)
        if len(picked) >= limit:
            break
    return picked


# ==================================================================== 槽位处理
def _handle_pending_slot(
    session: dict[str, Any], message: str, cfg: dict[str, Any]
) -> dict[str, Any] | None:
    """会话等待槽位回答时，先尝试从本条消息里提取槽位。"""
    pending = session.get("pending_slot")
    if not pending:
        return None
    rule = next((r for r in config.SLOT_RULES if r["id"] == pending), None)
    if not rule:
        db.update_session(session["id"], pending_slot=None)
        return None

    option = match_slot_option(rule, message)
    slots = dict(session.get("slots") or {})
    if not option:
        # 没能识别 → 放弃澄清，按普通消息继续处理（避免把用户卡死在澄清里）
        db.update_session(session["id"], pending_slot=None)
        return None

    slots[rule["id"]] = {"label": option["label"], "keywords": option["keywords"]}
    db.update_session(session["id"], slots=slots, pending_slot=None)

    # 用「触发澄清的那句用户原话 + 选项关键词」重新检索，而不是用命中条目的标准问：
    # 标准问里往往已经含有一个更泛的说法，拼接后会把它自己（而非细分条目）匹配回来。
    recent_user = db.get_recent_user_messages(session["id"], 6)
    base = recent_user[-2] if len(recent_user) >= 2 else (session.get("last_question") or "")
    query = f"{base} {' '.join(option['keywords'])}"
    entries = db.list_knowledge(enabled=1)
    answered_ids = {m["kb_id"] for m in db.get_messages(session["id"]) if m.get("kb_id")}
    # 触发澄清的条目怎么处理，取决于它是"泛化入口"还是"细分条目"：
    #   · 泛化入口（自身带 slot，如「怎么开发票」）→ 保持排除，去找细分条目作答
    #   · 细分条目（不带 slot，只是恰好被排到第一，如「商品质量问题退货的运费」）→ 放回来
    trigger_id = session.get("last_kb_id")
    trigger_entry = db.get_knowledge(trigger_id) if trigger_id else None
    if not (trigger_entry and trigger_entry.get("slot") == rule["id"]):
        answered_ids.discard(trigger_id)
    cands = knowledge.retrieve(query, entries, top_k=config.TOP_K, exclude_ids=answered_ids)
    best = cands[0] if cands else None

    if best is None or best["score"] < config.MID_CONFIDENCE:
        # 槽位答案没找到对应条目 → 用原问题的答案兜底
        fallback_id = session.get("last_kb_id")
        entry = db.get_knowledge(fallback_id) if fallback_id else None
        if entry:
            best = {"id": entry["id"], "question": entry["question"], "answer": entry["answer"],
                    "category": entry["category"], "score": 1.0,
                    "matched_surface": entry["question"], "source_ref": entry.get("source_ref")}
        else:
            return None

    reply = f"（已记录：{rule['name']} = {option['label']}）\n\n{best['answer']}"
    return {
        "reply": reply,
        "answer_type": "kb_slot_filled",
        "matched": best,
        "suggestions": _suggestions(cands[1:4] if len(cands) > 1 else []),
        "context_used": True,
        "sources": [best["id"]],
        "slot_filled": {"slot_id": rule["id"], "slot_name": rule["name"], "label": option["label"]},
    }


def _suggestions(cands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"id": c["id"], "question": c["question"], "score": c["score"]} for c in cands]


# ==================================================================== 检索与决策
def _retrieve_and_decide(
    session: dict[str, Any], message: str, cfg: dict[str, Any]
) -> dict[str, Any]:
    entries = db.list_knowledge(enabled=1)
    answered_ids = {m["kb_id"] for m in db.get_messages(session["id"]) if m.get("kb_id")}
    recent_user_msgs = db.get_recent_user_messages(session["id"], config.CONTEXT_HISTORY_TURNS)

    high, mid = config.HIGH_CONFIDENCE, config.MID_CONFIDENCE
    cands = knowledge.retrieve(message, entries, top_k=config.TOP_K)
    best = cands[0] if cands else None
    context_used = False
    used_query = message

    # ⑤ 聚焦检索：当前问法不够可信 + 存在上文时，结合上文重新检索。
    #    注意：向量召回会让「那要多久」这类省略式追问脱离上下文也有较高分
    #    （例如误命中「发票多久能开出来」），因此**追问一律要走上下文消歧**，
    #    并在比较时给上下文话题更宽的容差。
    follow_up = bool(session.get("last_question")) and _needs_context(message)
    if (best is None or best["score"] < high or follow_up) and session.get("last_question"):
        rewritten = f"{session['last_question']} {message}"
        cat = session.get("last_category")
        focus = bool(cat and _needs_context(message))
        ctx_cands = knowledge.retrieve(
            rewritten, entries, top_k=config.TOP_K, exclude_ids=answered_ids,
            prefer_category=cat, category_only=focus,
        )
        # 追问（「那…呢」「怎么办」）更可能指回当前话题，
        # 若排除已答条目后没有好结果，则允许再次命中上一轮条目
        if not ctx_cands or ctx_cands[0]["score"] < config.MID_CONFIDENCE * 0.8:
            allow = answered_ids - {session.get("last_kb_id")}
            ctx_cands2 = knowledge.retrieve(
                rewritten, entries, top_k=config.TOP_K, exclude_ids=allow,
                prefer_category=cat, category_only=focus,
            )
            if ctx_cands2 and (not ctx_cands or ctx_cands2[0]["score"] > ctx_cands[0]["score"]):
                ctx_cands = ctx_cands2
        tolerance = 0.15 if follow_up else 0.05
        if ctx_cands and (best is None or ctx_cands[0]["score"] >= best["score"] - tolerance):
            best, cands = ctx_cands[0], ctx_cands
            context_used = True
            used_query = rewritten
            mid = mid * 0.8

    # ⑥ 命中条目需要槽位 → 发起澄清
    if best is not None and best["score"] >= mid:
        rule = find_slot_rule(best, message)
        slots = session.get("slots") or {}
        if rule and rule["id"] not in slots:
            clarify_round = int(session.get("clarify_round") or 0) + 1
            db.update_session(
                session["id"], pending_slot=rule["id"], clarify_round=clarify_round,
                last_kb_id=best["id"], last_question=session.get("last_question"),
                last_category=best["category"],
            )
            # 消息落库统一由 _finalize 负责，这里不要重复写入
            return {
                "reply": rule["question"],
                "answer_type": "clarify_slot",
                "matched": best,
                "suggestions": [],
                "context_used": context_used,
                "sources": [best["id"]],
                "clarify": _clarify_payload(rule),
            }

    # ⑦ 置信度分档
    if best is None or best["score"] < mid:
        return _fallback(session, message, entries, _suggestions(cands), context_used, cfg)

    if not context_used and session.get("last_question") and _needs_context(message) \
            and best["category"] == session.get("last_category"):
        context_used = True

    # 高置信直接作答；「仅靠向量命中」的候选必须先向用户确认一次
    vector_only = bool(best.get("vector_only")) and config.VECTOR_ONLY_REQUIRE_CONFIRM
    if best["score"] >= high and not vector_only:
        reply, answer_type = best["answer"], "kb_exact"
    else:
        reply = f"您是想问「{best['question']}」吗？\n\n{best['answer']}"
        answer_type = "kb_vector_confirm" if vector_only and best["score"] >= high else "kb_confirm"

    sources = [c["id"] for c in cands if c["score"] >= mid][:3] or [best["id"]]
    source_kind = "kb"
    polished = llm.polish_answer(message, best["question"], best["answer"], recent_user_msgs)
    if polished:
        reply, source_kind = polished, "llm"
        sources = [best["id"]]

    if context_used:
        answer_type = answer_type.replace("kb_", "kb_context_")

    return {
        "reply": reply,
        "answer_type": answer_type,
        "matched": best,
        "suggestions": _suggestions(cands[1:4]),
        "context_used": context_used,
        "sources": sources,
        "source": source_kind,
        "used_query": used_query,
    }


def _fallback(
    session: dict[str, Any],
    message: str,
    entries: list[dict[str, Any]],
    suggestions: list[dict[str, Any]],
    context_used: bool,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """兜底：猜你想问 → 引导换说法 → 连续兜底则提示转人工。"""
    last_cat = session.get("last_category")
    if not suggestions and last_cat:
        same_cat = [e for e in entries if e["category"] == last_cat]
        suggestions = [{"id": e["id"], "question": e["question"], "score": None}
                       for e in same_cat[:3]]

    lines = [cfg["fallback"]]
    if suggestions:
        lines.append("您是想问下面这些问题吗？")
        lines += [f"{i}. {s['question']}" for i, s in enumerate(suggestions[:3], 1)]
        lines.append("您也可以换个说法再问一次，比如补上具体商品或订单场景。")
    else:
        lines.append("您可以换个说法再问一次，或者补充一下具体场景（比如商品、订单、售后）。")

    rule = db.get_trigger_thresholds().get("fallback_count") or {}
    next_fallback = int(session.get("fallback_count") or 0) + 1
    if rule.get("enabled") and next_fallback >= int(rule.get("threshold", 2)):
        lines.insert(0, cfg["handoff_suggest"])
        answer_type = "fallback_handoff_suggest"
    else:
        answer_type = "fallback"

    db.add_fallback(session["id"], message)
    return {
        "reply": "\n".join(lines),
        "answer_type": answer_type,
        "matched": None,
        "suggestions": suggestions[:3],
        "context_used": context_used,
        "sources": [],
    }


# ==================================================================== 落库与状态更新
def _finalize(session_id: str, session: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    matched = result.get("matched") or {}
    reply = result.get("reply") or ""
    answer_type = result.get("answer_type") or "kb_exact"

    if reply:
        msg_id = db.add_message(
            session_id, "bot", reply,
            kb_id=matched.get("id"),
            kb_category=matched.get("category"),
            score=matched.get("score"),
            answer_type=answer_type,
            context_used=bool(result.get("context_used")),
            sources=result.get("sources") or [],
        )
        if not session.get("first_bot_at"):
            db.update_session(session_id, first_bot_at=db.now())
        # 实时推送给用户端（WebSocket；没有连接时静默跳过）
        try:
            from . import realtime

            pushed = db.attach_source_details([{
                "id": msg_id, "session_id": session_id, "role": "bot", "content": reply,
                "kb_id": matched.get("id"), "kb_category": matched.get("category"),
                "score": matched.get("score"), "answer_type": answer_type,
                "context_used": bool(result.get("context_used")),
                "sources": result.get("sources") or [], "agent_id": None,
                "created_at": db.now(),
            }])[0]
            realtime.realtime.push_message(session_id, pushed)
        except Exception:  # noqa: BLE001 - 推送失败不影响对话
            pass

    turn = int(session["turn_count"]) + 1
    is_fallback = answer_type.startswith("fallback")
    new_fallback = int(session["fallback_count"]) + 1 if is_fallback else 0

    fields: dict[str, Any] = {"turn_count": turn, "fallback_count": new_fallback}
    if matched.get("id"):
        fields["last_kb_id"] = matched["id"]
        fields["last_question"] = matched.get("question") or session.get("last_question")
        fields["last_category"] = matched.get("category") or session.get("last_category")
    if "clarify" not in result:
        fields["clarify_round"] = 0
    db.update_session(session_id, **fields)

    refreshed = db.get_session(session_id) or session
    result.update({
        "session_id": session_id,
        "turn": turn,
        "fallback_count": new_fallback,
        "session_status": refreshed["status"],
        "source": result.get("source", "kb"),
        "csat_requested": bool(result.get("csat_requested")),
    })
    result.setdefault("sources", [])
    return result
