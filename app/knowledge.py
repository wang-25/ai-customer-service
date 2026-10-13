"""知识库检索引擎（纯离线，无外部依赖）。

算法：字符 n-gram 相似度 + 关键词命中 的加权混合检索。
不依赖向量模型，也不依赖任何外部服务，保证「零配置可运行」。

打分公式：
    score = 0.68 * text_sim + 0.32 * keyword_score
    text_sim  = 0.65 * bigram_dice + 0.35 * unigram_dice   （在标准问与全部相似问中取最大值）
    keyword_score = min(1, 命中关键词数 / min(关键词总数, 3))

另外两个短路规则：
    1. 标准问 / 相似问 与用户问题互为子串  → 直接给 0.97（精确命中）
    2. 命中同义词组时，两侧文本都会补充规范词，提升召回
"""
from __future__ import annotations

import os
import re
from typing import Any, Iterable

from . import config

# ------------------------------------------------------------------ 文本归一化
_PUNCT = re.compile(
    "[\\s\u3000，。！？、；：\u201c\u201d\u2018\u2019（）《》【】"
    ",.!?;:\"'()\\[\\]<>~`@#$%^&*_+=|\\\\/-]+"
)
_FULL2HALF = {ord(c) + 0xFEE0: ord(c) for c in "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"}


def normalize(text: str) -> str:
    """全角转半角、转小写、去标点与空白。"""
    text = (text or "").translate(_FULL2HALF).lower()
    return _PUNCT.sub("", text)


def char_ngrams(text: str, n: int) -> set[str]:
    if len(text) < n:
        return {text} if text else set()
    return {text[i : i + n] for i in range(len(text) - n + 1)}


def dice(a: set[str], b: set[str]) -> float:
    """Dice 相似度，对短文本比 Jaccard 更平滑。"""
    if not a or not b:
        return 0.0
    return 2.0 * len(a & b) / (len(a) + len(b))


# ------------------------------------------------------------------ 同义词
# 每组第一个词为规范词，其余为同义变体。
# 匹配规则：**最长优先 + 跨度不重叠** ——「多少钱」会优先占用「钱」的位置，
# 因此不会因为短词「钱」把「多少钱」也扩展到退款组。
SYNONYM_GROUPS: list[list[str]] = [
    ["退货", "退换", "退掉", "退单", "退了", "无理由", "申请退货", "想退"],
    ["退款", "退钱", "返款", "退费", "退回来", "退回", "钱什么时候回来", "钱多久回来",
     "什么时候退钱", "钱", "退钱到哪"],
    ["物流", "快递", "配送", "寄送", "包裹", "单号", "轨迹", "物流信息", "走到哪了"],
    ["发货", "发出", "寄出", "出货", "什么时候发", "发货时间", "几天能到", "发出时间"],
    ["运费", "邮费", "快递费", "快递运费", "谁承担", "谁出", "谁付", "运费谁出"],
    ["发票", "开票", "票据", "报销凭证", "开个票", "开票资料"],
    ["价格", "收费", "多少钱", "费用", "价位", "报价", "贵不贵", "怎么卖", "价格表"],
    ["试用", "免费体验", "体验", "demo", "先试试", "免费版"],
    ["密码", "口令", "登录密码", "登陆密码", "登不上", "登录不上"],
    ["会员", "vip", "等级", "会员卡", "积分", "会员权益"],
    ["优惠券", "代金券", "优惠码", "券", "满减券", "折扣券"],
    ["地址", "收货地址", "收货信息", "收件地址", "地址写错", "改地址"],
    ["保修", "质保", "维修", "保修期", "三包", "坏了"],
    ["手机号", "手机号码", "绑定号码", "手机", "换号码"],
    ["支付", "付款", "付钱", "结账", "付不了", "支付失败", "付不了款"],
    ["到账", "入账", "到款", "到帐", "到账时间", "多久到账"],
    ["客服", "人工", "人工客服", "真人", "找客服", "客服人员"],
    ["安全", "隐私", "数据保护", "数据安全", "合规", "泄露"],
    ["换货", "更换", "换一个", "换个", "换新"],
    ["退货流程", "怎么退", "如何退货", "退货步骤", "退货要多久"],
    ["时间", "多久", "几天", "多长时间", "什么时候", "要多久"],
    ["没收到", "还没到", "没到货", "一直没收到", "还没收到", "东西没到", "没发货"],
    ["分期", "花呗", "白条", "分期付款"],
]

