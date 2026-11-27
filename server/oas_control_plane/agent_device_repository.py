"""设备配置文件的校验、原子写入与任务调度视图。

Android Agent 的运行态不写入本模块管理的 JSON；这里的文件只保存管理员
配置的静态设备信息、运行时配置和任务调度配置。
"""
from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .scheduler import ScheduleRule, schedule_tasks

# Keep the control plane independent from the Agent's full OAS runtime. Server
# validation must not import OCR, ADB, or task modules merely to read JSON.
SUPPORTED_AGENT_GAME_PACKAGES = frozenset({
    "com.netease.onmyoji.wyzymnqsd_cps",
    "com.netease.onmyoji.mi",
    "com.netease.onmyoji",
    "com.netease.onmyoji.huawei",
    "com.netease.onmyoji.bili",
    "com.netease.onmyoji.vivo",
    "com.netease.onmyoji.m4399",
    "com.netease.onmyoji.nearme.gamecenter",
    "com.tencent.tmgp.yys.zqb",
})


# 下划线会被既有错误日志名解析器当作脚本运行时分隔符，因此设备 ID 只允许连字符。
DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
ANDROID_PACKAGE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$")
# Agent 将 PascalCase 名称转换为 OAS 配置的 snake_case 键；若允许下划线、空格
# 或路径字符，合法的 Server JSON 会在 Agent 写本地快照时失败。这里与 Agent
# ``_native_task_key`` 的输入约束保持一致，让管理员在保存时获得明确错误。
TASK_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,63}$")
# 首批 Root Agent 中这些副本会沿用 ``Script.exception_handler`` 的御魂溢出
# 诊断；命中后会 task_call("SoulsTidy")。在保存设备 JSON 时提前校验关联
# 任务，不能等到任务已在设备上运行才因本地白名单拒绝调度。
SOUL_OVERFLOW_SOURCE_TASKS = frozenset({"Exploration", "Orochi", "EvoZone"})
# Root Agent 会把 ``device.game_package`` 直接写入既有 OAS 的
# ``Device.package_name``。该字段是 PackageName 枚举而非任意 Android 包名，
# 因此控制面必须和运行时使用同一个允许集合，避免保存后才在设备端启动失败。


class DeviceConfigError(ValueError):
    """设备配置不满足控制面约束。"""


class DeviceConfigNotFoundError(DeviceConfigError):
    """请求的设备尚未在 Server 上预配置。"""


class DeviceConfigConflictError(DeviceConfigError):
    """配置修订号已经被其他管理请求更新。"""


@dataclass(frozen=True)
class ScheduledTask:
    """Server 调度视图，字段名与现有 ``TaskScheduler`` 保持一致。"""

    command: str
    priority: int
    next_run: datetime


def validate_device_id(device_id: str) -> str:
    """校验设备标识，防止它被当作文件路径使用。"""
    value = str(device_id or "").strip()
    if not DEVICE_ID_PATTERN.fullmatch(value):
        raise DeviceConfigError("device_id 只能包含字母、数字和连字符，且长度为 1-64")
    return value


def parse_schedule_time(value: Any) -> datetime:
    """解析设备 JSON 中兼容现有 OAS 的本地时间字符串。"""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text = str(value or "").strip()
    if not text:
        raise DeviceConfigError("scheduler.next_run 不能为空")
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError as exc:
        raise DeviceConfigError(f"无效的 scheduler.next_run: {value!r}") from exc


def normalize_schedule_time(value: Any) -> str:
    """把调度时间统一为 OAS 现有配置使用的秒级格式。"""
    return parse_schedule_time(value).replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def version_at_least(actual: str, required: str) -> bool:
    """比较镜像资源版本；版本须是由数字和分隔符组成的递增版本号。"""
    if not required:
        return True
    if actual == required:
        return True
    actual_parts = re.findall(r"\d+", str(actual))
    required_parts = re.findall(r"\d+", str(required))
    if not actual_parts or not required_parts:
        return False
    length = max(len(actual_parts), len(required_parts))
    actual_tuple = tuple(int(item) for item in actual_parts) + (0,) * (length - len(actual_parts))
    required_tuple = tuple(int(item) for item in required_parts) + (0,) * (length - len(required_parts))
    return actual_tuple >= required_tuple


