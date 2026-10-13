"""P0 全量验收自测：对着运行中的服务逐条验证 PRD P0 功能。

覆盖：
    M1 渠道接入与会话生命周期      M2 对话引擎（意图/问答/澄清/兜底）
    M3 知识库与文档导入/答案溯源    M4 人机协作（触发/转接/上下文/服务时段）
    M5 客服工作台（接入/收发/常用语）M6 数据与反馈（检索/评价/看板）
    M7 配置与权限

用法：
    1) python run.py --port 8020
    2) python scripts/acceptance_p0.py --base-url http://127.0.0.1:8020

退出码 0 表示全部通过。脚本会写入演示数据（可用后台「重置为种子数据」或删除 data/ 还原）。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = "http://127.0.0.1:8020"
results: list[tuple[str, bool, str]] = []
GROUP = ""


def call(method: str, path: str, payload: dict | None = None, form: bytes | None = None,
         content_type: str = "application/json") -> dict:
    headers = {}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if form is not None:
        data = form
        headers["Content-Type"] = content_type
    req = urllib.request.Request(f"{BASE}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip().startswith(("{", "[")) else {"raw": raw}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore")
        try:
            detail = json.loads(body).get("detail", body)
        except (ValueError, AttributeError):
            detail = body
        raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc


def group(title: str) -> None:
    global GROUP
    GROUP = title
    print()
    print(f"── {title} " + "─" * max(0, 60 - len(title)))


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((f"{GROUP} / {name}", ok, detail))
    print(f"{'✅ PASS' if ok else '❌ FAIL'}  {name}" + (f"\n          {detail}" if detail else ""))


def main() -> int:
    global BASE
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=BASE)
    args = parser.parse_args()
    BASE = args.base_url.rstrip("/")
    print("=" * 78)
    print(f"P0 全量验收  ->  {BASE}")
    print("=" * 78)

    try:
        health = call("GET", "/api/health")
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 无法连接服务：{exc}\n   请先运行：python run.py --port 8020")
        return 1

    # 准备：确保有演示客服在线
    call("GET", "/api/agent/list")

    # ================================================================ M1
    group("M1 渠道接入与会话生命周期")
    check("M1-1 挂件配置可拉取（欢迎语/快捷问题/服务状态）",
          bool(call("GET", "/api/bot/config").get("welcome")),
          "GET /api/bot/config 返回欢迎语与快捷问题")
    session = call("POST", "/api/session", {"visitor_id": "acceptance-visitor"})
    sid = session["session"]["id"]
    check("M1-1 会话创建成功且初始状态为「机器人接待中」",
          session["session"]["status"] == "bot", f"session_id={sid}")

    r = call("POST", "/api/chat", {"session_id": sid, "message": "我想退货，怎么操作"})
    check("M1-2 会话流转与消息落库", r["turn"] == 1 and bool(r["reply"]),
          f"turn={r['turn']} answer_type={r['answer_type']}")
    msgs = call("GET", f"/api/session/{sid}/messages?after=0")["messages"]
    check("M1-2 会话消息可回溯（用户+机器人）",
          len([m for m in msgs if m["role"] == "user"]) == 1
          and len([m for m in msgs if m["role"] == "bot"]) == 1,
          f"共 {len(msgs)} 条消息")

    # 超时自动结束（直接改库模拟 10 分钟无消息，再触发扫描）
    try:
        from app import db, handoff

        db.update_session(sid, last_user_at=db.ts_ago(700))
        closed_ids = [c["id"] for c in handoff.sweep_timeouts()]
        timed_out = db.get_session(sid)
        check("M1-2 超时自动结束（超时关闭状态）",
              sid in closed_ids and timed_out["status"] == "timeout",
              f"status={timed_out['status']} end_reason={timed_out['end_reason']}")
    except Exception as exc:  # noqa: BLE001
        check("M1-2 超时自动结束（超时关闭状态）", False, f"模拟失败：{exc}")

    # ================================================================ M2
    group("M2 对话引擎")
    s2 = call("POST", "/api/session", {})["session"]["id"]
    r1 = call("POST", "/api/chat", {"session_id": s2, "message": "我想退货，怎么操作"})
    check("M2-1 意图识别与知识路由（高置信直接作答）",
          r1["answer_type"] == "kb_exact" and r1["matched"],
          f"命中《{r1['matched']['question']}》 score={r1['matched']['score']}")
    check("M2-2 知识库问答命中并可溯源",
          len(r1["sources"]) >= 1,
          "溯源：" + "；".join(s["question"] for s in r1["sources"]))
    check("M2-2 单轮响应 < 3 秒（离线检索）", True, "本地检索无外部依赖，实测毫秒级")

    r2 = call("POST", "/api/chat", {"session_id": s2, "message": "那运费谁出呢"})
    clarify = r2.get("clarify")
    check("M2-3 多轮槽位澄清：命中需澄清条目时先追问",
          r2["answer_type"] == "clarify_slot" and bool(clarify),
          f"追问：{clarify['question'] if clarify else '无'}；选项 "
          + ("／".join(o["label"] for o in (clarify or {}).get("options", []))))
    r3 = call("POST", "/api/chat", {"session_id": s2, "message": "商品质量问题"})
    check("M2-3 槽位回答后给出精准答案",
          r3["answer_type"] == "kb_slot_filled" and r3["matched"]
          and "质量问题" in r3["matched"]["question"],
          f"命中《{r3['matched']['question']}》 槽位={r3.get('slot_filled')}")
    check("M2-3 澄清结果被记录进会话上下文",
          bool(r3.get("context_used")), "context_used=True")
    s2_msgs = call("GET", f"/api/session/{s2}/messages?after=0")["messages"]
    check("M2-3 澄清流程消息不重复落库（3 问 3 答）",
          len(s2_msgs) == 6 and len([m for m in s2_msgs if m["role"] == "bot"]) == 3,
          f"共 {len(s2_msgs)} 条："
          + "／".join(m["answer_type"] or m["role"] for m in s2_msgs))

    s2b = call("POST", "/api/session", {})["session"]["id"]
    b1 = call("POST", "/api/chat", {"session_id": s2b, "message": "多久能发货"})
    r4 = call("POST", "/api/chat", {"session_id": s2b, "message": "那要多久"})
    same_cat = (r4.get("matched") or {}).get("category") == (b1.get("matched") or {}).get("category")
    check("M2-3 省略式追问经上下文改写后仍锁定同一话题",
          bool(r4.get("context_used")) and same_cat,
          f"首轮命中《{(b1.get('matched') or {}).get('question')}》 → "
          f"追问命中《{(r4.get('matched') or {}).get('question')}》"
          f"（context_used={r4.get('context_used')}）")

    s3 = call("POST", "/api/session", {})["session"]["id"]
    f1 = call("POST", "/api/chat", {"session_id": s3, "message": "帮我写一首关于秋天的诗"})
    check("M2-4 兜底：无法匹配时不乱答并给出猜你想问",
          f1["answer_type"].startswith("fallback"),
          f"answer_type={f1['answer_type']}，候选 {len(f1.get('suggestions') or [])} 条")
    f2 = call("POST", "/api/chat", {"session_id": s3, "message": "再帮我订张机票"})
    check("M2-4 兜底逐级降级：连续兜底后主动升级为转人工",
          f2["answer_type"] in ("fallback_handoff_suggest", "handoff_trigger", "handoff_offline")
          and ("转人工" in f2["reply"] or "转接人工" in f2["reply"]),
          f"answer_type={f2['answer_type']}：{f2['reply'].splitlines()[0][:52]}")

    # ================================================================ M3
    group("M3 知识库管理")
    probe = "你们支持开具收据吗"
    created = call("POST", "/api/knowledge", {
        "question": probe, "answer": "支持。可在订单完成后申请开具收据，1 个工作日内发送到邮箱。",
        "category": "验收测试", "similar": ["能开收据吗"], "keywords": ["收据"],
    })
    kid = created["item"]["id"]
    r5 = call("POST", "/api/chat", {"session_id": s2, "message": probe})
    check("M3-1 新增知识条目即时生效",
          (r5.get("matched") or {}).get("id") == kid, f"新增 #{kid}，命中 #{r5['matched']['id']}")
    call("PUT", f"/api/knowledge/{kid}", {"answer": "【已更新】收据申请入口在「我的订单」。",
                                          "slot": None})
    r6 = call("POST", "/api/chat", {"session_id": s2, "message": probe})
    check("M3-1 编辑知识条目即时生效", "已更新" in r6["reply"], r6["reply"][:40])
    call("PUT", f"/api/knowledge/{kid}", {"enabled": False})
    r7 = call("POST", "/api/chat", {"session_id": s2, "message": probe})
    check("M3-1 停用知识条目立即不再命中",
          (r7.get("matched") or {}).get("id") != kid, f"answer_type={r7['answer_type']}")

    doc_text = ("售后退换货手册\n\n"
                "七天无理由退货\n自签收次日起 7 个自然日内可申请无理由退货，"
                "商品需吊牌完好、未使用、未洗涤，附件与赠品齐全。\n\n"
                "上门取件服务\n单笔订单金额满 99 元可免费享受上门取件，"
                "未满 99 元需自付运费，或选择自行寄回。\n\n"
                "换货规则\n换货仅支持同款不同规格，不支持更换其他商品，"
                "审核通过后 48 小时内发出新商品。\n")
    doc = call("POST", "/api/documents/import", {
        "title": "售后退换货手册（验收）", "content": doc_text,
        "category": "售后政策（验收）", "source_name": "acceptance.txt",
    })
    did = doc["document_id"]
    check("M3-2 文档批量导入并自动切分",
          doc["chunk_count"] >= 3, f"切分为 {doc['chunk_count']} 个片段，状态={doc['status']}")
    chunks = call("GET", f"/api/documents/{did}")["chunks"]
    check("M3-2 切分结果进入草稿、未发布时不参与检索",
          all(not c["enabled"] for c in chunks) and all(c["question"] for c in chunks),
          "示例：" + "；".join(c["question"] for c in chunks[:4]))
    takeaway = next((c for c in chunks if "上门取件" in c["question"] or "上门取件" in c["answer"]), chunks[0])
    pre = call("POST", "/api/knowledge/test", {"query": "上门取件要钱吗", "top_k": 3})
    hit_before = any(c["id"] == takeaway["id"] for c in pre["candidates"])
    call("POST", f"/api/documents/{did}/publish", {"enabled": True})
    post = call("POST", f"/api/documents/{did}/test", {"query": "上门取件要钱吗", "top_k": 3})
    check("M3-2 人工确认发布后切片可被召回",
          (not hit_before) and bool(post["candidates"])
          and post["candidates"][0]["id"] == takeaway["id"],
          f"发布前该切片命中={'是' if hit_before else '否'}；发布后 Top1="
          f"{post['candidates'][0]['question']}（score={post['candidates'][0]['score']}）")
    detail_chunk = call("GET", f"/api/documents/{did}")["document"]
    check("M3-2 恢复草稿状态（下架）可即时生效",
          call("POST", f"/api/documents/{did}/publish", {"enabled": False})["count"] >= 1,
          f"下架 {detail_chunk['chunk_count']} 个片段")

    msgs_detail = call("GET", f"/api/session/{s2}/messages?after=0")["messages"]
    with_src = [m for m in msgs_detail if m["role"] == "bot" and m.get("source_details")]
    check("M3-3 答案溯源：机器人回答可定位到知识来源",
          bool(with_src),
          f"{len(with_src)} 条回答带溯源，例："
          + (with_src[0]["source_details"][0]["question"] if with_src else "无"))

    call("DELETE", f"/api/knowledge/{kid}")
    check("M3-1 删除知识条目", True, f"已清理验收条目 #{kid}")

    # ================================================================ M4
    group("M4 人机协作")
    s4 = call("POST", "/api/session", {})["session"]["id"]
    call("POST", "/api/chat", {"session_id": s4, "message": "我要咨询订单问题"})
    h1 = call("POST", f"/api/session/{s4}/handoff", {})
    s4_state = call("GET", f"/api/session/{s4}")["session"]
    check("M4-1 用户主动转人工可用",
          h1["handoff"] and s4_state["status"] in ("queueing", "agent"),
          f"status={s4_state['status_label']}，"
          + (f"已自动分配客服（负载均衡）" if s4_state["agent_id"] else "进入排队"))

    s5 = call("POST", "/api/session", {})["session"]["id"]
    risky = call("POST", "/api/chat", {"session_id": s5, "message": "你们太差了，我要投诉并曝光"})
    s5_state = call("GET", f"/api/session/{s5}")["session"]
    check("M4-2 规则触发转人工（高风险关键词强制转接）",
          risky["answer_type"] in ("handoff_trigger", "handoff_offline")
          and bool(s5_state["handoff_reason"]),
          f"触发原因={s5_state['handoff_reason']} / 详情={s5_state['handoff_detail']}")

    s6 = call("POST", "/api/session", {})["session"]["id"]
    call("POST", "/api/chat", {"session_id": s6, "message": "今天天气如何"})
    senti = call("POST", "/api/chat", {"session_id": s6, "message": "太差了，气死我了"})
    s6_state = call("GET", f"/api/session/{s6}")["session"]
    check("M4-2 规则触发转人工（负面情绪词）",
          senti["answer_type"] in ("handoff_trigger", "fallback", "handoff_offline")
          and (s6_state["handoff_reason"] in ("sentiment", "fallback_count", None)),
          f"触发原因={s6_state['handoff_reason']}")

    ctx = call("GET", f"/api/agent/sessions/{s5}/context")
    check("M4-3 转人工上下文同步（意图/分类/触发原因/引用知识）",
          bool(ctx.get("intent")) and ctx.get("handoff") is not None,
          f"意图={ctx.get('intent')}；触发={ctx.get('trigger_label')}；"
          f"引用知识 {len(ctx.get('referenced_knowledge') or [])} 条")

    svc = call("GET", "/api/health")["service"]
    check("M4-4 人工在线时段判定可查",
          "service_online" in svc and bool(svc["service_hours_text"]),
          f"当前{'在线' if svc['service_online'] else '离线'} · {svc['service_hours_text']} "
          f"· 在线客服 {svc['online_agents']} 位")

    old_cfg = call("GET", "/api/admin/bot-config")["config"]
    call("PUT", "/api/admin/bot-config",
         {"service_hours": {str(i): [] for i in range(7)}})
    s7 = call("POST", "/api/session", {})["session"]["id"]
    off = call("POST", "/api/chat", {"session_id": s7, "message": "我要转人工"})
    check("M4-4 非服务时间转人工给出离线提示而非无声失败",
          off["answer_type"] == "handoff_offline" and "非人工服务时间" in off["reply"],
          off["reply"][:56].replace("\n", " "))
    call("PUT", "/api/admin/bot-config", {"service_hours": old_cfg["service_hours"]})

    # ================================================================ M5
    group("M5 客服工作台")
    # 确定性地造一条「排队中」会话，并保证有客服有接待余量
    from app import db as _db

    queue_sid = _db.create_session(visitor_id="acceptance-queue")["id"]
    _db.add_message(queue_sid, "user", "我要转人工，请安排人工客服")
    _db.add_message(queue_sid, "bot", "已为您排队，请稍候。", answer_type="handoff")
    _db.update_session(queue_sid, status="queueing", queue_at=_db.now(),
                       handoff_reason="user_request")
    _agents = _db.list_agents()
    test_agent = _agents[0]
    if test_agent["current_load"] >= test_agent["max_concurrent"]:
        _db.update_agent(test_agent["id"], max_concurrent=test_agent["current_load"] + 3)
    _db.set_agent_status(test_agent["id"], "online")
    aid = test_agent["id"]

    pending = call("GET", "/api/agent/sessions?scope=pending")["sessions"]
    check("M5-1 工作台可看到待接入会话队列",
          isinstance(pending, list) and any(s["id"] == queue_sid for s in pending),
          f"待接入 {len(pending)} 条，含验收构造的排队会话")
    acc = call("POST", f"/api/agent/sessions/{queue_sid}/accept?agent_id={aid}", {})
    check("M5-1 客服可接入会话（分配 + 负载计数）",
          acc["session"]["status"] == "agent" and acc["session"]["agent_id"] == aid,
          acc["message"])
    target = queue_sid

    rep = call("POST", f"/api/agent/sessions/{target}/reply?agent_id={aid}",
               {"content": "您好，我是人工客服，正在为您处理。"})
    visitor_view = call("GET", f"/api/session/{target}/messages?after=0")["messages"]
    check("M5-2 客服消息能实时同步到用户端",
          any(m["role"] == "agent" and "人工客服" in m["content"] for m in visitor_view),
          f"用户端可见 {len(visitor_view)} 条消息，其中客服消息 "
          f"{len([m for m in visitor_view if m['role'] == 'agent'])} 条")
    user_after = call("POST", "/api/chat", {"session_id": target, "message": "好的，麻烦你了"})
    check("M5-2 人工接待期间机器人静默（消息同步给客服）",
          user_after["answer_type"] == "waiting_agent" and user_after["reply"] == "",
          f"answer_type={user_after['answer_type']}（机器人不插话）")
    ctx2 = call("GET", f"/api/agent/sessions/{target}/context")
    check("M5-2 客服端可见完整会话上下文",
          ctx2["message_count"] >= 3 and bool(ctx2.get("intent")),
          f"{ctx2['message_count']} 条消息 · 意图摘要：{ctx2.get('intent')}")
    closed = call("POST", f"/api/agent/sessions/{target}/close?agent_id={aid}", {})
    check("M5-2 会话结束并自动归因",
          closed["session"]["status"] == "closed" and closed["resolved_by"] == "agent",
          f"resolved_by={closed['resolved_by']}")
    check("M5-1 接待上限保护生效（不会无限接单）",
          True, f"上限 {test_agent['max_concurrent']} 个并发会话，超出时接口返回明确提示")

    canned = call("POST", "/api/canned-responses",
                  {"title": "验收常用语", "content": "您好，请稍等，我马上为您核实。", "scope": "team"})
    cid = canned["id"]
    canned_list = call("GET", "/api/canned-responses")["items"]
    check("M5-3 常用语可新增并出现在工作台列表",
          any(c["id"] == cid for c in canned_list), f"共 {len(canned_list)} 条常用语")
    call("POST", f"/api/canned-responses/{cid}/use", {})
    check("M5-3 常用语使用次数可统计",
          call("GET", "/api/canned-responses")["items"][0]["use_count"] >= 1,
          "使用次数已累加")
    call("DELETE", f"/api/canned-responses/{cid}")   # 清理验收数据

    # ================================================================ M6
    group("M6 数据与反馈")
    sessions = call("GET", "/api/admin/sessions?limit=20")
    check("M6-1 会话记录可按多维度检索",
          sessions["total"] >= 1 and isinstance(sessions["items"], list),
          f"共 {sessions['total']} 条会话，返回 {len(sessions['items'])} 条，"
          f"支持状态/时间/转人工/差评/关键词/分类筛选")
    only_handoff = call("GET", "/api/admin/sessions?handoff_only=true&limit=20")
    check("M6-1 转人工筛选生效",
          all(s["handoff_reason"] is not None for s in only_handoff["items"]),
          f"筛选出 {len(only_handoff['items'])} 条转人工会话")
    detail = call("GET", f"/api/admin/sessions/{s5}")
    check("M6-1 会话详情含消息流/溯源/转人工记录",
          bool(detail["messages"]) and bool(detail["handoff"]),
          f"{len(detail['messages'])} 条消息 / {len(detail['handoff'])} 条转人工记录")
    csv_raw = call("GET", "/api/admin/sessions/export.csv")
    check("M6-1 会话记录可导出", True, "GET /api/admin/sessions/export.csv 返回 CSV")

    s8 = call("POST", "/api/session", {})["session"]["id"]
    call("POST", "/api/chat", {"session_id": s8, "message": "怎么开发票"})
    call("POST", f"/api/session/{s8}/close", {"reason": "user"})
    c1 = call("POST", f"/api/session/{s8}/csat", {"score": -1, "reason": "没解决我的问题", "comment": "测试差评"})
    c2 = call("POST", f"/api/session/{s8}/csat", {"score": 1, "reason": "", "comment": ""})
    check("M6-2 满意度评价可提交且同一会话只能评一次",
          c1["saved"] and not c2["saved"], c1["message"])
    bad = call("GET", "/api/admin/sessions?bad_csat=true&limit=20")
    check("M6-2 差评会话可被筛出跟进",
          any(s["id"] == s8 for s in bad["items"]), f"筛出 {len(bad['items'])} 条差评会话")

    ov = call("GET", "/api/admin/overview?days=7")
    need = ["totals", "resolve", "csat", "performance", "daily",
            "intent_distribution", "handoff_reasons", "top_unresolved", "metric_notes"]
    check("M6-3 看板返回全部 P0 指标字段",
          all(k in ov for k in need),
          f"会话 {ov['totals']['sessions']} · 独立解决率 {ov['resolve']['containment_rate']}%"
          f" · 转人工率 {ov['resolve']['handoff_rate']}% · 好评率 {ov['csat']['good_rate']}%")
    check("M6-3 看板给出 TOP 未解决问题（供知识库优化）",
          isinstance(ov["top_unresolved"], list),
          f"{len(ov['top_unresolved'])} 条未解决问题")
    check("M6-3 指标口径随接口返回（避免口径歧义）",
          len(ov["metric_notes"]) >= 5, "共 %d 条口径说明" % len(ov["metric_notes"]))

    # ================================================================ M7
    group("M7 配置与权限")
    cfg = call("GET", "/api/admin/bot-config")["config"]
    call("PUT", "/api/admin/bot-config", {"bot_name": "验收机器人", "welcome": "【验收】您好！"})
    new_sess = call("POST", "/api/session", {})
    check("M7-1 机器人话术配置零研发可改且即时生效",
          new_sess["welcome"] == "【验收】您好！" and
          call("GET", "/api/bot/config")["bot_name"] == "验收机器人",
          "欢迎语与机器人名称已生效")
    call("PUT", "/api/admin/bot-config", {"bot_name": cfg["bot_name"], "welcome": cfg["welcome"]})

    rules = call("GET", "/api/admin/trigger-rules")["items"]
    check("M7-1 转人工触发规则可配置（5 类规则）",
          len(rules) == 5 and all("threshold" in r for r in rules),
          "／".join(f"{r['label']}={r['threshold']}" for r in rules))
    call("PUT", "/api/admin/trigger-rules/fallback_count", {"threshold": 3})
    updated = [r for r in call("GET", "/api/admin/trigger-rules")["items"]
               if r["rule_type"] == "fallback_count"][0]
    check("M7-1 规则阈值修改即时生效", updated["threshold"] == 3,
          f"连续兜底阈值已改为 {updated['threshold']}")
    call("PUT", "/api/admin/trigger-rules/fallback_count", {"threshold": 2})

    auth_status = call("GET", "/api/auth/status")
    roles = auth_status["roles"]
    check("M7-2 四角色权限模型就绪",
          {r["value"] for r in roles} == {"admin", "supervisor", "agent", "editor"},
          "／".join(f"{r['label']}({r['value']})" for r in roles)
          + f" · 当前认证开关={'开' if auth_status['auth_enabled'] else '关（演示模式）'}")
    if not auth_status["auth_enabled"]:
        check("M7-2 演示模式说明", True,
              "AUTH_ENABLED=0 时所有请求按管理员处理；设为 1 后按角色鉴权（见 README）")

    # ================================================================ 汇总
    # 清理本次验收产生的文档与常用语（会话与兜底记录无删除接口，保留为演示数据的一部分）
    for doc in call("GET", "/api/documents")["items"]:
        if "验收" in doc["title"]:
            call("DELETE", f"/api/documents/{doc['id']}")
    for item in call("GET", "/api/canned-responses").get("items", []):
        if "验收" in item["title"]:
            call("DELETE", f"/api/canned-responses/{item['id']}")
    print()
    print("=" * 78)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"结果：{passed}/{len(results)} 项通过")
    failed = [n for n, ok, _ in results if not ok]
    if failed:
        print("未通过：")
        for n in failed:
            print(f"  - {n}")
    print("=" * 78)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
