"""Embedding 向量召回。

三种 provider（通过 EMBEDDING_PROVIDER 选择，默认 auto）：

    api   —— 调用 OpenAI 兼容的 /embeddings 接口，**真正的语义向量**。
             能理解「钱什么时候回来」≈「退款多久到账」这类完全没有字面重叠的表达。
             需要配置 EMBEDDING_API_KEY。

    local —— 离线可用的稀疏向量：以「同义词图 + 词项共现」构建词项向量，
             再把文档/查询表示为词项向量的加权和。零额外依赖、结果确定。
             它能把同义词典覆盖到的说法拉到一起（钱 → 退款 → 退款条目），
             但**没有开放域世界知识**，词典没覆盖的说法仍然召回不到。

    off   —— 关闭向量召回，退化为纯词面检索（保留作为兜底与对照）。

设计要点：
1. 向量随知识条目持久化在 `knowledge.embedding`，避免每次启动重新计算。
2. 用内容 hash 判断条目是否变化，支持**增量索引**（api provider 只重新嵌入变化条目）。
3. local provider 的词表依赖整个知识库，任何改动都触发全量重建（几十毫秒级）。
4. 任何失败都静默降级为纯词面检索，绝不影响问答可用性。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from typing import Any, Iterable

from . import config

_lock = threading.Lock()
_state: dict[str, Any] = {"provider": None, "built_at": 0.0, "dirty": True}


# ==================================================================== 工具
def _cosine(a: dict[int, float] | list[float], b: dict[int, float] | list[float]) -> float:
    """支持稀疏 dict 与稠密 list 两种向量。"""
    if not a or not b:
        return 0.0
    if isinstance(a, dict) and isinstance(b, dict):
        if len(a) > len(b):
            a, b = b, a
        return sum(v * b.get(k, 0.0) for k, v in a.items())
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return 0.0
        return sum(x * y for x, y in zip(a, b))
    return 0.0


def _normalize_sparse(vec: dict[int, float]) -> dict[int, float]:
    norm = math.sqrt(sum(v * v for v in vec.values()))
    if norm <= 0:
        return {}
    return {k: v / norm for k, v in vec.items() if abs(v) > 1e-9}


def _normalize_dense(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm <= 0:
        return vec
    return [v / norm for v in vec]


def content_hash(entry: dict[str, Any]) -> str:
    """条目内容指纹：任一会影响检索的字段变化都会导致重新嵌入。"""
    payload = "|".join([
        entry.get("question", ""),
        "／".join(entry.get("similar") or []),
        ",".join(entry.get("keywords") or []),
        (entry.get("answer") or "")[:300],
        entry.get("category", ""),
    ])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


# ==================================================================== Provider
class BaseProvider:
    name = "off"
    kind = "none"          # sparse / dense / none
    label = "关闭向量召回"

    def available(self) -> bool:
        return False

    def embed_documents(self, entries: list[dict[str, Any]]) -> dict[int, Any]:
        """返回 {entry_id: vector}。"""
        return {}

    def embed_query(self, text: str) -> Any:
        return None


# -------------------------------------------------------------------- local
class LocalProvider(BaseProvider):
    """离线稀疏向量：同义词图 + 词项共现传播。

    词项来源：关键词（权重 2.0）、问法切分片段（1.0）、问法字符二元组（0.5）、
    同义词典命中的规范词（1.5）。词项之间建立两类边：
      - 同义词典边（强，0.9）
      - 同一文档内共现边（弱，0.35 × idf，只在 idf 足够高时才连）
    词项向量 = 自身 + 邻居加权；文档向量 = Σ(idf × tf × 词项向量) 后归一化。
    """

    name = "local"
    kind = "sparse"
    label = "离线同义词图向量"

    def __init__(self) -> None:
        self.vocab: dict[str, int] = {}
        self.term_vectors: dict[str, dict[int, float]] = {}
        self.idf: dict[str, float] = {}
        self.built = False

    def available(self) -> bool:
        return True

    # -------------------------------------------------- 词项抽取
    @staticmethod
    def _terms(entry: dict[str, Any]) -> Counter:
        terms: Counter = Counter()
        for kw in entry.get("keywords") or []:
            kw = kw.strip()
            if len(kw) >= 2:
                terms[kw] += 2.0
        for surface in [entry.get("question", ""), *(entry.get("similar") or [])]:
            for piece in re.split(r"[，。、；：,.;:！？!?\s（）()【】\[\]—\-/]+", surface):
                piece = piece.strip("？?…的了吗呢")
                if 2 <= len(piece) <= 12:
                    terms[piece] += 1.0
                for i in range(len(piece) - 1):
                    terms[piece[i:i + 2]] += 0.5
        for canonical in _canonical_terms(entry.get("question", "")):
            terms[canonical] += 1.5
        return terms

    @staticmethod
    def _query_terms(text: str) -> Counter:
        terms: Counter = Counter()
        for piece in re.split(r"[，。、；：,.;:！？!?\s（）()【】\[\]—\-/]+", text):
            piece = piece.strip("？?…的了吗呢")
            if 2 <= len(piece) <= 12:
                terms[piece] += 1.0
            for i in range(len(piece) - 1):
                terms[piece[i:i + 2]] += 0.5
        for canonical in _canonical_terms(text):
            terms[canonical] += 2.0
        return terms

    # -------------------------------------------------- 建索引
    def build(self, entries: list[dict[str, Any]]) -> dict[int, dict[int, float]]:
        from .knowledge import SYNONYM_GROUPS, SYNONYM_LINKS

        docs: list[tuple[int, Counter]] = []
        df: Counter = Counter()
        for e in entries:
            terms = self._terms(e)
            if not terms:
                continue
            docs.append((e["id"], terms))
            for t in terms:
                df[t] += 1

        total = max(1, len(docs))
        self.idf = {t: math.log((total + 1) / (c + 1)) + 1.0 for t, c in df.items()}

        # --- 建立词项图
        edges: dict[str, dict[str, float]] = defaultdict(dict)

        def link(a: str, b: str, w: float) -> None:
            if a == b:
                return
            if edges[a].get(b, 0.0) < w:
                edges[a][b] = w
                edges[b][a] = w

        # 同义词典边（强）
        for group in SYNONYM_GROUPS:
            canonical = group[0]
            for variant in group:
                link(variant, canonical, 0.9)

        # 跨组语义边（略弱于组内边，但强于共现边）
        for left, right in SYNONYM_LINKS:
            link(left, right, 0.75)

        # 共现边（弱，按 idf 加权，避免"可以/什么"这类高频词互连）
        for _eid, terms in docs:
            keys = [t for t in terms if self.idf.get(t, 0) >= 1.0][:40]
            for i, a in enumerate(keys):
                for b in keys[i + 1:]:
                    w = 0.35 * min(self.idf.get(a, 1.0), self.idf.get(b, 1.0))
                    if w >= 0.42:
                        link(a, b, min(w, 1.0))

        # --- 词项向量（自身 + 邻居），维度只在词表内
        vocabulary = sorted(set(df) | {k for k in edges})
        self.vocab = {t: i for i, t in enumerate(vocabulary)}
        in_vocab = set(df)
        self.term_vectors = {}
        for term in vocabulary:
            vec: dict[int, float] = {}
            if term in in_vocab:
                vec[self.vocab[term]] = 1.0
            for neighbor, weight in edges.get(term, {}).items():
                if neighbor in in_vocab:                      # 只保留落在词表里的维度
                    idx = self.vocab[neighbor]
                    vec[idx] = vec.get(idx, 0.0) + weight
            if vec:
                self.term_vectors[term] = _normalize_sparse(vec)

        # --- 文档向量
        vectors: dict[int, dict[int, float]] = {}
        for eid, terms in docs:
            acc: dict[int, float] = {}
            for term, tf in terms.items():
                tvec = self.term_vectors.get(term)
                if not tvec:
                    continue
                weight = tf * self.idf.get(term, 1.0)
                for idx, value in tvec.items():
                    acc[idx] = acc.get(idx, 0.0) + value * weight
            if acc:
                vectors[eid] = _normalize_sparse(acc)

        self.built = True
        return vectors

    def embed_documents(self, entries: list[dict[str, Any]]) -> dict[int, dict[int, float]]:
        return self.build(entries)

    def embed_query(self, text: str) -> dict[int, float]:
        if not self.built:
            return {}
        acc: dict[int, float] = {}
        for term, tf in self._query_terms(text).items():
            tvec = self.term_vectors.get(term)
            if not tvec:
                continue
            weight = tf * self.idf.get(term, 1.2)
            for idx, value in tvec.items():
                acc[idx] = acc.get(idx, 0.0) + value * weight
        return _normalize_sparse(acc)


def _canonical_terms(text: str) -> list[str]:
    """从文本里提取命中的同义词典规范词。"""
    from .knowledge import _match_synonyms  # 复用同一套「最长匹配优先」逻辑

    return _match_synonyms(text)


# -------------------------------------------------------------------- api
class ApiProvider(BaseProvider):
    """OpenAI 兼容的 Embedding 接口。"""

    name = "api"
    kind = "dense"
    label = "在线语义向量"

    def __init__(self) -> None:
        self.dim = 0
        self.calls = 0
        self.errors = 0

    def available(self) -> bool:
        return bool(config.EMBEDDING_API_KEY)

    def _post(self, texts: list[str]) -> list[list[float]]:
        payload = {"model": config.EMBEDDING_MODEL, "input": texts, "encoding_format": "float"}
        req = urllib.request.Request(
            f"{config.EMBEDDING_BASE_URL.rstrip('/')}/embeddings",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {config.EMBEDDING_API_KEY}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=config.EMBEDDING_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        items = sorted(data["data"], key=lambda x: x.get("index", 0))
        return [_normalize_dense(list(map(float, it["embedding"]))) for it in items]

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), config.EMBEDDING_BATCH):
            batch = texts[i:i + config.EMBEDDING_BATCH]
            try:
                vectors = self._post(batch)
                self.calls += 1
                if vectors and not self.dim:
                    self.dim = len(vectors[0])
                # 对齐数量，防止接口少返回
                while len(vectors) < len(batch):
                    vectors.append([])
                out.extend(vectors)
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                    ValueError, KeyError, OSError):
                self.errors += 1
                out.extend([[] for _ in batch])
        return out

    @staticmethod
    def _doc_text(entry: dict[str, Any]) -> str:
        parts = [entry.get("question", "")]
        if entry.get("similar"):
            parts.append("；".join(entry["similar"]))
        if entry.get("keywords"):
            parts.append("，".join(entry["keywords"]))
        parts.append((entry.get("answer") or "")[:300])
        return "\n".join(p for p in parts if p)

    def embed_documents(self, entries: list[dict[str, Any]]) -> dict[int, list[float]]:
        if not entries:
            return {}
        vectors = self.embed_texts([self._doc_text(e) for e in entries])
        return {e["id"]: v for e, v in zip(entries, vectors) if v}

    def embed_query(self, text: str) -> list[float] | None:
        vectors = self.embed_texts([text])
        return vectors[0] if vectors and vectors[0] else None


# -------------------------------------------------------------------- off
class OffProvider(BaseProvider):
    name = "off"
    kind = "none"
    label = "已关闭（纯词面检索）"


# ==================================================================== 门面
def get_provider(force: str | None = None) -> BaseProvider:
    mode = (force or config.EMBEDDING_PROVIDER or "auto").lower()
    if mode == "auto":
        mode = "api" if config.EMBEDDING_API_KEY else "local"
    if mode == "api":
        provider: BaseProvider = ApiProvider()
        if provider.available():
            return provider
        return LocalProvider()          # 配了 api 但没 Key → 退回离线
    if mode == "local":
        return LocalProvider()
    return OffProvider()


def current() -> BaseProvider:
    with _lock:
        mode = config.EMBEDDING_PROVIDER
        if mode == "auto":
            mode = "api" if config.EMBEDDING_API_KEY else "local"
        cached = _state.get("provider")
        if cached is not None and getattr(cached, "name", "") == mode:
            return cached
        provider = get_provider()
        _state["provider"] = provider
        return provider


def mark_dirty() -> None:
    _state["dirty"] = True


def is_dirty() -> bool:
    return bool(_state.get("dirty"))


def status() -> dict[str, Any]:
    from . import db

    provider = current()
    cached = db.embedding_stats()
    return {
        "provider": provider.name,
        "provider_label": provider.label,
        "kind": provider.kind,
        "configured_mode": config.EMBEDDING_PROVIDER,
        "model": config.EMBEDDING_MODEL if provider.name == "api" else "内置同义词图（无模型）",
        "api_ready": bool(config.EMBEDDING_API_KEY),
        "enabled": provider.name != "off",
        "dim": getattr(provider, "dim", 0) or (len(provider.vocab) if provider.name == "local" else 0),
        "indexed": cached["indexed"],
        "total": cached["total"],
        "stale": cached["stale"],
        "built_at": cached["built_at"],
        "weights": {
            "vector_weight": config.VECTOR_WEIGHT,
            "min_cosine": config.VECTOR_MIN_COSINE,
            "agree_bonus": config.VECTOR_AGREE_BONUS,
        },
    }


def build_index(entries: list[dict[str, Any]] | None = None,
                force: bool = False) -> dict[str, Any]:
    """构建 / 增量更新向量索引。任何异常都被吞掉并降级为纯词面检索。"""
    from . import db

    provider = current()
    started = time.time()
    if provider.name == "off":
        with _lock:
            _state["dirty"] = False
            _state["built_at"] = started
        return {"provider": "off", "indexed": 0, "skipped": 0, "elapsed_ms": 0,
                "message": "向量召回已关闭"}

    entries = entries if entries is not None else db.list_knowledge(enabled=1)
    by_id = {e["id"]: e for e in entries}
    if provider.name == "local":
        # 词表依赖整个知识库 → 全量重建（几十毫秒）
        try:
            vectors = provider.embed_documents(entries)
        except Exception as exc:  # noqa: BLE001
            return {"provider": provider.name, "indexed": 0, "error": str(exc),
                    "elapsed_ms": int((time.time() - started) * 1000)}
        rows = [
            (eid, json.dumps({str(k): round(v, 6) for k, v in vec.items()}),
             "local", content_hash(by_id[eid]))
            for eid, vec in vectors.items() if eid in by_id
        ]
        db.save_embeddings(rows, provider="local")
        indexed = len(rows)
        skipped = len(entries) - indexed
    else:
        # api provider：只嵌入新增或内容变化的条目
        stale_entries = [
            e for e in entries
            if force or db.embedding_of(e["id"]) is None
            or db.embedding_hash_of(e["id"]) != content_hash(e)
            or db.embedding_provider_of(e["id"]) != provider.name
        ]
        vectors = provider.embed_documents(stale_entries)
        rows = []
        for e in stale_entries:
            vec = vectors.get(e["id"])
            if vec:
                rows.append((e["id"], json.dumps([round(v, 6) for v in vec]),
                             provider.name, content_hash(e)))
        db.save_embeddings(rows, provider=provider.name)
        indexed, skipped = len(rows), len(entries) - len(rows)

    with _lock:
        _state["dirty"] = False
        _state["built_at"] = time.time()
    if hasattr(provider, "built"):
        pass
    return {
        "provider": provider.name,
        "provider_label": provider.label,
        "indexed": indexed,
        "skipped": skipped,
        "elapsed_ms": int((time.time() - started) * 1000),
        "api_calls": getattr(provider, "calls", 0),
        "api_errors": getattr(provider, "errors", 0),
    }


def ensure_index(entries: list[dict[str, Any]] | None = None) -> BaseProvider:
    """供检索调用：需要时自动重建索引（脏标记 + local 全量重建）。"""
    provider = current()
    if provider.name == "off":
        return provider
    if is_dirty() or (provider.name == "local" and not provider.built):
        build_index(entries)
    return provider


def embed_query(text: str) -> Any:
    provider = current()
    if provider.name == "off":
        return None
    try:
        return provider.embed_query(text)
    except Exception:  # noqa: BLE001
        return None


def parse_vector(raw: str | None, kind: str) -> Any:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if kind == "sparse":
        return {int(k): float(v) for k, v in data.items()} if isinstance(data, dict) else None
    return [float(v) for v in data] if isinstance(data, list) else None


def vector_similarity(query_vec: Any, raw_vector: str | None, kind: str) -> float:
    if query_vec is None or not raw_vector:
        return 0.0
    doc_vec = parse_vector(raw_vector, kind)
    if not doc_vec:
        return 0.0
    return max(0.0, min(1.0, _cosine(query_vec, doc_vec)))