def validate_device_config(data: dict[str, Any], expected_device_id: str | None = None) -> dict[str, Any]:
    """验证并规范化单台设备的持久化配置。"""
    if not isinstance(data, dict):
        raise DeviceConfigError("设备配置必须是 JSON object")

    normalized = copy.deepcopy(data)
    device_id = validate_device_id(normalized.get("device_id"))
    if expected_device_id is not None and device_id != validate_device_id(expected_device_id):
        raise DeviceConfigError("请求路径中的 device_id 与 JSON 内容不一致")
    normalized["device_id"] = device_id

    revision = normalized.get("revision", 0)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise DeviceConfigError("revision 必须是非负整数")
    normalized["revision"] = revision

    required_agent_version = normalized.get("required_agent_version")
    if not isinstance(required_agent_version, str) or not required_agent_version.strip():
        raise DeviceConfigError("required_agent_version 必须是非空字符串")
    normalized["required_agent_version"] = required_agent_version.strip()

    device = normalized.get("device")
    if not isinstance(device, dict):
        raise DeviceConfigError("device 必须是 object")
    if device.get("transport") != "root_shell":
        raise DeviceConfigError("首版 device.transport 必须是 root_shell")
    game_package = device.get("game_package")
    if not isinstance(game_package, str) or not ANDROID_PACKAGE_PATTERN.fullmatch(game_package):
        raise DeviceConfigError("device.game_package 必须是合法 Android 包名")
    if game_package not in SUPPORTED_AGENT_GAME_PACKAGES:
        raise DeviceConfigError("device.game_package 必须是当前 OAS 支持的游戏包名")
    screen_size = device.get("screen_size")
    if not (isinstance(screen_size, list) and len(screen_size) == 2 and all(isinstance(item, int) and item > 0 for item in screen_size)):
        raise DeviceConfigError("device.screen_size 必须是两个正整数")

    runtime = normalized.get("runtime", {})
    if not isinstance(runtime, dict):
        raise DeviceConfigError("runtime 必须是 object")
    normalized["runtime"] = runtime

    tasks = normalized.get("tasks")
    if not isinstance(tasks, dict) or not tasks:
        raise DeviceConfigError("tasks 必须是至少包含一个任务的 object")
    # Agent 单任务运行器在实际任务前复用 OAS 的游戏级恢复流程。除 Restart
    # 自身外，所有任务都可能在游戏未运行时调用 Restart；省略该配置会使本地
    # 恢复修改无法形成合法的 Server schedule_patch，故必须在同一快照声明。
    if "Restart" not in tasks:
        raise DeviceConfigError("tasks 必须包含 Restart，供 Agent 执行游戏级恢复")
    for task_name, task_config in tasks.items():
        if not isinstance(task_name, str) or not TASK_NAME_PATTERN.fullmatch(task_name):
            raise DeviceConfigError("任务名只能包含字母和数字，且必须以字母开头、长度为 1-64")
        if not isinstance(task_config, dict):
            raise DeviceConfigError(f"任务 {task_name} 的配置必须是 object")
        scheduler = task_config.get("scheduler")
        if not isinstance(scheduler, dict):
            raise DeviceConfigError(f"任务 {task_name} 缺少 scheduler")
        if not isinstance(scheduler.get("enable"), bool):
            raise DeviceConfigError(f"任务 {task_name} 的 scheduler.enable 必须是 bool")
        scheduler["next_run"] = normalize_schedule_time(scheduler.get("next_run"))
        priority = scheduler.get("priority", 5)
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise DeviceConfigError(f"任务 {task_name} 的 scheduler.priority 必须是整数")
        scheduler["priority"] = priority
        agent = task_config.get("agent", {})
        if not isinstance(agent, dict):
            raise DeviceConfigError(f"任务 {task_name} 的 agent 必须是 object")
        minimum_resource_version = agent.get("minimum_resource_version", "")
        if minimum_resource_version is not None and not isinstance(minimum_resource_version, str):
            raise DeviceConfigError(f"任务 {task_name} 的 agent.minimum_resource_version 必须是字符串")
        if minimum_resource_version:
            agent["minimum_resource_version"] = minimum_resource_version.strip()
        normalized_agent = agent if agent else None
        if normalized_agent is None:
            task_config.pop("agent", None)
        else:
            task_config["agent"] = normalized_agent

    # GotoMain 是一次性恢复命令，没有独立的周期任务配置。它只能由管理员
    # 通过手动执行接口下发，不能被过期 next_run 自动调度。
    goto_main_scheduler = tasks.get("GotoMain", {}).get("scheduler")
    if isinstance(goto_main_scheduler, dict) and goto_main_scheduler.get("enable") is True:
        raise DeviceConfigError("GotoMain 只能手动执行，scheduler.enable 必须为 false")

    # 原 OAS 的 RestartConfig.enable_daily 默认值是 true。若设备 JSON 漏掉
    # restart_config，Agent 本地 ConfigUpdater 会补入这个桌面默认值，并在
    # Restart 后隐式 task_call("DailyTrifles")，绕开 Server 明确授权的任务
    # 快照。C/S 配置必须把该开关写实；缺省时保守关闭，管理员要启用时再显式
    # 填 true，并确保关联任务也存在于同一份权威 JSON。
    restart_task = tasks["Restart"]
    restart_config = restart_task.get("restart_config")
    if restart_config is None:
        restart_config = {"enable_daily": False}
        restart_task["restart_config"] = restart_config
    if not isinstance(restart_config, dict):
        raise DeviceConfigError("Restart.restart_config 必须是 object")
    enable_daily = restart_config.get("enable_daily", False)
    if not isinstance(enable_daily, bool):
        raise DeviceConfigError("Restart.restart_config.enable_daily 必须是 bool")
    restart_config["enable_daily"] = enable_daily
    if enable_daily and "DailyTrifles" not in tasks:
        raise DeviceConfigError(
            "Restart.restart_config.enable_daily=true 时必须同时配置 DailyTrifles"
        )

    # 这些基础副本在御魂达到上限时会由既有全局异常处理唤醒 SoulsTidy。
    # 即使副本当前关闭，也可能被管理员手动执行；因此只要设备 JSON 声明了
    # 其中任意任务，就必须同时声明收尾任务，确保 schedule_patch 有合法目标。
    soul_overflow_sources = sorted(SOUL_OVERFLOW_SOURCE_TASKS.intersection(tasks))
    if soul_overflow_sources and "SoulsTidy" not in tasks:
        raise DeviceConfigError(
            f"任务 {', '.join(soul_overflow_sources)} 可能触发御魂溢出，必须同时配置 SoulsTidy"
        )

    # 当前首批镜像没有迁移以下关联任务。对应开关若放行，OAS 会通过
    # set_next_run 只改设备本地的默认 ConfigModel；Agent 调度守卫会拒绝该
    # 写入，但应当在管理员保存设备 JSON 时就给出可操作的错误信息。
    exploration_config = tasks.get("Exploration", {}).get("scrolls", {})
    if isinstance(exploration_config, dict):
        scrolls_enable = exploration_config.get("scrolls_enable", False)
        if not isinstance(scrolls_enable, bool):
            raise DeviceConfigError("Exploration.scrolls.scrolls_enable 必须是 bool")
        if scrolls_enable:
            raise DeviceConfigError(
                "首批 Agent 尚未迁移 Exploration 绘卷模式所需的 RealmRaid 和 MemoryScrolls"
            )

    orochi_config = tasks.get("Orochi", {}).get("orochi_config", {})
    if isinstance(orochi_config, dict):
        check_true_orochi = orochi_config.get("check_true_orochi_enable", False)
        if not isinstance(check_true_orochi, bool):
            raise DeviceConfigError("Orochi.orochi_config.check_true_orochi_enable 必须是 bool")
        if check_true_orochi:
            raise DeviceConfigError("首批 Agent 尚未迁移 Orochi 真蛇检测所需的 TrueOrochi")

    return normalized


