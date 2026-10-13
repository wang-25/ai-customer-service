"""检索命中质量检验工具。

回答一个具体问题：**我怎么知道机器人是不是真的命中了？**

三种模式，分别对应「没有真实数据」「有标注数据」「有真实流量」三个阶段：

1) --selfcheck（不需要任何真实数据）
   用知识库自身的「标准问 + 相似问法」当测试集，逐条验证能否被自己召回。
   · 如果一个条目连它自己的相似问法都召不回来 → 它在线上必然答不上；
   · 如果条目 A 的相似问法命中了条目 B → 说明两者语义重叠，是误命中的高发点。
   这两类问题都能在没有任何真实数据时暴露出来。

2) --questions FILE（有真实问题，可选标注）
   批量跑一批真实问法。
   · 两列格式（问题 <TAB> 期望命中的关键词/条目ID）→ 直接算准确率、误命中率；
   · 一列格式（只有问题）→ 输出 Top3 候选 + 风险标记，人工扫一眼就能判断，
     相当于把「逐条标注」变成「快速抽检」。

3) --from-sessions（有真实会话后）
   把数据库里真实的用户提问回放一遍，用当前知识库重新检索，
   与当时的实际命中对比 —— 用来验证「改了知识库/调了阈值之后有没有变好」。

输出：控制台表格 + 可选的 Markdown 报告（--report 文件路径）。

用法示例：
    python scripts/eval_retrieval.py --selfcheck
    python scripts/eval_retrieval.py --selfcheck --report data/selfcheck.md
    python scripts/eval_retrieval.py --questions data/my_questions.txt
    python scripts/eval_retrieval.py --questions data/labeled.tsv --labeled
    python scripts/eval_retrieval.py --from-sessions --limit 100
    python scripts/eval_retrieval.py --export-template data/my_questions.txt
"""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db, embedding, knowledge  # noqa: E402

BAND_DIRECT, BAND_CONFIRM, BAND_FALLBACK = "直接作答", "作答+确认", "兜底"

# 这些 answer_type 由规则/人工链路产生，不经过知识检索，回放时没有可比性
RULE_ONLY_TYPES = {
    "greeting", "smalltalk", "ending", "handoff", "handoff_trigger", "handoff_offline",
    "waiting_agent", "queueing", "confirm_context", "system", "agent", "session_closed",
}


def band_of(cand: dict | None) -> str:
    if not cand:
        return BAND_FALLBACK
    if cand["score"] >= config.HIGH_CONFIDENCE:
        return BAND_CONFIRM if (cand.get("vector_only") and config.VECTOR_ONLY_REQUIRE_CONFIRM) else BAND_DIRECT
    if cand["score"] >= config.MID_CONFIDENCE:
        return BAND_CONFIRM
    return BAND_FALLBACK


def risk_flags(cand: dict | None) -> list[str]:
    """标记「看起来命中了、但值得人工复核」的情况。"""
    if not cand:
        return ["无候选"]
    flags = []
    if cand.get("vector_only"):
        flags.append("仅靠语义命中")
    if band_of(cand) == BAND_CONFIRM and cand["score"] >= config.HIGH_CONFIDENCE:
        flags.append("语义兜底为确认")
    if config.MID_CONFIDENCE <= cand["score"] < config.HIGH_CONFIDENCE:
        flags.append("中等置信度")
    if cand.get("lex_score", 0) < config.MID_CONFIDENCE <= cand.get("vec_score", 0):
        flags.append("词面弱")
    return flags


# ==================================================================== 自检
def selfcheck(entries: list[dict], top_k: int = 3) -> dict:
    """用知识库自身的问法做召回自测（无需真实数据）。"""
    rows = []
    total_queries = 0
    total_hits = 0
    for entry in entries:
        queries = [entry["question"]] + list(entry.get("similar") or [])
        queries = [q for q in queries if q and q.strip()][:6]
        if not queries:
            continue
        hits = 0
        wrong = []
        for q in queries:
            cands = knowledge.retrieve(q, entries, top_k=top_k)
            top = cands[0] if cands else None
            total_queries += 1
            if top and top["id"] == entry["id"]:
                hits += 1
                total_hits += 1
            else:
                wrong.append({
                    "query": q,
                    "got": top["question"] if top else "（无候选）",
                    "score": top["score"] if top else 0.0,
                    "band": band_of(top),
                })
        rows.append({
            "id": entry["id"],
            "question": entry["question"],
            "category": entry["category"],
            "queries": len(queries),
            "hits": hits,
            "recall": hits / len(queries),
            "wrong": wrong,
        })
    rows.sort(key=lambda r: (r["recall"], r["id"]))
    return {
        "entries": rows,
        "total_entries": len(rows),
        "total_queries": total_queries,
        "total_hits": total_hits,
        "recall": (total_hits / total_queries) if total_queries else 0.0,
        "perfect": sum(1 for r in rows if r["recall"] == 1.0),
        "problematic": [r for r in rows if r["recall"] < 1.0],
    }


