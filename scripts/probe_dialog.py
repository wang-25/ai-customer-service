"""多轮上下文效果自测（进程内直连对话引擎，用于快速调参）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, dialog  # noqa: E402

DIALOGS = [
    ["我想退货，怎么操作", "那运费谁出呢", "那要多久"],
    ["多久能发货", "可以改地址吗", "那运费呢"],
    ["你好", "怎么开发票", "谢谢"],
    ["帮我订一张明天去上海的机票"],  # 应兜底，且第 2 次兜底提示转人工
]


def main() -> None:
    db.init_db(seed=True)
    for idx, script in enumerate(DIALOGS, 1):
        session = db.create_session()
        print("=" * 96)
        print(f"对话 {idx}   会话ID={session['id']}")
        print("=" * 96)
        for turn, msg in enumerate(script, 1):
            r = dialog.handle_message(session["id"], msg)
            matched = r.get("matched") or {}
            print(f"  第{turn}轮 用户: {msg}")
            print(f"        机器人[{r['answer_type']}] 上下文={'是' if r['context_used'] else '否'} "
                  f"命中={matched.get('question')} score={matched.get('score')}")
            print(f"        回复: {r['reply'].splitlines()[0][:70]}")
        print()


if __name__ == "__main__":
    main()
