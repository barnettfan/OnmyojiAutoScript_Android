"""Android Agent 本地任务调度的 Server 快照边界。"""
from __future__ import annotations

import json
import os

from module.exception import ScriptError


def _validate_agent_scheduled_task(task: str, operation: str) -> None:
    """拒绝 Agent 修改不在 Server 快照中的关联任务调度。"""
    serialized = os.environ.get("OAS_AGENT_SCHEDULED_TASKS")
    if serialized is None:
        return
    try:
        allowed = json.loads(serialized)
    except json.JSONDecodeError as exc:
        raise ScriptError("OAS_AGENT_SCHEDULED_TASKS 不是合法 JSON") from exc
    if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
        raise ScriptError("OAS_AGENT_SCHEDULED_TASKS 必须是字符串数组")
    if task not in allowed:
        raise ScriptError(f"Agent {operation} target `{task}` 未在 Server 设备配置中声明")


def validate_agent_task_call(task: str) -> None:
    """拒绝 Agent ``task_call`` 唤醒快照外的关联任务。"""
    _validate_agent_scheduled_task(task, "task_call")


def validate_agent_task_delay(task: str) -> None:
    """拒绝 Agent 仅在本地为快照外任务写入 ``next_run``。

    ``task_call`` 之外，既有任务还可能用 ``set_next_run`` 直接安排关联任务。
    这些写入同样必须由 ``schedule_patch`` 回到 Server；否则 ConfigModel 的默认
    任务会吞掉变更，Server 永远不会知道或派发该关联任务。
    """
    _validate_agent_scheduled_task(task, "task_delay")
