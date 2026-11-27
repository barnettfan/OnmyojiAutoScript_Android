"""Root Android Agent 专用的本地 RPC 运行时约束。"""
from __future__ import annotations

import os
from typing import Any


LOCAL_RPC_ONLY_ENV = "OAS_AGENT_LOCAL_RPC_ONLY"
LOCAL_AGENT_EXECUTION_ENV = "OAS_AGENT_EXECUTION_LEASE"
LOCAL_RPC_HOST = "127.0.0.1"
LOCAL_OCR_ADDRESS = "127.0.0.1:22268"
LOCAL_IMAGE_ADDRESS = "127.0.0.1:22269"


def local_agent_rpc_only() -> bool:
    """返回当前进程是否必须把图像与 OCR RPC 限制在容器 loopback。"""
    return os.environ.get(LOCAL_RPC_ONLY_ENV, "").strip().lower() in {"1", "true", "yes"}


def local_agent_execution() -> bool:
    """返回当前任务是否由 Server 已授权的 Android Agent 租约启动。

    ``Restart.scheduler.enable`` 在 C/S 配置里只控制 Server 是否把 Restart
    作为周期任务派发。其它已租约任务发现游戏未运行时，仍须在同一租约内调用
    Restart 完成游戏级恢复；该标记仅由 Agent 子进程写入，不能影响桌面调度器。
    """
    return os.environ.get(LOCAL_AGENT_EXECUTION_ENV, "").strip().lower() in {"1", "true", "yes"}


def configure_local_agent_rpc(deploy_config: Any | None = None) -> None:
    """强制 Agent 子进程使用本机图像/OCR 服务，不传输正常截图。"""
    if not local_agent_rpc_only():
        return
    if deploy_config is None:
        from module.server.setting import State

        deploy_config = State.deploy_config

    # Agent 每次任务在自己的容器内拉起 RPC 子服务；不能继承 Server 的远端地址。
    deploy_config.StartImageServer = True
    deploy_config.ImageServerPort = 22269
    deploy_config.ImageClientAddress = LOCAL_IMAGE_ADDRESS
    deploy_config.StartOcrServer = True
    deploy_config.OcrServerPort = 22268
    deploy_config.OcrClientAddress = LOCAL_OCR_ADDRESS
