"""把 Server 设备快照转换为 Agent 本地 OAS 配置文件。"""
from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from tasks.Script.config_device import PackageName


# Server 快照会直接映射到 OAS 的 ``Device.package_name`` 枚举。与 Server
# 使用同一枚举校验旧快照，避免配置来源异常时在任务子进程初始化阶段才失败。
SUPPORTED_AGENT_GAME_PACKAGES = frozenset(
    package.value for package in PackageName if package is not PackageName.AUTO
)


class ConfigSyncError(ValueError):
    """Server 下发的配置快照无法安全转换为 OAS 本地配置。"""


def _native_task_key(task_name: str) -> str:
    """把 OAS 的 PascalCase 任务名映射回当前配置 JSON 的 snake_case 键。"""
    # 当前 OAS 中唯一的全大写任务名；其余名称按常规 PascalCase 处理。
    if task_name == "LBS":
        return "lbs"
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", task_name):
        raise ConfigSyncError(f"不支持的任务名: {task_name!r}")
    return re.sub(r"(?<!^)(?=[A-Z])", "_", task_name).lower()


def native_task_keys(snapshot: dict[str, Any]) -> list[str]:
    """返回 Server 快照中允许 OAS ``task_call`` 唤醒的本地任务键。"""
    tasks = snapshot.get("tasks")
    if not isinstance(tasks, dict):
        raise ConfigSyncError("配置快照缺少 tasks object")
    return sorted(_native_task_key(str(task_name)) for task_name in tasks)


class RuntimeConfigStore:
    """保存已确认快照，并生成既有 OAS 任务运行时读取的 config JSON。"""

    def __init__(self, runtime_root: Path, state_root: Path, device_id: str) -> None:
        self.runtime_root = runtime_root
        self.state_root = state_root
        self.device_id = device_id
        # 使用连字符避免 OAS 的日志名规范化器把下划线后的设备 ID 截断为 ``agent``。
        self.config_name = f"agent-{device_id}"
        self.snapshot_path = state_root / "confirmed-config.json"
        self.runtime_config_path = runtime_root / "config" / f"{self.config_name}.json"

    def save_confirmed(self, snapshot: dict[str, Any], *, activate_runtime: bool = True) -> int:
        """校验后缓存 Server 快照；执行中可只缓存，避免覆盖当前任务配置。"""
        revision = self._validate(snapshot)
        self._atomic_write_json(self.snapshot_path, snapshot)
        if activate_runtime:
            self._atomic_write_json(self.runtime_config_path, self._build_native_config(snapshot))
        return revision

    def load_confirmed(self) -> dict[str, Any] | None:
        """读取最后一次确认的快照；缓存不可用时返回 None 以等待 Server hello。"""
        try:
            snapshot = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
            self._validate(snapshot)
            return snapshot
        except (OSError, json.JSONDecodeError, ConfigSyncError):
            return None

    def activate_confirmed_runtime(self) -> int | None:
        """将已确认快照切换为下一次 OAS 任务读取的运行时配置。

        Agent 在已有任务租约时仅缓存管理员的新快照，避免覆盖运行中的
        ``config/agent-<device>.json``。收到该任务的最终结果确认后，由常驻
        Agent 调用本方法安全地激活缓存版本；缓存损坏时保持现有运行时文件，
        等待 Server 下一次完整快照而不制造默认配置。
        """
        snapshot = self.load_confirmed()
        if snapshot is None:
            return None
        revision = self._validate(snapshot)
        self._atomic_write_json(self.runtime_config_path, self._build_native_config(snapshot))
        return revision

    def schedule_patch_from_runtime(self, snapshot: dict[str, Any]) -> dict[str, Any] | None:
        """只提取本地任务状态机变更的 enable/next_run，绝不回传完整配置。"""
        try:
            native = json.loads(self.runtime_config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        changed: dict[str, Any] = {}
        for task_name, task in snapshot["tasks"].items():
            native_task = native.get(_native_task_key(task_name))
            native_scheduler = native_task.get("scheduler") if isinstance(native_task, dict) else None
            source_scheduler = task.get("scheduler") if isinstance(task, dict) else None
            if not isinstance(native_scheduler, dict) or not isinstance(source_scheduler, dict):
                continue
            update: dict[str, Any] = {}
            for key in ("enable", "next_run"):
                if key in native_scheduler and native_scheduler[key] != source_scheduler.get(key):
                    update[key] = native_scheduler[key]
            if update:
                changed[task_name] = {"scheduler": update}
        return {"tasks": changed} if changed else None

    def disable_task_in_runtime(self, task_name: str) -> bool:
        """暂停本地运行时任务，供 Agent 子进程无法启动时安全回传调度补丁。

        正常 OAS 任务会在子进程内自行处理异常。若 Python 子进程根本未启动或
        未生成结果文件，常驻 Agent 仍须阻止 Server 用同一过期 ``next_run``
        立即重试。只修改 ``scheduler.enable``，完整配置仍由 Server 持有。
        """
        try:
            native = json.loads(self.runtime_config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        task = native.get(_native_task_key(task_name))
        scheduler = task.get("scheduler") if isinstance(task, dict) else None
        if not isinstance(scheduler, dict) or not isinstance(scheduler.get("enable"), bool):
            return False
        if not scheduler["enable"]:
            return False
        scheduler["enable"] = False
        self._atomic_write_json(self.runtime_config_path, native)
        return True

    def _validate(self, snapshot: dict[str, Any]) -> int:
        """验证 Agent 使用快照所必需的字段。"""
        if not isinstance(snapshot, dict):
            raise ConfigSyncError("配置快照必须是 object")
        if snapshot.get("device_id") != self.device_id:
            raise ConfigSyncError("配置快照 device_id 与 Agent 不一致")
        revision = snapshot.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ConfigSyncError("配置快照 revision 必须是正整数")
        device = snapshot.get("device")
        if not isinstance(device, dict) or device.get("transport") != "root_shell":
            raise ConfigSyncError("Agent 仅支持 device.transport=root_shell")
        game_package = device.get("game_package")
        if not isinstance(game_package, str) or not game_package:
            raise ConfigSyncError("配置快照缺少 device.game_package")
        if game_package not in SUPPORTED_AGENT_GAME_PACKAGES:
            raise ConfigSyncError("配置快照的 device.game_package 不受当前 OAS 支持")
        if not isinstance(snapshot.get("runtime", {}), dict) or not isinstance(snapshot.get("tasks"), dict):
            raise ConfigSyncError("配置快照缺少 runtime 或 tasks object")
        return revision

    def _build_native_config(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """合并公共运行时与按任务配置，强制使用 RootShell Device 后端。"""
        native = copy.deepcopy(snapshot.get("runtime", {}))
        native["config_name"] = self.config_name
        native["running_task"] = ""
        script = native.setdefault("script", {})
        device = script.setdefault("device", {})
        device.update(
            {
                "serial": "root-shell",
                "package_name": snapshot["device"]["game_package"],
                "screenshot_method": "root_shell",
                "control_method": "root_shell",
                "adb_restart": False,
            }
        )
        for task_name, task_config in snapshot["tasks"].items():
            native[_native_task_key(task_name)] = copy.deepcopy(task_config)
        return native

    @staticmethod
    def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
        """以同目录临时文件替换，保证任务不会读到半截配置。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
                json.dump(data, file, ensure_ascii=False, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_name, path)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
