"""一键启动脚本。

用法：
    python run.py                  # 默认 http://127.0.0.1:8000
    python run.py --port 9000      # 指定端口
    python run.py --reload         # 开发模式（改代码自动重载）

启用四角色权限（默认关闭，演示模式无需登录）：
    Windows :  $env:AUTH_ENABLED="1"; python run.py
    Linux   :  AUTH_ENABLED=1 python run.py
"""
from __future__ import annotations

import argparse

import uvicorn

from app import config, db, embedding


def main() -> None:
    parser = argparse.ArgumentParser(description="AI 智能客服 MVP")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=8000, help="监听端口，默认 8000")
    parser.add_argument("--reload", action="store_true", help="开发模式：代码变更自动重载")
    args = parser.parse_args()

    count = db.init_db(seed=True)
    base = f"http://{args.host}:{args.port}"
    print("=" * 68)
    print("  AI 智能客服 MVP")
    print("=" * 68)
    print(f"  ① 用户会话页   : {base}/          （提问 / 澄清 / 转人工 / 评价）")
    print(f"  ② 客服工作台   : {base}/agent     （接入 / 回复 / 常用语 / 上下文）")
    print(f"  ③ 数据看板     : {base}/dashboard （解决率 / 转人工率 / 满意度）")
    print(f"  ④ 运营后台     : {base}/admin     （知识库 / 文档导入 / 会话检索 / 配置）")
    print(f"  ⑤ 接口文档     : {base}/docs")
    print("-" * 68)
    print(f"  知识条目 : {count} 条        文档 : {len(db.list_documents())} 个")
    print(f"  数据库   : {config.DB_PATH}")
    print(f"  对话模式 : {'知识库检索 + 大模型润色' if config.llm_configured() else '纯离线知识库检索（未配置大模型 Key）'}")
    print(f"  向量召回 : {embedding.current().label}")
    print(f"  实时通道 : {'WebSocket 推送' if config.WS_ENABLED else '已关闭（前端退化为轮询）'}")
    print(f"  权限认证 : {'已启用（四角色，需登录）' if config.AUTH_ENABLED else '未启用（演示模式，全部权限）'}")
    print(f"  服务时间 : {db.get_bot_config().get('service_hours_text')}"
          f"  当前{'在线' if db.is_service_online() else '离线（转人工将提示留言）'}")
    print("=" * 68)

    uvicorn.run("app.main:app", host=args.host, port=args.port,
                reload=args.reload, log_level="info")


if __name__ == "__main__":
    main()
