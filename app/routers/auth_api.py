"""认证接口（PRD M7-2）。默认 AUTH_ENABLED=0 时无需登录。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from .. import auth
from ..schemas import LoginRequest

router = APIRouter(prefix="/api/auth", tags=["认证"])


@router.get("/status", summary="认证开关与角色说明")
def status() -> dict[str, Any]:
    return auth.auth_status()


@router.post("/login", summary="登录")
def login(payload: LoginRequest) -> dict[str, Any]:
    result = auth.login(payload.username, payload.password)
    if not result:
        raise HTTPException(status_code=401, detail="用户名或密码错误，或账号已停用")
    return {"message": "登录成功", **result}


@router.get("/me", summary="当前登录账号")
def me(account: dict = Depends(auth.current_account)) -> dict[str, Any]:
    return {"account": account, "auth_enabled": auth.config.AUTH_ENABLED}
