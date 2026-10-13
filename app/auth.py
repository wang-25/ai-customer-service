"""账号认证与角色权限（PRD M7-2）。

设计取舍：
- 默认 AUTH_ENABLED=0：本地演示无需登录，所有请求按「管理员」处理，方便直接体验。
- 设为 1 后启用四角色权限，接口按角色鉴权：
    admin       全部权限
    supervisor  数据看板 + 全部会话 + 常用语
    agent       工作台 + 本人会话
    editor      知识库 + 文档导入 + badcase 回流
- Token 形式：base64(payload).hmac_sha256 签名，无状态，无需额外依赖。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Callable

from fastapi import HTTPException, Request

from . import config, db

DEMO_ACCOUNT = {
    "id": 0,
    "username": "demo",
    "name": "演示用户（未启用认证）",
    "role": "admin",
    "role_label": config.ROLE_LABELS["admin"],
}


# ------------------------------------------------------------------ Token
def _sign(payload: str) -> str:
    return hmac.new(config.TOKEN_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_token(account: dict[str, Any]) -> str:
    payload = {
        "uid": account["id"],
        "username": account["username"],
        "name": account.get("name") or account["username"],
        "role": account["role"],
        "exp": int(time.time()) + config.TOKEN_TTL_SECONDS,
    }
    raw = base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()
    return f"{raw}.{_sign(raw)}"


def parse_token(token: str) -> dict[str, Any] | None:
    if not token or "." not in token:
        return None
    raw, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(_sign(raw), signature):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw.encode()).decode())
    except (ValueError, TypeError):
        return None
    if int(payload.get("exp", 0)) < time.time():
        return None
    # 统一账号标识字段名，业务代码统一用 account["id"]
    if "uid" in payload:
        payload["id"] = payload["uid"]
    payload["role_label"] = config.ROLE_LABELS.get(payload.get("role", ""), payload.get("role", ""))
    return payload


def login(username: str, password: str) -> dict[str, Any] | None:
    account = db.verify_account(username, password)
    if not account:
        return None
    account = {**account}
    account["role_label"] = config.ROLE_LABELS.get(account["role"], account["role"])
    return {"token": make_token(account), "account": {
        k: account[k] for k in ("id", "username", "name", "role", "role_label")
    }}


# ------------------------------------------------------------------ 依赖
def _extract_token(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.query_params.get("token", "")


def current_account(request: Request) -> dict[str, Any]:
    """FastAPI 依赖：解析当前登录账号。未启用认证时返回演示管理员。"""
    if not config.AUTH_ENABLED:
        return dict(DEMO_ACCOUNT)
    payload = parse_token(_extract_token(request))
    if not payload:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    return payload


def require_role(*roles: str) -> Callable[[Request], dict[str, Any]]:
    """生成一个角色校验依赖。admin 始终放行。"""
    allowed = set(roles) | {"admin"}

    def dependency(request: Request) -> dict[str, Any]:
        account = current_account(request)
        if account["role"] not in allowed:
            raise HTTPException(
                status_code=403,
                detail=f"当前角色「{account.get('role_label', account['role'])}」无权访问该功能",
            )
        return account

    return dependency


def auth_status() -> dict[str, Any]:
    return {
        "auth_enabled": config.AUTH_ENABLED,
        "roles": [{"value": r, "label": config.ROLE_LABELS[r]} for r in config.ROLES],
        "demo_accounts": (
            [
                {"username": "admin", "password": "admin123", "role": "管理员"},
                {"username": "supervisor", "password": "123456", "role": "客服主管"},
                {"username": "agent1", "password": "123456", "role": "客服"},
                {"username": "agent2", "password": "123456", "role": "客服"},
                {"username": "editor", "password": "123456", "role": "知识运营"},
            ]
            if config.AUTH_ENABLED
            else []
        ),
    }