class DeviceConfigRepository:
    """管理 ``config/devices/<device_id>.json`` 的单实例文件仓库。"""

    def __init__(self, root: Path | str = Path("config") / "devices") -> None:
        self.root = Path(root)
        self._lock = threading.RLock()

    def list_ids(self) -> list[str]:
        """返回所有符合安全命名规则的设备标识。"""
        if not self.root.exists():
            return []
        return sorted(
            path.stem for path in self.root.glob("*.json") if DEVICE_ID_PATTERN.fullmatch(path.stem)
        )

    def read(self, device_id: str) -> dict[str, Any]:
        """读取并校验一台设备的配置快照。"""
        path = self._path(device_id)
        with self._lock:
            if not path.exists():
                raise DeviceConfigNotFoundError(f"设备 {device_id} 未预配置")
            try:
                with path.open("r", encoding="utf-8") as file:
                    data = json.load(file)
            except (OSError, json.JSONDecodeError) as exc:
                raise DeviceConfigError(f"无法读取设备 {device_id} 的 JSON 配置: {exc}") from exc
        return validate_device_config(data, device_id)

    def replace(
        self,
        device_id: str,
        data: dict[str, Any],
        expected_revision: int | None,
    ) -> dict[str, Any]:
        """原子替换配置，并在成功后只递增一次修订号。"""
        device_id = validate_device_id(device_id)
        candidate = validate_device_config(data, device_id)
        path = self._path(device_id)
        with self._lock:
            current_revision: int | None = None
            if path.exists():
                current_revision = self.read(device_id)["revision"]
            if expected_revision != current_revision:
                raise DeviceConfigConflictError(
                    f"revision 冲突: expected={expected_revision}, current={current_revision}"
                )
            candidate["revision"] = 1 if current_revision is None else current_revision + 1
            self._atomic_write(path, candidate)
        return copy.deepcopy(candidate)

    def apply_schedule_patch(
        self,
        device_id: str,
        patch: dict[str, Any],
        expected_revision: int,
    ) -> dict[str, Any]:
        """把 Agent 任务调度补丁合并到权威 JSON 并原子写入。"""
        if not isinstance(patch, dict) or not isinstance(patch.get("tasks"), dict):
            raise DeviceConfigError("schedule_patch 必须包含 tasks object")

        with self._lock:
            current = self.read(device_id)
            if current["revision"] != expected_revision:
                raise DeviceConfigConflictError(
                    f"schedule_patch revision 冲突: expected={expected_revision}, current={current['revision']}"
                )
            for task_name, task_patch in patch["tasks"].items():
                if task_name not in current["tasks"]:
                    raise DeviceConfigError(f"schedule_patch 包含未知任务: {task_name}")
                if not isinstance(task_patch, dict) or not isinstance(task_patch.get("scheduler"), dict):
                    raise DeviceConfigError(f"任务 {task_name} 的补丁必须包含 scheduler")
                scheduler_patch = task_patch["scheduler"]
                unsupported = set(scheduler_patch) - {"enable", "next_run"}
                if unsupported:
                    raise DeviceConfigError(f"任务 {task_name} 包含不允许的调度字段: {sorted(unsupported)}")
                if "enable" in scheduler_patch:
                    if not isinstance(scheduler_patch["enable"], bool):
                        raise DeviceConfigError(f"任务 {task_name} 的 scheduler.enable 必须是 bool")
                    current["tasks"][task_name]["scheduler"]["enable"] = scheduler_patch["enable"]
                if "next_run" in scheduler_patch:
                    current["tasks"][task_name]["scheduler"]["next_run"] = normalize_schedule_time(
                        scheduler_patch["next_run"]
                    )

            # 补丁只允许修改调度字段，但这些字段仍可能改变完整配置的
            # 业务约束（例如把仅供管理员手动执行的 GotoMain 周期启用）。
            # 合并后重新走统一校验，避免 schedule_patch 成为绕过配置编辑器
            # 约束的第二条写入路径。
            validated = validate_device_config(current, device_id)
            validated["revision"] = current["revision"] + 1
            self._atomic_write(self._path(device_id), validated)
        return copy.deepcopy(validated)

    def due_tasks(self, device_id: str, now: datetime | None = None) -> list[str]:
        """按既有 OAS 规则返回全部到期任务，供控制面跳过不可用项。

        Agent 镜像升级期间，一份设备 JSON 可能短暂包含当前镜像不支持的任务。
        返回完整候选队列让控制面尝试后续任务，避免一个不可执行任务长期占住
        设备；首项仍严格遵守既有 Filter/FIFO/Priority 排序。
        """
        now = (now or datetime.now()).replace(tzinfo=None)
        config = self.read(device_id)
        pending: list[ScheduledTask] = []
        for task_name, task_config in config["tasks"].items():
            scheduler = task_config["scheduler"]
            if not scheduler["enable"]:
                continue
            next_run = parse_schedule_time(scheduler["next_run"])
            if next_run <= now:
                pending.append(ScheduledTask(task_name, scheduler["priority"], next_run))
        if not pending:
            return []
        rule_text = str(
            config.get("runtime", {}).get("script", {}).get("optimization", {}).get("schedule_rule", "Filter")
        )
        try:
            rule = ScheduleRule(rule_text)
        except ValueError:
            rule = ScheduleRule.FILTER
        ordered = schedule_tasks(rule, pending)
        # Filter 规则只会返回开发者定义的任务；自定义任务未在固定优先表中时，
        # 使用稳定的 Priority 兜底，避免已到期任务永久不被调度。即使 Filter
        # 已有结果，也把未命中的任务附在其后：控制面仅会在前面的任务因镜像
        # 不兼容而不能租约时才尝试它们，不改变正常 OAS 的首选调度顺序。
        fallback = sorted(pending, key=lambda item: (item.priority, item.next_run, item.command))
        ordered = ordered or fallback
        seen: set[str] = set()
        candidates: list[str] = []
        for task in [*ordered, *fallback]:
            if task.command in seen:
                continue
            seen.add(task.command)
            candidates.append(task.command)
        return candidates

    def due_task(self, device_id: str, now: datetime | None = None) -> str | None:
        """兼容单任务调用方，返回 OAS 调度顺序中的第一个到期任务。"""
        tasks = self.due_tasks(device_id, now)
        return tasks[0] if tasks else None

    def _path(self, device_id: str) -> Path:
        return self.root / f"{validate_device_id(device_id)}.json"

    @staticmethod
    def _atomic_write(path: Path, data: dict[str, Any]) -> None:
        """临时文件 fsync 后 replace，保证读者只会看到完整 JSON。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
                json.dump(data, file, ensure_ascii=False, indent=2, sort_keys=True)
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