def print_selfcheck(result: dict, entries: list[dict]) -> None:
    print("=" * 92)
    print("知识库召回自检（用条目自己的标准问 + 相似问法做测试集，不需要真实数据）")
    print("=" * 92)
    print(f"条目 {result['total_entries']} 条 | 测试问法 {result['total_queries']} 条 | "
          f"自召回率 {result['recall'] * 100:.1f}% ({result['total_hits']}/{result['total_queries']})")
    print(f"完全可自召回条目：{result['perfect']}/{result['total_entries']}")
    print()
    if not result["problematic"]:
        print("✅ 每条知识都能被自己的问法召回，没有互相抢占的情况。")
        return
    print(f"⚠️  有 {len(result['problematic'])} 条存在自召回问题，按严重程度排序：")
    print("-" * 92)
    for row in result["problematic"]:
        print(f"#{row['id']} [{row['category']}] {row['question']}")
        print(f"    自召回 {row['hits']}/{row['queries']}（{row['recall'] * 100:.0f}%）")
        for w in row["wrong"]:
            print(f"    ✗ 「{w['query']}」→ 命中【{w['got']}】(分数 {w['score']:.3f} / {w['band']})")
    print("-" * 92)
    print("怎么读这份报告：")
    print("  · 「自召回率低」= 这条知识在线上很可能答不上（问法覆盖不够，补 similar / keywords）")
    print("  · 「串到了另一条」= 两条知识语义重叠，是误命中的高发点（合并或删掉泛化的那条）")


# ==================================================================== 批量问题
def parse_questions(path: Path) -> list[tuple[str, str]]:
    """支持「问题」单列，或「问题 <TAB> 期望命中的关键词/条目ID」两列。"""
    items: list[tuple[str, str]] = []
    for raw in io.open(path, encoding="utf-8").read().split("\n"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("\t")]
        if len(parts) == 1:
            parts = [p.strip() for p in line.split(",")]
        question = parts[0]
        expect = parts[1] if len(parts) > 1 else ""
        if question:
            items.append((question, expect))
    return items


def eval_questions(pairs: list[tuple[str, str]], entries: list[dict], top_k: int = 3) -> dict:
    rows = []
    labeled = any(expect for _, expect in pairs)
    stats = {"hit": 0, "miss": 0, "fallback": 0, "need_review": 0}
    for question, expect in pairs:
        cands = knowledge.retrieve(question, entries, top_k=top_k)
        top = cands[0] if cands else None
        band = band_of(top)
        verdict = "—"
        flags = risk_flags(top)
        if expect:
            expect_norm = expect.strip()
            # 期望「兜底」的用例（域外问题）：真正的正确行为是**不要答**
            expect_fallback = ("兜底" in expect_norm) or expect_norm in ("-", "—", "无")
            if expect_fallback:
                if band == BAND_FALLBACK:
                    verdict = "命中(正确兜底)"
                    stats["hit"] += 1
                else:
                    verdict = "误命中(不该答却答了)"
                    stats["miss"] += 1
                    flags = [*flags, "域外问题被误答"]
            else:
                ok = bool(top) and (
                    expect_norm.isdigit() and top["id"] == int(expect_norm)
                    or expect_norm in (top["question"] or "")
                )
                if ok:
                    verdict = "命中"
                    stats["hit"] += 1
                elif band == BAND_FALLBACK:
                    verdict = "未命中(兜底)"
                    stats["miss"] += 1
                else:
                    verdict = "误命中"
                    stats["miss"] += 1
        else:
            if band == BAND_FALLBACK:
                verdict = "兜底"
                stats["fallback"] += 1
            else:
                verdict = "待人工判断"
                stats["need_review"] += 1
            if flags:
                verdict += " ⚠"
        rows.append({
            "question": question, "expect": expect, "top": top, "band": band,
            "verdict": verdict, "flags": flags, "candidates": cands,
        })
    return {"rows": rows, "labeled": labeled, "stats": stats, "total": len(pairs)}


