"""Root Android Agent 的无 ADB shell 命令选择。"""
from __future__ import annotations

import os


def root_shell_command(command: str) -> list[str]:
    """构造本机 Root shell 命令，兼容没有 ``su`` 的 uid 0 redroid init 服务。

    Agent init 已以 root 用户运行时直接使用 Android shell；普通 Root 手机应用进程
    才经 ``su -c`` 提权。这个轻量模块不导入 Device/日志依赖，预检也可安全复用。
    """
    # PRoot 会把进程呈现为 uid 0，但 Android 内核中的实际调用身份仍是 Termux
    # 应用用户。Root 手机部署可显式指定真实 Android su 的绝对路径，避免误把
    # PRoot 的虚拟 root 当成系统 root，从而使截图与触控权限不足。
    root_su_bin = os.environ.get("OAS_AGENT_ROOT_SU_BIN", "").strip()
    if root_su_bin:
        return [root_su_bin, "-c", command]
    try:
        is_root_process = os.geteuid() == 0
    except AttributeError:
        is_root_process = False
    if is_root_process:
        return ["/system/bin/sh", "-c", command]
    return ["su", "-c", command]
