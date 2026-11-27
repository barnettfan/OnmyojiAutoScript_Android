"""Root Android Agent 的无 ADB shell 命令选择。"""
from __future__ import annotations

import os


def root_shell_command(command: str) -> list[str]:
    """构造本机 Root shell 命令，兼容没有 ``su`` 的 uid 0 redroid init 服务。

    Agent init 已以 root 用户运行时直接使用 Android shell；普通 Root 手机应用进程
    才经 ``su -c`` 提权。这个轻量模块不导入 Device/日志依赖，预检也可安全复用。
    """
    try:
        is_root_process = os.geteuid() == 0
    except AttributeError:
        is_root_process = False
    if is_root_process:
        return ["/system/bin/sh", "-c", command]
    return ["su", "-c", command]
