"""生成演示数据：制造一批跨天的真实会话，让看板与后台有内容可演示。

用法：
    python scripts/seed_demo_data.py              # 默认生成 60 个会话，分布在近 7 天
    python scripts/seed_demo_data.py --count 120 --days 14
    python scripts/seed_demo_data.py --reset      # 先清空会话数据再生成

会真实调用对话引擎，因此产生的 answer_type / 溯源 / 转人工记录 / 评价都是真实数据，
不是硬编码的假数据。
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, dialog, handoff  # noqa: E402

# 覆盖各类结局的提问样本
QUESTIONS = [
    "我想退货，怎么操作", "那运费谁出呢", "商品质量问题", "个人原因",
    "多久能发货", "怎么查询物流", "没收到货但显示签收了", "能改收货地址吗",
    "退款多久到账", "七天无理由退货条件是什么", "怎么换货", "保修期多久",
    "怎么开发票", "增值税专用发票怎么开", "支持哪些支付方式", "支付失败怎么办",
    "忘记密码了", "如何修改绑定手机号", "会员怎么升级", "黑卡",
    "优惠券怎么用", "优惠券过期了能补发吗", "支持试用吗", "你们的产品怎么收费",
    "能和企业微信集成吗", "数据安全怎么保障", "发什么快递", "订单一直没更新",
    "你们太差了，我要投诉", "气死我了，什么破服务", "我要转人工",
    "帮我写一首诗", "今天天气怎么样", "给我订张机票",
]
CSAT_BAD_REASONS = ["答案不准确", "没解决我的问题", "回复太慢", "态度问题"]
COMMENTS = ["", "还不错", "回答有点慢", "问题没解决", "谢谢"]


def backdate_session(sid: str, created_ts: float, rng: random.Random) -> None:
    """把会话与其消息的时间整体前移，制造跨天分布。"""
    session = db.get_session(sid)
    if not session:
        return
    base_created = session["created_at"]
    delta = db.parse_ts(base_created) - created_ts
    if delta <= 0:
        return

    def shift(value: str | None, extra: float = 0.0) -> str | None:
        if not value:
            return None
        return time.strftime("%Y-%m-%d %H:%M:%S",
                             time.localtime(db.parse_ts(value) - delta + extra))

    with db.connect() as conn:
        conn.execute(
            "UPDATE chat_session SET created_at=?, updated_at=?, queue_at=?, assigned_at=?,"
            " first_bot_at=?, first_reply_at=?, ended_at=?, last_user_at=? WHERE id=?",
            (
                shift(session["created_at"]),
                shift(session["updated_at"]),
                shift(session.get("queue_at")),
                shift(session.get("assigned_at")),
                shift(session.get("first_bot_at")),
                shift(session.get("first_reply_at")),
                shift(session.get("ended_at")),
                shift(session.get("last_user_at")),
                sid,
            ),
        )
        # 消息时间按真实节奏铺开：机器人在 1-2 秒内回复，用户下一轮间隔 10-60 秒，
        # 这样「机器人首次响应」等指标才符合真实分布（不能等距铺开，否则指标会失真）
        rows = conn.execute(
            "SELECT id, role FROM chat_message WHERE session_id = ? ORDER BY id", (sid,)
        ).fetchall()
        clock = db.parse_ts(session["created_at"]) - delta
        for idx, row in enumerate(rows):
            if row["role"] == "user":
                if idx > 0:                      # 首个用户消息与会话创建同时刻
                    clock += rng.randint(4, 26)
            else:
                clock += rng.randint(1, 3)       # 机器人在 1-3 秒内回复
            conn.execute(
                "UPDATE chat_message SET created_at = ? WHERE id = ?",
                (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(clock)), row["id"]),
            )
        conn.execute(
            "UPDATE handoff_log SET created_at = ?, queue_at = ?, assigned_at = ? WHERE session_id = ?",
            (shift(session.get("queue_at")) or shift(session["created_at"]),
             shift(session.get("queue_at")), shift(session.get("assigned_at")), sid),
        )
        fb = conn.execute(
            "SELECT id, created_at FROM fallback_log WHERE session_id = ?", (sid,)
        ).fetchall()
        for row in fb:
            shifted = shift(row["created_at"])
            if shifted:
                conn.execute("UPDATE fallback_log SET created_at = ? WHERE id = ?",
                             (shifted, row["id"]))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=60)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--reset", action="store_true", help="先清空历史会话数据")
    parser.add_argument("--seed", type=int, default=20261002)
    args = parser.parse_args()

    db.init_db(seed=True)
    rng = random.Random(args.seed)

    if args.reset:
        with db.connect() as conn:
            for table in ("chat_message", "chat_session", "handoff_log",
                          "fallback_log", "agent_status_log"):
                conn.execute(f"DELETE FROM {table}")
        db.init_db(seed=True)
        print("[重置] 已清空会话相关数据")

    agents = db.list_agents()
    if not agents:
        print("没有客服账号，请先启动过一次服务（会自动创建演示客服）")
        return 1
    online = [a for a in agents if a["status"] in ("online", "busy")] or agents[:1]
    for a in online:
        db.set_agent_status(a["id"], "online")
        db.update_agent(a["id"], max_concurrent=999)   # 造数据阶段不受接待上限限制
    print(f"[准备] 在线客服 {len(online)} 位：{'、'.join(a['name'] for a in online)}")

    now = time.time()
    created = 0
    # 预留一批「刚刚发生」的会话，保证演示时工作台与看板都有进行中的会话
    keep_active = max(4, args.count // 12)
    for i in range(args.count):
        recent = i < keep_active
        if recent:
            created_ts = now - rng.randint(60, 480)
        else:
            # 时间分布在近 N 天的工作时段内
            day_offset = rng.randint(0, max(0, args.days - 1))
            hour = rng.randint(9, 20)
            minute = rng.randint(0, 59)
            when = now - day_offset * 86400
            local = time.localtime(when)
            created_ts = time.mktime((
                local.tm_year, local.tm_mon, local.tm_mday, hour, minute, rng.randint(0, 59),
                0, 0, -1))

        sid = db.create_session(visitor_id=f"demo-{rng.randint(1000, 9999)}")["id"]
        # 每个会话问 1~4 轮
        turns = rng.choices([1, 2, 3, 4], weights=[35, 30, 22, 13])[0]
        asked: list[str] = []
        for _ in range(turns):
            q = rng.choice(QUESTIONS)
            if asked and q == asked[-1]:
                continue
            asked.append(q)
            try:
                dialog.handle_message(sid, q)
            except Exception:  # noqa: BLE001 - 造数据不允许中断
                pass

        state = db.get_session(sid)
        # 结局抽样：机器人解决 / 转人工 / 未解决
        roll = rng.random()
        if recent:
            pass  # 刚发生的会话保持进行中，供工作台演示
        elif state and state["status"] == "agent":
            # 已在人工接待中：让客服回一句再结束
            agent = online[rng.randrange(len(online))]
            handoff.agent_reply(sid, agent["id"],
                                rng.choice([
                                    "您好，我已经在为您核实，请稍等片刻。",
                                    "这边帮您查询到订单状态了，稍后短信通知您。",
                                    "已经为您提交处理，预计 24 小时内完成。",
                                    "很抱歉给您带来不便，我马上帮您处理。",
                                ]))
            handoff.close_session(sid, end_reason="agent", agent_id=agent["id"])
        elif roll < 0.85:
            db.finish_session(sid, end_reason="user")
        else:
            db.finish_session(sid, end_reason="timeout")

        # 满意度：约 35% 的已结束会话参评，其中 80% 好评
        state = db.get_session(sid)
        if state and state["status"] in ("closed", "timeout") and rng.random() < 0.35:
            if rng.random() < 0.8:
                db.set_csat(sid, 1, "", rng.choice(COMMENTS))
            else:
                db.set_csat(sid, -1, rng.choice(CSAT_BAD_REASONS), rng.choice(COMMENTS))

        backdate_session(sid, created_ts, rng)
        created += 1

    # 还原接待上限
    for a in online:
        db.update_agent(a["id"], max_concurrent=3, current_load=0)
    with db.connect() as conn:
        conn.execute("UPDATE agent SET current_load = 0")

    print(f"[完成] 生成 {created} 个会话，分布在前 {args.days} 天")
    print("       打开 http://127.0.0.1:8000/dashboard 查看看板")
    print("       打开 http://127.0.0.1:8000/admin 查看会话记录")
    return 0


if __name__ == "__main__":
    sys.exit(main())
