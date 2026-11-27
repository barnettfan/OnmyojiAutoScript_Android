"""控制面与管理面的独立鉴权边界。"""
from __future__ import annotations

import hmac
import os

from fastapi import HTTPException, status


AGENT_SECRET_HEADER = "x-oas-agent-secret"
AGENT_SECRET_ENV = "OAS_AGENT_SHARED_SECRET"
ADMIN_PASSWORD_ENV = "OAS_AGENT_ADMIN_PASSWORD"


def verify_agent_secret(candidate: str | None) -> None:
    """验证 Agent 固定共享密钥；未配置密钥时服务端拒绝暴露 Agent 接口。"""
    expected = os.environ.get(AGENT_SECRET_ENV)
    if not expected:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Agent shared secret 未配置")
    # WebSocket hello 的字段来自任意 JSON 类型；先拒绝非字符串，避免把
    # ``hmac.compare_digest`` 的 TypeError 暴露成 500 或中断鉴权流程。
    if not isinstance(candidate, str) or not candidate or not hmac.compare_digest(candidate, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Agent shared secret 无效")


def verify_admin_password(candidate: str | None) -> None:
    """验证管理 API 专用密码，不能与 Agent 密钥互用。"""
    expected = os.environ.get(ADMIN_PASSWORD_ENV)
    if not expected:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="管理员密码未配置")
    # 保持管理面凭据校验的类型边界与 Agent 密钥一致，避免内部异常泄露。
    if not isinstance(candidate, str) or not candidate or not hmac.compare_digest(candidate, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="管理员密码无效",
            headers={"WWW-Authenticate": "Basic"},
        )
