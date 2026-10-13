"""向量召回效果对比：同一批「同义表达」查询，在关闭/开启向量召回下的命中差异。

用法：
    python scripts/probe_vector.py          # 默认 local provider
    python scripts/probe_vector.py --api    # 用在线 Embedding 接口（需配置 Key）

输出每一句的：
    词面分 / 向量分 / 融合分 / 命中条目标题，
并给出「纯词面是否会兜底、开启向量后是否被正确召回」的对照结论。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db, embedding, knowledge  # noqa: E402

# (用户口语化问法, 期望命中的标准问里的关键词)
CASES = [
    ("钱什么时候回来", "退款"),
    ("我要退钱", "退货"),
    ("退钱到哪了", "退款"),
    ("快递走到哪了", "物流"),
    ("怎么退", "退货"),
    ("登不上去了", "密码"),
    ("开个票", "发票"),
    ("贵不贵", "收费"),
    ("坏了能修吗", "保修"),
    ("换一个行不行", "换货"),
    ("我想把东西退了", "退货"),
    ("这个多少钱", "收费"),
    ("东西还没到", "没有收到"),
    # 知识库里没有「分期」相关条目，因此期望它进入兜底（并在猜你想问里给出支付方式）
    ("可以分期吗", None),
    # 应当进入兜底的无关问题
    ("今天天气怎么样", None),
    ("帮我写首诗", None),
    ("推荐一部电影", None),
    # 信息性用例（前缀 ~）：错别字 + 过短的查询，离线向量可能误命中。
    # 期望的不是"答对"，而是**不要给出自信的错误答案**——落到「作答+确认」而不是「直接作答」。
    ("~分数能换东西吗", None),
    ("~那个东西", None),
]


def run(use_vector: bool, entries: list[dict]) -> list[dict]:
    """用与线上一致的口径判定等级：仅靠向量命中时强制降级为「作答+确认」。"""
    out = []
    for raw_query, expect in CASES:
        informational = raw_query.startswith("~")
        query = raw_query.lstrip("~")
        cands = knowledge.retrieve(query, entries, top_k=1, use_vector=use_vector)
        top = cands[0] if cands else None
        level = "兜底"
        if top:
            high = top["score"] >= config.HIGH_CONFIDENCE
            if high and not (top.get("vector_only") and config.VECTOR_ONLY_REQUIRE_CONFIRM):
                level = "直接作答"
            elif top["score"] >= config.MID_CONFIDENCE:
                level = "作答+确认"
        out.append({
            "query": query, "expect": expect, "top": top, "level": level,
            "informational": informational,
            # 信息性用例只要求"不给出自信的错误答案"（不能是直接作答）
            "ok": True if informational else (
                (expect is None and level == "兜底")
                or (expect is not None and top is not None
                    and level != "兜底" and expect in top["question"])),
        })
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", action="store_true", help="强制使用在线 Embedding 接口")
    args = parser.parse_args()

    if args.api:
        config.EMBEDDING_PROVIDER = "api"
        embedding._state["provider"] = None
    db.init_db(seed=True)
    entries = db.list_knowledge(enabled=1)

    provider = embedding.current()
    print("=" * 104)
    print(f"知识条目 {len(entries)} 条 | provider = {provider.name}（{provider.label}）")
    if provider.name == "api" and not provider.available():
        print("⚠️  未配置 EMBEDDING_API_KEY，已自动退回离线 provider")
        provider = embedding.current()
    rebuilt = embedding.build_index(entries, force=True)
    print(f"索引构建：{rebuilt}")
    print("=" * 104)

    plain = run(False, entries)
    vec = run(True, entries)

    print(f"{'用户问法':<18}{'期望':<8}{'纯词面命中':<20}{'融合后命中':<22}{'词面分':>7}{'向量分':>7}{'结论':>8}")
    print("-" * 104)
    improved = regressed = 0
    for a, b in zip(plain, vec):
        name_a = (a["top"]["question"][:16] if a["top"] else "—")
        name_b = (b["top"]["question"][:18] if b["top"] else "—")
        vs = b["top"]["vec_score"] if b["top"] else 0
        ls = b["top"]["lex_score"] if b["top"] else 0
        mark = "（信息性）" if b["informational"] else ""
        if not b["informational"]:
            if not a["ok"] and b["ok"]:
                mark, improved = "↑ 改善", improved + 1
            elif a["ok"] and not b["ok"]:
                mark, regressed = "↓ 退化", regressed + 1
        print(f"{b['query']:<18}{(b['expect'] or '应兜底'):<8}{name_a:<20}{name_b:<22}"
              f"{ls:>7.3f}{vs:>7.3f}{mark:>8}")

    print("-" * 104)
    print(f"纯词面通过 {sum(1 for x in plain if x['ok'])}/{len(CASES)}"
          f" | 融合后通过 {sum(1 for x in vec if x['ok'])}/{len(CASES)}"
          f" | 改善 {improved} 例，退化 {regressed} 例")
    bad = [x for x in vec if not x["ok"]]
    if bad:
        print("融合后仍未通过：")
        for x in bad:
            top = x["top"]["question"] if x["top"] else "—"
            print(f"  - {x['query']}（期望含「{x['expect'] or '应兜底'}」）"
                  f"→ 实际 [{x['level']}] {top}")
    info = [(a, b) for a, b in zip(plain, vec) if b["informational"]]
    if info:
        print("信息性用例（错别字/过短查询，只要求不给出自信的错误答案）：")
        for _a, b in info:
            top = b["top"]["question"] if b["top"] else "—"
            safe = b["level"] != "直接作答"
            print(f"  - {b['query']} → [{b['level']}] {top}"
                  f"（{'安全：会先向用户确认' if safe else '⚠️ 直接作答，存在自信答错风险'}）")
    if regressed:
        print("退化用例（原本对、现在错）：")
        for a, b in zip(plain, vec):
            if a["ok"] and not b["ok"]:
                print(f"  - {b['query']}：纯词面={a['level']}，融合后={b['level']}")
    print("=" * 104)
    return 0 if regressed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
