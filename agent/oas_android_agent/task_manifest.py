"""随版本化镜像发布的首批 RootShell 任务清单。"""
from __future__ import annotations

from . import AGENT_VERSION


# 这些任务只通过 OAS 的统一 Device 接口截图、触控和进程控制；因此可直接复用
# RootShell 后端。新增任务必须随镜像版本更新此清单，不能由 Server 动态下发代码。
TASK_MANIFEST: tuple[dict[str, str], ...] = (
    {"name": "Restart", "minimum_resource_version": AGENT_VERSION},
    {"name": "GotoMain", "minimum_resource_version": AGENT_VERSION},
    # 高频每日事项复用统一截图/OCR/RootShell；捐赠昵称经本机剪贴板输入。
    {"name": "DailyTrifles", "minimum_resource_version": AGENT_VERSION},
    {"name": "Exploration", "minimum_resource_version": AGENT_VERSION},
    {"name": "Orochi", "minimum_resource_version": AGENT_VERSION},
    {"name": "EvoZone", "minimum_resource_version": AGENT_VERSION},
    {"name": "GoldYoukai", "minimum_resource_version": AGENT_VERSION},
    {"name": "ExperienceYoukai", "minimum_resource_version": AGENT_VERSION},
    {"name": "Sougenbi", "minimum_resource_version": AGENT_VERSION},
    # 基础副本达到御魂上限时会由 Script 调度此任务，必须与副本同镜像发布。
    {"name": "SoulsTidy", "minimum_resource_version": AGENT_VERSION},
)


def task_minimum_resource_version(task_name: str) -> str | None:
    """返回固定镜像中任务的最低资源版本；未知任务返回 ``None``。"""
    for item in TASK_MANIFEST:
        if item["name"] == task_name:
            return item["minimum_resource_version"]
    return None


def resource_version_at_least(actual: str, required: str) -> bool:
    """按数字段比较资源版本，避免字符串排序造成 2026.10/2026.9 误判。"""
    import re

    actual_parts = re.findall(r"\d+", str(actual))
    required_parts = re.findall(r"\d+", str(required))
    if not actual_parts or not required_parts:
        return str(actual) == str(required)
    length = max(len(actual_parts), len(required_parts))
    actual_tuple = tuple(int(item) for item in actual_parts) + (0,) * (length - len(actual_parts))
    required_tuple = tuple(int(item) for item in required_parts) + (0,) * (length - len(required_parts))
    return actual_tuple >= required_tuple