# 跨组连接：两个规范词之间的显式语义边。
# 作用是把「退货流程」和「退货/退款」、「发货」和「物流/时间」这些
# 分属不同同义词组但语义相关的概念连起来，让向量能在组间传播。
SYNONYM_LINKS: list[tuple[str, str]] = [
    ("退货流程", "退货"), ("退货流程", "退款"), ("退货流程", "换货"),
    ("退货流程", "时间"), ("退货流程", "运费"),
    ("退款", "到账"), ("到账", "时间"), ("退款", "支付"),
    ("发货", "物流"), ("发货", "时间"), ("物流", "没收到"),
    ("物流", "地址"), ("物流", "发货"),
    ("价格", "支付"), ("价格", "试用"), ("价格", "发票"),
    ("保修", "退货"), ("保修", "物流"),
    ("优惠券", "会员"), ("优惠券", "价格"),
    ("支付", "分期"), ("安全", "发票"), ("客服", "退货"),
    ("换货", "运费"), ("密码", "客服"),
]

CANON: dict[str, str] = {}
for _group in SYNONYM_GROUPS:
    canonical = _group[0]
    for _variant in _group:
        CANON[_variant] = canonical

# 按长度降序，保证「最长优先」
_VARIANTS_BY_LENGTH = sorted(CANON, key=len, reverse=True)


def _match_synonyms(text: str) -> list[str]:
    """返回文本中命中的规范词（最长优先、跨度不重叠、去重保序）。

    同义词扩展与向量词项抽取共用这一套匹配逻辑，避免两处行为不一致。
    """
    raw = (text or "").lower()
    if not raw:
        return []
    consumed = [False] * len(raw)
    found: list[str] = []
    for variant in _VARIANTS_BY_LENGTH:
        start = raw.find(variant)
        while start != -1:
            end = start + len(variant)
            if not any(consumed[start:end]):
                for i in range(start, end):
                    consumed[i] = True
                canonical = CANON[variant]
                if canonical not in found:
                    found.append(canonical)
                break
            start = raw.find(variant, start + 1)
    return found


def expand_synonyms(text: str) -> str:
    """在文本末尾补充同义词的规范写法，使「退钱」也能匹配到「退款」。"""
    raw = (text or "").lower()
    extra = [c for c in _match_synonyms(raw) if c not in raw]
    return raw + ("" if not extra else " " + " ".join(extra))


# ------------------------------------------------------------------ 打分
EXACT_SCORE = 0.97


def _text_similarity(q_bigram: set[str], q_unigram: set[str], surface: str) -> float:
    s = normalize(expand_synonyms(surface))
    return 0.65 * dice(q_bigram, char_ngrams(s, 2)) + 0.35 * dice(q_unigram, char_ngrams(s, 1))


