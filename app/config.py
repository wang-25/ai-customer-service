"""全局配置。

所有配置项均可通过环境变量覆盖，默认值保证「零配置即可离线运行」。
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------- 数据存储
DATA_DIR = Path(os.getenv("APP_DATA_DIR", BASE_DIR / "data"))
DB_PATH = Path(os.getenv("APP_DB_PATH", DATA_DIR / "app.db"))
UPLOAD_DIR = Path(os.getenv("APP_UPLOAD_DIR", DATA_DIR / "uploads"))

# ---------------------------------------------------------------- 检索阈值
HIGH_CONFIDENCE = float(os.getenv("KB_HIGH_CONFIDENCE", "0.52"))  # >= 该值：直接作答
MID_CONFIDENCE = float(os.getenv("KB_MID_CONFIDENCE", "0.30"))    # >= 该值：作答 + 主动确认
TOP_K = int(os.getenv("KB_TOP_K", "3"))
CONTEXT_HISTORY_TURNS = int(os.getenv("CONTEXT_HISTORY_TURNS", "3"))

# ---------------------------------------------------------------- 向量召回（Embedding）
# provider:
#   auto  —— 配了 EMBEDDING_API_KEY 就用 api，否则用 local（默认）
#   api   —— OpenAI 兼容 /embeddings 接口，真正的语义向量
#   local —— 离线「同义词图 + 共现传播」稀疏向量，零额外依赖
#   off   —— 关闭向量召回，退化为纯词面检索
EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "auto").lower()
EMBEDDING_API_KEY = (os.getenv("EMBEDDING_API_KEY") or os.getenv("LLM_API_KEY")
                     or os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY") or "")
EMBEDDING_BASE_URL = os.getenv(
    "EMBEDDING_BASE_URL", os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1"))
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
EMBEDDING_TIMEOUT = float(os.getenv("EMBEDDING_TIMEOUT", "20"))
EMBEDDING_BATCH = int(os.getenv("EMBEDDING_BATCH", "32"))

# 向量分与词面分的融合： final = max(lexical, VECTOR_WEIGHT × cosine) + 双通道一致加成
# 阈值经实测标定：离线同义词图向量下，真正相关的查询余弦约 0.70~1.00，
# 无关查询约 0.30~0.45，因此用 0.55 作为「向量分是否可信」的门槛。
VECTOR_WEIGHT = float(os.getenv("VECTOR_WEIGHT", "0.82"))
VECTOR_MIN_COSINE = float(os.getenv("VECTOR_MIN_COSINE", "0.55"))
VECTOR_AGREE_BONUS = float(os.getenv("VECTOR_AGREE_BONUS", "0.06"))   # 两路都命中时的加成
# 仅靠向量命中（词面分不达标）时，是否必须先向用户确认再给答案。
# 这是防止离线向量"自信答错"的关键策略开关（见 app/dialog.py）。
VECTOR_ONLY_REQUIRE_CONFIRM = os.getenv("VECTOR_ONLY_REQUIRE_CONFIRM", "1") not in ("0", "false", "False")

# ---------------------------------------------------------------- WebSocket
WS_ENABLED = os.getenv("WS_ENABLED", "1") not in ("0", "false", "False")
WS_HEARTBEAT_SECONDS = int(os.getenv("WS_HEARTBEAT_SECONDS", "25"))

# ---------------------------------------------------------------- 会话生命周期
# 用户无消息超过该秒数 → 提示一次；再超过则自动结束（超时关闭）
SESSION_IDLE_HINT_SECONDS = int(os.getenv("SESSION_IDLE_HINT_SECONDS", "300"))     # 5 分钟
SESSION_TIMEOUT_SECONDS = int(os.getenv("SESSION_TIMEOUT_SECONDS", "600"))         # 10 分钟
# 转人工排队等待超过该秒数 → 提示用户并记录体验风险事件
QUEUE_WARN_SECONDS = int(os.getenv("QUEUE_WARN_SECONDS", "60"))

# ---------------------------------------------------------------- 文档导入
CHUNK_MIN_CHARS = int(os.getenv("CHUNK_MIN_CHARS", "200"))
CHUNK_MAX_CHARS = int(os.getenv("CHUNK_MAX_CHARS", "600"))

# ---------------------------------------------------------------- 权限
# 默认关闭：本地演示无需登录（此时所有请求按「管理员」处理）。
# 设为 1 后启用四角色权限：admin / supervisor / agent / editor
AUTH_ENABLED = os.getenv("AUTH_ENABLED", "0") not in ("0", "false", "False")
TOKEN_TTL_SECONDS = int(os.getenv("TOKEN_TTL_SECONDS", "86400"))
TOKEN_SECRET = os.getenv("TOKEN_SECRET", "mvp-demo-secret-change-me")

ROLES = ["admin", "supervisor", "agent", "editor"]
ROLE_LABELS = {
    "admin": "管理员",
    "supervisor": "客服主管",
    "agent": "客服",
    "editor": "知识运营",
}

# ---------------------------------------------------------------- 可选大模型
LLM_API_KEY = os.getenv("LLM_API_KEY") or os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "20"))
LLM_ENABLED = os.getenv("LLM_ENABLED", "1") not in ("0", "false", "False")


def llm_configured() -> bool:
    """是否配置了大模型。未配置则走纯离线检索模式。"""
    return bool(LLM_ENABLED and LLM_API_KEY)


# ================================================================ 默认机器人配置
DEFAULT_BOT_CONFIG: dict = {
    "bot_name": "智能客服助手",
    "bot_avatar": "AI",
    "welcome": (
        "您好！我是智能客服助手 😊\n"
        "您可以直接用一句话描述问题，例如「怎么退货」「多久能发货」。"
    ),
    "ending": "感谢您的咨询，祝您购物愉快！",
    "fallback": "抱歉，我暂时没有找到匹配的答案 🤔",
    "handoff": "好的，正在为您转接人工客服，请稍候。",
    "handoff_offline": "当前非人工服务时间（{hours}），您可以继续问我，或留下联系方式由客服在工作时间回复您。",
    "handoff_trigger_prefix": "很抱歉给您带来不好的体验，正在为您转接人工客服。",
    "handoff_suggest": "看起来这个问题我暂时帮不上忙，您可以回复「转人工」由人工客服协助处理。",
    "quick_questions": ["怎么退货", "多久能发货", "怎么开发票", "会员怎么升级"],
    # 服务时段：key 为 0=周日 … 6=周六，值为 [["09:00","21:00"]] 列表，空列表表示当天不服务
    "service_hours": {
        "0": [["09:00", "21:00"]],
        "1": [["09:00", "21:00"]],
        "2": [["09:00", "21:00"]],
        "3": [["09:00", "21:00"]],
        "4": [["09:00", "21:00"]],
        "5": [["09:00", "21:00"]],
        "6": [["09:00", "21:00"]],
    },
    "service_hours_text": "每日 09:00-21:00",
}


# ================================================================ 转人工触发规则
DEFAULT_TRIGGER_RULES: list[dict] = [
    {"rule_type": "fallback_count", "label": "连续兜底次数", "threshold": 2,
     "enabled": 1, "priority": 1,
     "desc": "机器人连续 N 次未匹配到答案时，主动转人工"},
    {"rule_type": "sentiment", "label": "负面情绪词命中数", "threshold": 2,
     "enabled": 1, "priority": 2,
     "desc": "单条消息命中 N 个及以上负面情绪词时转人工"},
    {"rule_type": "risky_keyword", "label": "高风险关键词命中数", "threshold": 1,
     "enabled": 1, "priority": 3,
     "desc": "命中投诉/曝光/起诉等高风险关键词时强制转人工"},
    {"rule_type": "turn_limit", "label": "会话轮次上限", "threshold": 15,
     "enabled": 1, "priority": 4,
     "desc": "单会话超过 N 轮仍未解决时转人工"},
    {"rule_type": "clarify_limit", "label": "澄清轮次上限", "threshold": 3,
     "enabled": 1, "priority": 5,
     "desc": "连续澄清超过 N 轮仍无法作答时转人工"},
]

SENTIMENT_WORDS = [
    "生气", "气死", "骗子", "骗人", "垃圾", "太差", "差评", "无语", "恶心",
    "坑人", "什么破", "失望", "服了", "气人", "火大", "离谱", "过分",
]

RISKY_WORDS = [
    "投诉", "曝光", "起诉", "律师", "监管", "工商", "12315", "媒体",
    "举报", "法院", "消协", "仲裁", "报警",
]

# ================================================================ 槽位澄清规则
# M2-3 多轮槽位澄清：命中「需要澄清」的知识条目时，先追问槽位，再结合槽位重新检索。
# when_keywords：命中条目的问法/关键词中包含其中任一词即触发
SLOT_RULES: list[dict] = [
    {
        "id": "return_reason",
        "name": "退货原因",
        "when_keywords": ["运费", "邮费"],
        "when_category": "售后政策",
        "question": "为了给您准确的答复，请问这次退货的原因是？",
        "options": [
            {"label": "商品质量问题", "keywords": ["质量问题"]},
            {"label": "个人原因（不喜欢 / 拍错）", "keywords": ["个人原因"]},
            {"label": "七天无理由", "keywords": ["无理由"]},
        ],
    },
    {
        "id": "member_level",
        "name": "会员等级",
        "when_keywords": ["权益", "会员"],
        "when_category": "会员优惠",
        "question": "请问您目前是哪个会员等级呢？",
        "options": [
            {"label": "银卡", "keywords": ["银卡"]},
            {"label": "金卡", "keywords": ["金卡"]},
            {"label": "黑卡", "keywords": ["黑卡"]},
        ],
    },
    {
        "id": "invoice_type",
        "name": "发票类型",
        "when_keywords": ["发票", "开票"],
        "when_category": "支付发票",
        "question": "请问您需要哪种发票？",
        "options": [
            {"label": "电子普通发票", "keywords": ["电子发票"]},
            {"label": "增值税专用发票", "keywords": ["专票"]},
        ],
    },
]

# ================================================================ 会话状态
SESSION_STATUS = {
    "bot": "机器人接待中",
    "queueing": "转人工排队",
    "agent": "人工接待中",
    "closed": "已结束",
    "timeout": "已超时关闭",
}
ACTIVE_STATUS = ("bot", "queueing", "agent")
