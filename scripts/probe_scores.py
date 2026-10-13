"""检索效果自测：打印一批真实问法在知识库中的命中分数，用于阈值标定。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db, knowledge  # noqa: E402

QUESTIONS = [
    # (测试问法, 期望命中的标准问关键词)
    ("我想退货，怎么操作", "退货流程"),
    ("那运费谁出呢", "运费"),
    # 期望值以 "~" 开头 = 信息性用例：这句话脱离上下文本身就有歧义，
    # 系统会用「您是想问…吗」的确认前缀兜住，不做严格判定。
    ("~那要多久", None),
    ("我要退钱", "退货"),
    ("多久能退款到账", "退款"),
    ("买了之后几天能发出来", "发货"),
    ("快递一直没更新怎么办", "物流"),
    ("没收到货但显示签收了", "签收"),
    ("能开专票吗", "发票"),
    ("密码忘记了登不上", "密码"),
    ("怎么改绑手机号", "手机号"),
    ("会员怎么升级", "会员"),
    ("优惠券过期了", "优惠券"),
    ("可以试用吗", "试用"),
    ("你们安全吗", "安全"),
    ("今天天气怎么样", None),  # 应进入兜底
    ("帮我写一首诗", None),    # 应进入兜底
]


def main() -> None:
    db.init_db(seed=True)
    entries = db.list_knowledge(enabled=1)
    print(f"知识库条目数: {len(entries)}")
    print(f"阈值: HIGH={config.HIGH_CONFIDENCE}  MID={config.MID_CONFIDENCE}")
    print("-" * 100)
    ok = 0
    for raw_q, expect in QUESTIONS:
        informational = raw_q.startswith("~")
        q = raw_q.lstrip("~")
        # 本脚本专门标定「词面通道」的阈值分布，因此显式关闭向量召回；
        # 向量通道的效果对照请看 scripts/probe_vector.py
        cands = knowledge.retrieve(q, entries, top_k=3, use_vector=False)
        top = cands[0] if cands else None
        level = "兜底"
        if top:
            level = "直接作答" if top["score"] >= config.HIGH_CONFIDENCE else (
                "作答+确认" if top["score"] >= config.MID_CONFIDENCE else "兜底"
            )
        hit = bool(expect and top and expect in top["question"])
        miss = bool(expect is None and top and top["score"] >= config.MID_CONFIDENCE)
        if informational:
            flag, passed = "-- ", True
        else:
            passed = (hit or (expect is None and level == "兜底")) and not miss
            flag = "OK " if passed else "!! "
        if passed:
            ok += 1
        print(f"{flag}[{level}] {q!r:32} -> {top['question'] if top else 'None':28} score={top['score'] if top else 0}")
    print("-" * 100)
    print(f"通过: {ok}/{len(QUESTIONS)}")


if __name__ == "__main__":
    main()
