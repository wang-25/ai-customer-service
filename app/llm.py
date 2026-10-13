"""可选的大模型适配层。

默认不启用：未配置任何 API Key 时，系统以纯离线知识库检索模式运行，
所有功能（问答、多轮、后台管理）完全可用。

配置了 Key 后，大模型仅用于「基于检索到的知识片段润色/组织答案」，
不参与知识决策（决策仍由检索分数决定），从架构上避免幻觉。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from . import config

SYSTEM_PROMPT = (
    "你是一名严谨的电商客服助手。只能依据【参考资料】回答用户问题。"
    "资料未覆盖的内容必须回答「抱歉，这一点我暂时没有查到」，严禁编造。"
    "不得承诺具体赔付金额、时效保证或法律责任。"
    "回答要求：口语化、简洁、分点不超过 3 条、不超过 150 字。"
)


def _post(payload: dict) -> dict | None:
    req = urllib.request.Request(
        f"{config.LLM_BASE_URL.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.LLM_API_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=config.LLM_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError):
        return None


def polish_answer(question: str, kb_question: str, kb_answer: str, history: list[str]) -> str | None:
    """基于检索到的知识片段组织回答。任何异常都返回 None，由调用方回退到知识库原文。"""
    if not config.llm_configured():
        return None

    history_text = "\n".join(f"- {h}" for h in history[-3:]) or "（无）"
    user_prompt = (
        f"【参考资料】\n问：{kb_question}\n答：{kb_answer}\n\n"
        f"【最近对话】\n{history_text}\n\n"
        f"【用户当前问题】{question}\n\n"
        "请依据参考资料给出回答。"
    )
    data = _post(
        {
            "model": config.LLM_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 400,
        }
    )
    if not data:
        return None
    try:
        text = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        return None
    return text or None
