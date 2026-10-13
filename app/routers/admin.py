"""运营后台接口：知识库、文档导入、会话记录、数据看板、机器人配置、账号权限。

对应 PRD：M3 知识库管理、M6 数据与反馈、M7 配置与权限
"""
from __future__ import annotations

import csv
import io
import json
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse

from .. import analytics, auth, config, db, docimport, embedding, handoff, knowledge
from ..schemas import (
    AccountIn,
    AccountUpdate,
    BotConfigUpdate,
    DocumentImportRequest,
    DocumentPublishRequest,
    FallbackToKnowledgeRequest,
    KnowledgeIn,
    KnowledgeUpdate,
    SearchTestRequest,
    TriggerRuleUpdate,
)

router = APIRouter(prefix="/api", tags=["运营后台"])

_can_edit_kb = auth.require_role("editor", "supervisor")
_can_read_data = auth.require_role("supervisor")
_can_admin = auth.require_role("admin")


# ==================================================================== 知识库（M3-1 / M3-3）
@router.get("/knowledge", summary="知识条目列表")
def list_knowledge(
    q: str = Query(default=""),
    category: str = Query(default=""),
    enabled: bool | None = Query(default=None),
    doc_id: int | None = Query(default=None),
    account: dict = Depends(_can_edit_kb),
) -> dict[str, Any]:
    items = db.list_knowledge(q=q, category=category,
                              enabled=None if enabled is None else int(enabled), doc_id=doc_id)
    return {"total": len(items), "items": items, "categories": db.list_categories(),
            "slots": [{"id": r["id"], "name": r["name"]} for r in config.SLOT_RULES]}


@router.get("/categories", summary="分类列表")
def categories() -> dict[str, Any]:
    return {"categories": db.list_categories()}


@router.get("/knowledge/{kid}", summary="知识条目详情")
def get_knowledge(kid: int, account: dict = Depends(_can_edit_kb)) -> dict[str, Any]:
    item = db.get_knowledge(kid)
    if not item:
        raise HTTPException(status_code=404, detail="知识条目不存在")
    return item


@router.post("/knowledge", summary="新增知识条目（即时生效）")
def create_knowledge(payload: KnowledgeIn, account: dict = Depends(_can_edit_kb)) -> dict[str, Any]:
    data = payload.model_dump()
    data["source_ref"] = f"手工录入（{account.get('name') or '后台'}）"
    kid = db.insert_knowledge(data)
    return {"message": "新增成功，已即时生效", "item": db.get_knowledge(kid)}


@router.put("/knowledge/{kid}", summary="更新知识条目（即时生效）")
def update_knowledge(kid: int, payload: KnowledgeUpdate, account: dict = Depends(_can_edit_kb)) -> dict[str, Any]:
    if not db.update_knowledge(kid, payload.model_dump(exclude_unset=True)):
        raise HTTPException(status_code=404, detail="知识条目不存在")
    return {"message": "更新成功，已即时生效", "item": db.get_knowledge(kid)}


@router.delete("/knowledge/{kid}", summary="删除知识条目（即时生效）")
def delete_knowledge(kid: int, account: dict = Depends(_can_edit_kb)) -> dict[str, Any]:
    if not db.delete_knowledge(kid):
        raise HTTPException(status_code=404, detail="知识条目不存在")
    return {"message": "删除成功，已即时生效", "id": kid}


@router.post("/knowledge/reset-seed", summary="重置为种子知识库")
def reset_seed(account: dict = Depends(_can_admin)) -> dict[str, Any]:
    from ..seed_data import SEED_KNOWLEDGE

    db.clear_knowledge()
    for item in SEED_KNOWLEDGE:
        db.insert_knowledge(item)
    return {"message": f"已重置为种子数据，共 {db.count_knowledge()} 条", "total": db.count_knowledge()}


@router.post("/knowledge/test", summary="检索测试（不改数据）")
def test_search(payload: SearchTestRequest, account: dict = Depends(_can_edit_kb)) -> dict[str, Any]:
    entries = db.list_knowledge(enabled=1)
    result = knowledge.explain(payload.query, entries, top_k=payload.top_k)
    high, mid = config.HIGH_CONFIDENCE, config.MID_CONFIDENCE
    for c in result["candidates"]:
        c["level"] = "直接作答" if c["score"] >= high else ("作答+确认" if c["score"] >= mid else "进入兜底")
        rule = next((r for r in config.SLOT_RULES
                     if r["id"] == c.get("slot")
                     or (c["category"] == r.get("when_category")
                         and any(k in (c["question"] + "".join(c.get("similar") or []) + "".join(c.get("keywords") or []))
                                 for k in r["when_keywords"]))), None)
        c["clarify"] = rule["name"] if rule else None
    result["thresholds"] = {"high": high, "mid": mid}
    return result