def print_questions(result: dict) -> None:
    print("=" * 92)
    print("批量问法检验" + ("（带标注，可直接算准确率）" if result["labeled"] else "（未标注，请人工扫 Top1）"))
    print("=" * 92)
    for i, row in enumerate(result["rows"], 1):
        top = row["top"]
        print(f"{i:>3}. {row['question']}")
        if top:
            print(f"     → 【{top['question'][:38]}】 分数 {top['score']:.3f} "
                  f"(词面 {top['lex_score']:.2f} / 语义 {top['vec_score']:.2f}) [{row['band']}] {row['verdict']}")
            if row["candidates"][1:]:
                others = "；".join(f"{c['question'][:20]}({c['score']:.2f})" for c in row["candidates"][1:])
                print(f"       次选：{others}")
        else:
            print(f"     → （无候选）{row['verdict']}")
        if row["flags"]:
            print(f"       ⚠ 复核点：{'、'.join(row['flags'])}")
    print("-" * 92)
    s = result["stats"]
    if result["labeled"]:
        denom = max(1, s["hit"] + s["miss"])
        print(f"命中 {s['hit']} | 未命中/误命中 {s['miss']} | 准确率 {s['hit'] / denom * 100:.1f}%")
    else:
        print(f"待人工判断 {s['need_review']} | 兜底 {s['fallback']} | 共 {result['total']} 条")
        print("人工判断方法：只看「Top1 是不是用户真正想问的那件事」，是→命中，不是→误命中。")


# ==================================================================== 会话回放
def eval_from_sessions(limit: int, entries: list[dict]) -> dict:
    """回放真实会话里的用户提问。

    注意：kb_id / answer_type 记在**该提问之后的机器人回复**上（用户消息本身不带命中信息），
    所以这里用子查询取「同一会话里紧跟在它之后的第一条机器人消息」。
    同时过滤掉「（请求转人工）」这类系统写入的标记，它们不是真实提问。
    """
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT u.content AS q, u.id AS uid,"
            " (SELECT b.kb_id FROM chat_message b WHERE b.session_id = u.session_id"
            "   AND b.role = 'bot' AND b.id > u.id ORDER BY b.id LIMIT 1) AS old_kb,"
            " (SELECT b.answer_type FROM chat_message b WHERE b.session_id = u.session_id"
            "   AND b.role = 'bot' AND b.id > u.id ORDER BY b.id LIMIT 1) AS old_band"
            " FROM chat_message u"
            " WHERE u.role = 'user' AND length(u.content) >= 2"
            "   AND u.content NOT LIKE '（%'"
            " ORDER BY u.id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    changed = 0
    skipped = 0
    for r in rows:
        q = r["q"]
        # 寒暄/致谢/转人工/转接中这类消息走的是规则或人工链路，不参与知识检索，
        # 回放时没有可比性，直接跳过（否则报告里全是假"变化"）
        if r["old_band"] in RULE_ONLY_TYPES:
            skipped += 1
            continue
        cands = knowledge.retrieve(q, entries, top_k=1)
        top = cands[0] if cands else None
        now_id = top["id"] if top else None
        same = (now_id == r["old_kb"])
        if not same:
            changed += 1
        out.append({
            "question": q, "old_kb": r["old_kb"], "new_kb": now_id,
            "old_band": r["old_band"] or "（当时未命中知识库）", "same": same,
            "new_question": top["question"] if top else "（无候选）",
            "new_band": band_of(top),
        })
    return {"rows": out, "changed": changed, "total": len(out), "skipped": skipped}


def print_sessions(result: dict) -> None:
    print("=" * 92)
    print("真实会话回放（用当前知识库重新检索，与当时的命中对比）")
    print("=" * 92)
    print(f"回放 {result['total']} 条真实提问 | 命中结果发生变化 {result['changed']} 条"
          f" | 跳过 {result.get('skipped', 0)} 条规则类消息（寒暄/转人工等，不参与知识检索）")
    print()
    for row in result["rows"]:
        mark = "＝" if row["same"] else "≠"
        print(f"  {mark} 「{row['question'][:34]}」")
        print(f"      当时 → {row['old_band']} (kb={row['old_kb']}) | "
              f"现在 → {row['new_band']} 【{row['new_question'][:34]}】")
    print()
    print("用法：改动知识库或阈值前后各跑一次，看「≠」的条目是变好了还是变差了。")