def score_entry(
    query: str,
    entry: dict[str, Any],
    q_bigram: set[str] | None = None,
    q_unigram: set[str] | None = None,
    q_raw: str | None = None,
) -> tuple[float, str]:
    """返回 (分数, 命中的问法)。分数越大越可信。

    说明：同义词扩展只用于「文本相似度」，不用于精确命中与关键词计数，
    否则「多少钱可以免费上门取件」会因为扩展出「价格」而误命中收费类条目。
    """
    q_raw = normalize(query) if q_raw is None else q_raw
    q_exp = normalize(expand_synonyms(query))
    q_bigram = q_bigram if q_bigram is not None else char_ngrams(q_exp, 2)
    q_unigram = q_unigram if q_unigram is not None else char_ngrams(q_exp, 1)

    surfaces = [entry["question"], *(entry.get("similar") or [])]

    # 1) 精确命中短路 —— 只在标准问 / 相似问上做，避免正文误命中。
    #
    #    这里必须区分三种包含关系，否则泛化条目会靠"子串"抢占细分条目：
    #      a) 完全相等            → 1.00  最强
    #      b) 条目问法 ⊂ 用户问法 → 0.95  用户问得**更具体**，取包含它最长的那个条目
    #         （反例：问「商品质量问题退货的运费由谁承担」不该命中泛化的「退货的运费由谁承担」）
    #      c) 用户问法 ⊂ 条目问法 → 0.90  条目比用户问得**更具体**，前提未明，交给槽位澄清
    #         （反例：问「退货的运费由谁承担」不该直接命中「商品质量问题退货的运费由谁承担」）
    best_exact: tuple[float, int, str] | None = None
    for surface in surfaces:
        s = normalize(surface)
        if not s:
            continue
        if s == q_raw:
            tier = (1.0, len(s), surface)
        elif len(s) >= 3 and s in q_raw:
            tier = (0.95, len(s), surface)
        elif len(q_raw) >= 3 and q_raw in s:
            tier = (0.90, len(s), surface)
        else:
            continue
        if best_exact is None or (tier[0], tier[1]) > (best_exact[0], best_exact[1]):
            best_exact = tier
    if best_exact is not None:
        return best_exact[0], best_exact[2]

    # 2) 文本相似度。文档导入的切片额外把正文作为匹配面，
    #    否则自动生成的标题（如「售后手册？」）几乎无法被召回。
    if entry.get("doc_id"):
        body = (entry.get("answer") or "").strip()
        if body:
            surfaces = [*surfaces, body[:200]]

    best_sim, best_surface = 0.0, entry["question"]
    for surface in surfaces:
        sim = _text_similarity(q_bigram, q_unigram, surface)
        if sim > best_sim:
            best_sim, best_surface = sim, surface

    # 3) 关键词命中（基于用户原话，不含同义词扩展）
    keywords = [k for k in (entry.get("keywords") or []) if k]
    if keywords:
        hits = sum(1 for k in keywords if normalize(k) in q_raw)
        denom = min(len(keywords), 3)
        keyword_score = min(1.0, hits / denom) if denom else 0.0
    else:
        keyword_score = 0.0

    return 0.68 * best_sim + 0.32 * keyword_score, best_surface