# ==================================================================== 文档导入（M3-2）
@router.get("/documents", summary="文档列表")
def list_documents(account: dict = Depends(_can_edit_kb)) -> dict[str, Any]:
    return {"items": db.list_documents()}


@router.get("/documents/{doc_id}", summary="文档详情与切片")
def get_document(doc_id: int, account: dict = Depends(_can_edit_kb)) -> dict[str, Any]:
    doc = db.get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    return {"document": doc, "chunks": db.list_document_chunks(doc_id)}


@router.post("/documents/import", summary="导入文档（粘贴文本，先入草稿）")
def import_document(payload: DocumentImportRequest, account: dict = Depends(_can_edit_kb)) -> dict[str, Any]:
    result = docimport.import_document(
        title=payload.title, content=payload.content, category=payload.category,
        source_name=payload.source_name or payload.title, source_type=payload.source_type,
        publish=payload.publish,
    )
    if not result["chunk_count"]:
        raise HTTPException(status_code=400, detail="未能从内容中切分出有效片段，请检查文本长度")
    return {
        **result,
        "message": f"已切分为 {result['chunk_count']} 个片段，"
                   + ("已直接发布" if payload.publish else "请预览确认后点击发布生效"),
    }


@router.post("/documents/upload", summary="上传文件导入（.txt / .md / .docx）")
async def upload_document(
    file: UploadFile = File(...),
    title: str = Form(default=""),
    category: str = Form(default=""),
    publish: bool = Form(default=False),
    account: dict = Depends(_can_edit_kb),
) -> dict[str, Any]:
    data = await file.read()
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="文件过大（上限 5MB）")
    try:
        text, source_type = docimport.extract_upload(file.filename or "", data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = docimport.import_document(
        title=title or (file.filename or "未命名文档").rsplit(".", 1)[0],
        content=text, category=category, source_name=file.filename or "",
        source_type=source_type, publish=publish,
    )
    if not result["chunk_count"]:
        raise HTTPException(status_code=400, detail="未能从文件中切分出有效片段")
    return {**result, "message": f"已解析并切分为 {result['chunk_count']} 个片段，请预览确认后发布"}


@router.post("/documents/{doc_id}/publish", summary="发布 / 下架文档切片")
def publish_document(doc_id: int, payload: DocumentPublishRequest,
                     account: dict = Depends(_can_edit_kb)) -> dict[str, Any]:
    if not db.get_document(doc_id):
        raise HTTPException(status_code=404, detail="文档不存在")
    count = db.publish_document(doc_id, enabled=payload.enabled)
    return {"message": f"已{'发布' if payload.enabled else '下架'} {count} 个片段，即时生效", "count": count}


@router.post("/documents/{doc_id}/test", summary="文档切片召回预检")
def test_document(doc_id: int, payload: SearchTestRequest, account: dict = Depends(_can_edit_kb)) -> dict[str, Any]:
    if not db.get_document(doc_id):
        raise HTTPException(status_code=404, detail="文档不存在")
    return {"query": payload.query, "candidates": docimport.search_in_document(doc_id, payload.query, payload.top_k)}


@router.delete("/documents/{doc_id}", summary="删除文档及其切片")
def delete_document(doc_id: int, account: dict = Depends(_can_edit_kb)) -> dict[str, Any]:
    if not db.delete_document(doc_id):
        raise HTTPException(status_code=404, detail="文档不存在")
    return {"message": "文档及其切片已删除"}


# ==================================================================== 向量索引（Embedding）
@router.get("/admin/embedding", summary="向量召回状态")
def embedding_status(account: dict = Depends(_can_edit_kb)) -> dict[str, Any]:
    return embedding.status()


@router.post("/admin/embedding/rebuild", summary="重建 / 增量更新向量索引")
def embedding_rebuild(
    force: bool = Query(default=False, description="true=全部重新嵌入；false=只补新增与变化"),
    account: dict = Depends(_can_edit_kb),
) -> dict[str, Any]:
    result = embedding.build_index(force=force)
    return {"message": "索引已更新", "result": result, "status": embedding.status()}


@router.post("/admin/embedding/test", summary="向量召回对照测试（同一问题看词面分与向量分）")
def embedding_test(payload: SearchTestRequest, account: dict = Depends(_can_edit_kb)) -> dict[str, Any]:
    entries = db.list_knowledge(enabled=1)
    with_vec = knowledge.retrieve(payload.query, entries, top_k=payload.top_k, use_vector=True)
    without = knowledge.retrieve(payload.query, entries, top_k=payload.top_k, use_vector=False)
    return {
        "query": payload.query,
        "synonyms_hit": knowledge._match_synonyms(payload.query),
        "provider": embedding.status(),
        "with_vector": with_vec,
        "lexical_only": without,
    }


# ==================================================================== 会话记录（M6-1）
@router.get("/admin/sessions", summary="会话记录检索")
def admin_sessions(
    status: str = Query(default=""),
    handoff_only: bool = Query(default=False),
    bad_csat: bool = Query(default=False),
    since: str = Query(default=""),
    until: str = Query(default=""),
    keyword: str = Query(default=""),
    category: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    account: dict = Depends(_can_read_data),
) -> dict[str, Any]:
    sessions = db.list_sessions(
        status=status, handoff=True if handoff_only else None,
        csat=0 if bad_csat else None, since=since, until=until,
        keyword=keyword, category=category, limit=limit, offset=offset,
    )
    return {
        "items": sessions,
        # 命中条数：与筛选条件完全一致；期间总数：只看时间范围
        "total": db.count_sessions(
            status=status, handoff=True if handoff_only else None,
            csat=0 if bad_csat else None, since=since, until=until,
            keyword=keyword, category=category,
        ),
        "total_in_range": db.count_sessions(since=since, until=until),
        "categories": db.list_categories(),
        "statuses": [{"value": k, "label": v} for k, v in config.SESSION_STATUS.items()],
    }


@router.get("/admin/sessions/export.csv", summary="导出会话记录 CSV")
def export_sessions(
    status: str = Query(default=""),
    handoff_only: bool = Query(default=False),
    bad_csat: bool = Query(default=False),
    since: str = Query(default=""),
    until: str = Query(default=""),
    keyword: str = Query(default=""),
    category: str = Query(default=""),
    account: dict = Depends(_can_read_data),
) -> StreamingResponse:
    sessions = db.list_sessions(
        status=status, handoff=True if handoff_only else None,
        csat=0 if bad_csat else None, since=since, until=until,
        keyword=keyword, category=category, limit=5000,
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["会话ID", "访客", "创建时间", "状态", "轮次", "兜底次数",
                     "最后意图", "分类", "转人工原因", "接待客服", "归因", "满意度", "差评原因"])
    for s in sessions:
        agent = db.get_agent(s["agent_id"]) if s.get("agent_id") else None
        writer.writerow([
            s["id"], s.get("visitor_id", ""), s["created_at"], s["status_label"],
            s["turn_count"], s["fallback_count"], s.get("last_question") or "",
            s.get("last_category") or "", handoff.describe_trigger(s.get("handoff_reason") or ""),
            agent["name"] if agent else "", s.get("resolved_by") or "",
            {1: "好评", -1: "差评"}.get(s.get("csat_score"), "未评价"), s.get("csat_reason") or "",
        ])
    content = "\ufeff" + buffer.getvalue()
    return StreamingResponse(
        iter([content]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="sessions.csv"'},
    )


@router.get("/admin/sessions/{session_id}", summary="会话完整详情（含溯源与转人工记录）")
def admin_session_detail(session_id: str, account: dict = Depends(_can_read_data)) -> dict[str, Any]:
    detail = analytics.session_detail(session_id)
    if not detail:
        raise HTTPException(status_code=404, detail="会话不存在")
    for h in detail["handoff"]:
        h["trigger_label"] = handoff.describe_trigger(h["trigger_type"])
    return detail


# ==================================================================== 数据看板（M6-3）
@router.get("/admin/overview", summary="核心指标看板")
def admin_overview(days: int = Query(default=7, ge=1, le=90),
                   account: dict = Depends(_can_read_data)) -> dict[str, Any]:
    return analytics.overview(days)


@router.get("/admin/fallbacks", summary="未解决问题（badcase）列表")
def admin_fallbacks(
    limit: int = Query(default=50, ge=1, le=500),
    handled: int | None = Query(default=None),
    account: dict = Depends(_can_edit_kb),
) -> dict[str, Any]:
    return {"items": db.list_fallbacks(limit=limit, handled=handled)}


@router.post("/admin/fallbacks/{fid}/to-knowledge", summary="Badcase 一键回流知识库")
def fallback_to_knowledge(
    fid: int, payload: FallbackToKnowledgeRequest, account: dict = Depends(_can_edit_kb)
) -> dict[str, Any]:
    items = db.list_fallbacks(limit=500)
    target = next((i for i in items if i["id"] == fid), None)
    if not target:
        raise HTTPException(status_code=404, detail="记录不存在")
    kid = db.insert_knowledge({
        "question": target["query"],
        "answer": payload.answer,
        "category": payload.category or "未分类",
        "similar": [],
        "keywords": [],
        "enabled": True,
        "source_ref": f"Badcase 回流 #{fid}",
    })
    db.mark_fallback_handled(fid, 1)
    return {"message": "已回流为新知识条目并即时生效", "item": db.get_knowledge(kid)}


# ==================================================================== 机器人配置（M7-1）
@router.get("/admin/bot-config", summary="机器人配置")
def get_bot_config(account: dict = Depends(_can_read_data)) -> dict[str, Any]:
    return {"config": db.get_bot_config(), "slots": config.SLOT_RULES}


@router.put("/admin/bot-config", summary="更新机器人配置（话术零研发可改）")
def update_bot_config(payload: BotConfigUpdate, account: dict = Depends(_can_read_data)) -> dict[str, Any]:
    cfg = db.update_bot_config(payload.model_dump(exclude_unset=True))
    return {"message": "配置已保存并即时生效", "config": cfg}


@router.get("/admin/trigger-rules", summary="转人工触发规则")
def get_trigger_rules(account: dict = Depends(_can_read_data)) -> dict[str, Any]:
    return {
        "items": db.list_trigger_rules(),
        "sentiment_words": config.SENTIMENT_WORDS,
        "risky_words": config.RISKY_WORDS,
    }


@router.put("/admin/trigger-rules/{rule_type}", summary="调整触发规则阈值")
def update_trigger_rule(rule_type: str, payload: TriggerRuleUpdate,
                        account: dict = Depends(_can_read_data)) -> dict[str, Any]:
    if not db.update_trigger_rule(rule_type, payload.threshold, payload.enabled):
        raise HTTPException(status_code=404, detail="规则不存在或未提供变更内容")
    return {"message": "规则已更新即时生效", "items": db.list_trigger_rules()}


# ==================================================================== 客服管理
@router.get("/admin/agents", summary="客服列表（主管视图）")
def admin_agents(account: dict = Depends(_can_read_data)) -> dict[str, Any]:
    return {"items": handoff.working_agents_summary()}


# ==================================================================== 账号与权限（M7-2）
@router.get("/admin/accounts", summary="账号列表")
def list_accounts(account: dict = Depends(_can_admin)) -> dict[str, Any]:
    return {"items": db.list_accounts(), "roles": [
        {"value": r, "label": config.ROLE_LABELS[r]} for r in config.ROLES
    ]}


@router.post("/admin/accounts", summary="新增账号")
def create_account(payload: AccountIn, account: dict = Depends(_can_admin)) -> dict[str, Any]:
    if db.get_account_by_username(payload.username):
        raise HTTPException(status_code=400, detail="用户名已存在")
    account_id = db.create_account(payload.username, payload.password,
                                   payload.name or payload.username, payload.role, payload.enabled)
    if payload.role == "agent":
        db.create_agent(payload.name or payload.username, account_id=account_id, status="offline")
    return {"message": "账号已创建", "id": account_id}


@router.put("/admin/accounts/{account_id}", summary="更新账号")
def update_account(account_id: int, payload: AccountUpdate,
                   account: dict = Depends(_can_admin)) -> dict[str, Any]:
    if not db.get_account(account_id):
        raise HTTPException(status_code=404, detail="账号不存在")
    data = payload.model_dump(exclude_unset=True)
    if not db.update_account(account_id, password=data.get("password"), name=data.get("name"),
                             role=data.get("role"),
                             enabled=None if data.get("enabled") is None else int(data["enabled"])):
        raise HTTPException(status_code=400, detail="未提供任何变更内容")
    return {"message": "账号已更新"}


@router.delete("/admin/accounts/{account_id}", summary="删除账号")
def delete_account(account_id: int, account: dict = Depends(_can_admin)) -> dict[str, Any]:
    if account.get("id") == account_id:
        raise HTTPException(status_code=400, detail="不能删除当前登录账号")
    if not db.delete_account(account_id):
        raise HTTPException(status_code=404, detail="账号不存在")
    return {"message": "账号已删除"}