# ==================================================================== 模板
def export_template(path: Path, entries: list[dict]) -> None:
    lines = [
        "# 检索命中检验模板",
        "# 格式说明：",
        "#   带标注（推荐）：问题 <TAB> 期望命中的关键词或条目ID     ← 能直接算准确率",
        "#   不带标注：只写问题                                     ← 输出 Top3 供人工快速抽检",
        "# 建议先填 30~50 条**真实用户问法**（越口语越好，不要照抄标准问）。",
        "",
    ]
    for entry in entries[:12]:
        lines.append(f"{entry['question']}\t{entry['id']}")
    lines.append("")
    lines.append("# ↓ 换成你自己的真实问法（下面几条演示了不带标注的写法）")
    for entry in entries[12:20]:
        similar = (entry.get("similar") or [entry["question"]])[0]
        lines.append(f"{similar}")
    path.parent.mkdir(parents=True, exist_ok=True)
    io.open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print(f"已生成模板：{path}")
    print("把里面的问题换成你自己的真实问法（第一列），有把握的再补第二列期望命中，然后跑：")
    print(f"  python scripts/eval_retrieval.py --questions {path}")


# ==================================================================== 报告
def write_report(path: Path, sections: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    io.open(path, "w", encoding="utf-8").write("\n\n".join(sections) + "\n")
    print(f"\n报告已写入：{path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="检索命中质量检验")
    parser.add_argument("--selfcheck", action="store_true", help="知识库自召回自检（无需真实数据）")
    parser.add_argument("--questions", type=str, help="批量问法文件（.txt/.tsv/.csv）")
    parser.add_argument("--labeled", action="store_true", help="问法文件是否带期望命中（两列）")
    parser.add_argument("--from-sessions", action="store_true", help="回放真实会话里的用户提问")
    parser.add_argument("--limit", type=int, default=100, help="回放条数上限")
    parser.add_argument("--export-template", type=str, help="导出自测模板文件")
    parser.add_argument("--report", type=str, help="把结果写成 Markdown 报告")
    args = parser.parse_args()

    db.init_db(seed=True)
    embedding.ensure_index()
    entries = db.list_knowledge(enabled=1)
    print(f"知识库 {len(entries)} 条 | 向量召回 provider = {embedding.current().label} "
          f"| 阈值 直接作答 ≥ {config.HIGH_CONFIDENCE}，作答+确认 ≥ {config.MID_CONFIDENCE}\n")

    sections: list[str] = []
    did_something = False

    if args.export_template:
        export_template(Path(args.export_template), entries)
        return 0

    if args.selfcheck:
        did_something = True
        result = selfcheck(entries)
        print_selfcheck(result, entries)
        lines = [
            "# 知识库召回自检报告", "",
            f"- 条目：{result['total_entries']} 条，测试问法：{result['total_queries']} 条",
            f"- 自召回率：**{result['recall'] * 100:.1f}%**（{result['total_hits']}/{result['total_queries']}）",
            f"- 完全可自召回条目：{result['perfect']}/{result['total_entries']}", "",
            "## 存在自召回问题的条目", "",
        ]
        for row in result["problematic"]:
            lines.append(f"### #{row['id']} {row['question']}")
            lines.append(f"自召回 {row['hits']}/{row['queries']}")
            for w in row["wrong"]:
                lines.append(f"- ✗ 「{w['query']}」→ 【{w['got']}】({w['score']:.3f} / {w['band']})")
            lines.append("")
        sections.append("\n".join(lines))

    if args.questions:
        did_something = True
        pairs = parse_questions(Path(args.questions))
        result = eval_questions(pairs, entries)
        print_questions(result)
        lines = ["# 批量问法检验", "",
                 f"- 共 {result['total']} 条，标注：{'是' if result['labeled'] else '否'}", ""]
        for row in result["rows"]:
            top = row["top"]
            lines.append(f"- **{row['question']}** → "
                         + (f"【{top['question']}】{top['score']:.3f} [{row['band']}] {row['verdict']}"
                            if top else f"（无候选）{row['verdict']}"))
        sections.append("\n".join(lines))

    if args.from_sessions:
        did_something = True
        result = eval_from_sessions(args.limit, entries)
        print_sessions(result)
        lines = ["# 真实会话回放", "",
                 f"- 回放 {result['total']} 条，命中变化 {result['changed']} 条", ""]
        for row in result["rows"]:
            lines.append(f"- {'＝' if row['same'] else '≠'} 「{row['question']}」 "
                         f"当时 {row['old_band']}(kb={row['old_kb']}) → 现在 {row['new_band']}")
        sections.append("\n".join(lines))

    if not did_something:
        parser.print_help()
        print("\n至少指定 --selfcheck / --questions / --from-sessions / --export-template 之一")
        return 1

    if args.report and sections:
        write_report(Path(args.report), sections)
    return 0


if __name__ == "__main__":
    sys.exit(main())