# ------------------------------------------------------------------ 检索入口
def retrieve(
    query: str,
    entries: Iterable[dict[str, Any]],
    *,
    top_k: int = 3,
    exclude_ids: set[int] | None = None,
    prefer_category: str | None = None,
    category_only: bool = False,
    category_bonus: float = 0.05,
    use_vector: bool = True,
) -> list[dict[str, Any]]:
    """从知识条目中检索候选。

    Args:
        query: 用户问题（建议先做上下文改写）
        entries: 知识条目列表（直接来自数据库，保证后台改动即时生效）
        exclude_ids: 需要排除的条目（如同一会话中已经答过的条目）
        prefer_category: 偏好分类，命中时给予小额加分
        category_only: 只在该分类内检索（用于上下文追问的聚焦检索）
        category_bonus: 偏好分类的加分值
        use_vector: 是否启用向量召回（关闭则退化为纯词面检索，便于对照）
    """
    exclude_ids = exclude_ids or set()
    q_raw = normalize(query)
    q_exp = normalize(expand_synonyms(query))
    q_bigram, q_unigram = char_ngrams(q_exp, 2), char_ngrams(q_exp, 1)

    # 向量召回：查询向量只算一次；provider 不可用时静默退化为纯词面
    vector_query = None
    vector_kind = "none"
    doc_vectors: dict[int, str] = {}
    if use_vector:
        from . import db, embedding

        provider = embedding.ensure_index()
        if provider.name != "off":
            vector_kind = provider.kind
            vector_query = embedding.embed_query(query)
            if vector_query is not None:
                doc_vectors = db.embedding_map()

    scored: list[dict[str, Any]] = []
    for entry in entries:
        if not entry.get("enabled", True):
            continue
        if entry["id"] in exclude_ids:
            continue
        if category_only and prefer_category and entry.get("category") != prefer_category:
            continue
        lex_score, surface = score_entry(query, entry, q_bigram, q_unigram, q_raw)

        vec_score = 0.0
        if vector_query is not None and doc_vectors:
            from . import embedding

            vec_score = embedding.vector_similarity(
                vector_query, doc_vectors.get(entry["id"]), vector_kind)
            if vec_score < config.VECTOR_MIN_COSINE:
                vec_score = 0.0

        # 融合：任一路径足够强即可命中；两路都命中时给小额加成。
        # 打分本身保持"诚实"（不做封顶，避免制造并列、丢失排序信息）；
        # 「仅靠向量命中时不给直接作答」这条策略放在决策层（app/dialog.py）执行。
        if vec_score > 0:
            score = max(lex_score, config.VECTOR_WEIGHT * vec_score)
            if lex_score >= config.MID_CONFIDENCE:
                score += config.VECTOR_AGREE_BONUS
        else:
            score = lex_score

        if prefer_category and entry.get("category") == prefer_category:
            score += category_bonus
        if score <= 0:
            continue
        scored.append(
            {
                "id": entry["id"],
                "question": entry["question"],
                "answer": entry["answer"],
                "category": entry.get("category") or "未分类",
                # 对外分数封顶到 1.0（便于展示与阈值比较），
                # 但排序用未封顶的原始分：否则「一致加成」会把多个候选一起顶到 1.0，
                # 排序被抹平，泛化条目就会靠 id 顺序抢占细分条目。
                "score": round(min(score, 1.0), 4),
                "_raw": score,
                "_surface_len": len(normalize(surface or "")),
                "lex_score": round(lex_score, 4),
                "vec_score": round(vec_score, 4),
                # 仅靠向量命中（词面分不达标）：决策层会强制走「作答+确认」而不是直接作答
                "vector_only": bool(vec_score > 0 and lex_score < config.MID_CONFIDENCE),
                "matched_surface": surface,
                # 以下字段供槽位规则匹配与答案溯源使用
                "source_ref": entry.get("source_ref") or "手工录入",
                "slot": entry.get("slot"),
                "similar": entry.get("similar") or [],
                "keywords": entry.get("keywords") or [],
            }
        )

    # 主排序：原始分；并列时优先「命中问法更长」的条目（更具体者胜）
    scored.sort(key=lambda x: (x["_raw"], x["_surface_len"]), reverse=True)
    top = scored[:top_k]
    for item in top:
        item.pop("_raw", None)
        item.pop("_surface_len", None)
    return top


def explain(query: str, entries: Iterable[dict[str, Any]], top_k: int = 5) -> dict[str, Any]:
    """后台「检索测试」用：展示归一化文本、向量分与全部候选，便于运营调优阈值。"""
    from . import embedding

    q = normalize(expand_synonyms(query))
    results = retrieve(query, entries, top_k=top_k)
    provider = embedding.current()
    return {
        "normalized_query": q,
        "synonyms_hit": _match_synonyms(query),
        "vector": {
            "provider": provider.name,
            "provider_label": provider.label,
            "kind": provider.kind,
            "min_cosine": config.VECTOR_MIN_COSINE,
            "weight": config.VECTOR_WEIGHT,
        },
        "candidates": results,
    }
