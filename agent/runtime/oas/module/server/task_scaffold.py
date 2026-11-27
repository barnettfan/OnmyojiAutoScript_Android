# This Python file uses the following encoding: utf-8
"""Create the minimum tracked files and registrations for a new OAS task."""
from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import inflection

from module.config.atomicwrites import atomic_write
from module.config.config_menu import ConfigMenu
from module.server.tool import AnnotatorError


PROJECT_ROOT = Path.cwd().resolve()
TASKS_ROOT = PROJECT_ROOT / "tasks"
CONFIG_MODEL_PATH = PROJECT_ROOT / "module" / "config" / "config_model.py"
CONFIG_MENU_PATH = PROJECT_ROOT / "module" / "config" / "config_menu.py"
CONFIG_MANUAL_PATH = PROJECT_ROOT / "module" / "config" / "config_manual.py"
TEMPLATE_PATH = PROJECT_ROOT / "config" / "template.json"
I18N_PATHS = {
    "zh-CN": PROJECT_ROOT / "assets" / "i18n" / "zh-CN.json",
    "en-US": PROJECT_ROOT / "assets" / "i18n" / "en-US.json",
}

_TASK_NAME_PATTERN = re.compile(r"^[A-Z][A-Za-z0-9]*$")
_SCHEDULER_FIELDS = {
    "next_run",
    "priority",
    "success_interval",
    "failure_interval",
    "server_update",
    "delay_date",
    "float_time",
}
_RESERVED_MENU_CATEGORIES = {"Overview", "TaskList", "Tools"}
_PRIORITY_SEGMENTS = {
    "Script": ("Restart", "SoulsTidy"),
    "Soul Zones": ("OtherWorldTwilight", None),
    "Daily Task": ("AutoCheckinBigGod", "RealmRaid"),
    "Liver Emperor Exclusive": ("CustomTask", None),
    "Guild": ("GuildActivityMonitor", None),
    "Weekly Task": ("DraftDuel", None),
    "Activity Task": ("IbukiArena", None),
    "gamehelper": ("CustomTask", None),
}


def _atomic_write_text(path: Path, content: str) -> None:
    """原子写入注册文件，避免服务读取到截断内容。"""
    with atomic_write(path, overwrite=True, encoding="utf-8", newline="\n") as file:
        file.write(content)


def _english_display_name(task_name: str) -> str:
    """将 PascalCase 任务标识转换为默认英文显示名。"""
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", task_name)
    return words.strip() or task_name


def _scheduler_template(overrides: dict[str, Any] | None) -> dict[str, Any]:
    """创建禁用状态的标准调度配置，并仅接受已知调度字段。"""
    scheduler: dict[str, Any] = {
        "enable": False,
        "next_run": "2023-01-01 00:00:00",
        "priority": 5,
        "success_interval": "01 00:00:00",
        "failure_interval": "01 00:00:00",
        "server_update": "09:00:00",
        "delay_date": 1,
        "float_time": "00:00:00",
    }
    if overrides is None:
        return scheduler
    if not isinstance(overrides, dict):
        raise AnnotatorError("invalid_scheduler", "scheduler 必须是对象", 400)
    unknown = set(overrides) - _SCHEDULER_FIELDS - {"enable"}
    if unknown:
        raise AnnotatorError("invalid_scheduler", f"不支持的 scheduler 字段: {', '.join(sorted(unknown))}", 400)
    scheduler.update({key: value for key, value in overrides.items() if key in _SCHEDULER_FIELDS})
    # 新任务绝不随骨架创建自动启用。
    scheduler["enable"] = False
    return scheduler


def _assets_source(task_name: str) -> str:
    """生成尚未添加识别规则时可被安全导入的空资产类。"""
    return (
        "# This file uses the following encoding: utf-8\n"
        "# 此文件会在保存标注规则后由 dev_tools/assets_extract.py 自动覆盖。\n\n"
        f"class {task_name}Assets:\n"
        "    \"\"\"新任务的空识别资产类。\"\"\"\n"
        "    pass\n"
    )


def _config_source(task_name: str, scaffold_type: str) -> str:
    """生成 basic 或 battle 任务需要的最小配置模型。"""
    common = (
        "# This Python file uses the following encoding: utf-8\n"
        "from pydantic import Field\n\n"
        "from tasks.Component.config_base import ConfigBase\n"
        "from tasks.Component.config_scheduler import Scheduler\n"
    )
    if scaffold_type == "battle":
        common += (
            "from tasks.Component.GeneralBattle.config_general_battle import GeneralBattleConfig\n"
            "from tasks.Component.SwitchSoul.switch_soul_config import SwitchSoulConfig\n"
        )
    common += f"\n\nclass {task_name}(ConfigBase):\n"
    common += "    # 新建任务默认关闭，避免骨架代码被调度器自动执行。\n"
    common += "    scheduler: Scheduler = Field(default_factory=Scheduler)\n"
    if scaffold_type == "battle":
        common += "    # 通用战斗行为由后续流程实现按需使用。\n"
        common += "    general_battle_config: GeneralBattleConfig = Field(default_factory=GeneralBattleConfig)\n"
        common += "    # 任务执行前可按配置切换御魂。\n"
        common += "    switch_soul_config: SwitchSoulConfig = Field(default_factory=SwitchSoulConfig)\n"
    return common


def _script_source(task_name: str, scaffold_type: str) -> str:
    """生成不截图、不点击且明确结束的安全任务入口。"""
    imports = (
        "# This Python file uses the following encoding: utf-8\n"
        "from module.exception import TaskEnd\n"
        "from module.logger import logger\n"
        "from tasks.GameUi.game_ui import GameUi\n"
    )
    bases = f"GameUi, {task_name}Assets"
    if scaffold_type == "battle":
        imports += (
            "from tasks.Component.GeneralBattle.general_battle import GeneralBattle\n"
            "from tasks.Component.SwitchSoul.switch_soul import SwitchSoul\n"
        )
        bases = f"GameUi, GeneralBattle, SwitchSoul, {task_name}Assets"
    imports += f"from tasks.{task_name}.assets import {task_name}Assets\n\n\n"
    return (
        imports
        + f"class ScriptTask({bases}):\n"
        + "    \"\"\"新建任务骨架；填充页面识别与流程前不会操作设备。\"\"\"\n\n"
        + "    def run(self) -> None:\n"
        + "        \"\"\"安全结束未实现任务，避免空骨架产生任何游戏操作。\"\"\"\n"
        + f"        logger.warning(\"{task_name} 尚未实现，任务安全结束\")\n"
        + f"        raise TaskEnd(\"{task_name} 尚未实现\")\n"
    )


class TaskScaffolder:
    """为 MCP 提供可验证、拒绝覆盖的新任务骨架创建服务。"""

    def __init__(self, project_root: Path | None = None) -> None:
        root = (project_root or PROJECT_ROOT).resolve()
        self.project_root = root
        self.tasks_root = root / "tasks"
        self.config_model_path = root / "module" / "config" / "config_model.py"
        self.config_menu_path = root / "module" / "config" / "config_menu.py"
        self.config_manual_path = root / "module" / "config" / "config_manual.py"
        self.template_path = root / "config" / "template.json"
        self.i18n_paths = {
            "zh-CN": root / "assets" / "i18n" / "zh-CN.json",
            "en-US": root / "assets" / "i18n" / "en-US.json",
        }

    @staticmethod
    def categories() -> list[str]:
        """返回可放入新任务的现有菜单分类。"""
        return [name for name in ConfigMenu().menu if name not in _RESERVED_MENU_CATEGORIES]

    def _validate(self, task_name: str, category: str, scaffold_type: str) -> tuple[str, str, str]:
        task_name = str(task_name or "").strip()
        category = str(category or "").strip()
        scaffold_type = str(scaffold_type or "").strip().lower()
        if not _TASK_NAME_PATTERN.fullmatch(task_name):
            raise AnnotatorError("invalid_task_name", "任务名必须是 PascalCase 英文标识，例如 MyTask", 400)
        if category not in self.categories():
            raise AnnotatorError("invalid_category", f"菜单分类不存在或不能注册任务: {category}", 400)
        if scaffold_type not in {"basic", "battle"}:
            raise AnnotatorError("invalid_scaffold_type", "骨架类型只支持 basic 或 battle", 400)
        if (self.tasks_root / task_name).exists():
            raise AnnotatorError("task_exists", f"任务目录已存在，拒绝覆盖: {task_name}", 409)
        return task_name, category, scaffold_type

    def _validate_registrations(self, task_name: str, task_key: str) -> None:
        """预先检查每个注册位置，确保不会覆盖已有任务注册。"""
        config_model = self.config_model_path.read_text(encoding="utf-8")
        config_menu = self.config_menu_path.read_text(encoding="utf-8")
        template = json.loads(self.template_path.read_text(encoding="utf-8"))
        i18n_values = {locale: json.loads(path.read_text(encoding="utf-8")) for locale, path in self.i18n_paths.items()}

        if re.search(rf"^from tasks\.{re.escape(task_name)}\.config import ", config_model, flags=re.MULTILINE):
            raise AnnotatorError("task_exists", f"配置模型已注册任务: {task_name}", 409)
        if re.search(rf"^\s*{re.escape(task_key)}:\s*{re.escape(task_name)}\s*=", config_model, flags=re.MULTILINE):
            raise AnnotatorError("task_exists", f"配置字段已注册任务: {task_key}", 409)
        if re.search(rf"[\"']{re.escape(task_name)}[\"']", config_menu):
            raise AnnotatorError("task_exists", f"菜单已注册任务: {task_name}", 409)
        if task_key in template:
            raise AnnotatorError("task_exists", f"配置模板已存在字段: {task_key}", 409)
        if any(task_name in values for values in i18n_values.values()):
            raise AnnotatorError("task_exists", f"i18n 已存在任务名: {task_name}", 409)

    @staticmethod
    def _register_config_model(source: str, task_name: str, task_key: str) -> str:
        import_line = f"from tasks.{task_name}.config import {task_name}\n"
        source = source.replace("\nclass ConfigModel(", f"\n{import_line}\nclass ConfigModel(", 1)
        field = (
            f"    # 由 OAS 任务制作工具注册的新任务配置。\n"
            f"    {task_key}: {task_name} = Field(default_factory={task_name})\n\n"
        )
        return source.replace("    def __init__(self, config_name:", field + "    def __init__(self, config_name:", 1)

    @staticmethod
    def _register_menu(source: str, task_name: str, category: str) -> str:
        insertion = f'        self.menu["{category}"].append("{task_name}")\n'
        marker = "\n    @cached_property\n    def gui_menu"
        return source.replace(marker, f"\n{insertion}{marker}", 1)

    @staticmethod
    def _register_priority(source: str, task_name: str, category: str) -> str:
        start, following = _PRIORITY_SEGMENTS[category]
        if category == "Script":
            old = "Restart > SoulsTidy"
            new = f"Restart > {task_name} > SoulsTidy"
        elif following:
            old = f"> {start} > {following}"
            new = f"> {start} > {task_name} > {following}"
        else:
            old = f"> {start}\n"
            new = f"> {start} > {task_name}\n"
        if old not in source:
            raise AnnotatorError("priority_registration_failed", f"未找到 {category} 的调度优先级段", 500)
        return source.replace(old, new, 1)

    def _planned_sources(
        self,
        task_name: str,
        task_key: str,
        category: str,
        chinese_name: str,
        english_name: str,
        scaffold_type: str,
        scheduler: dict[str, Any],
    ) -> dict[Path, str]:
        """在内存中构建所有注册变更，确认完整后再落盘。"""
        config_model = self._register_config_model(
            self.config_model_path.read_text(encoding="utf-8"), task_name, task_key
        )
        config_menu = self._register_menu(
            self.config_menu_path.read_text(encoding="utf-8"), task_name, category
        )
        config_manual = self._register_priority(
            self.config_manual_path.read_text(encoding="utf-8"), task_name, category
        )
        template = json.loads(self.template_path.read_text(encoding="utf-8"))
        template[task_key] = {"scheduler": scheduler}
        if scaffold_type == "battle":
            template[task_key]["general_battle_config"] = {
                "lock_team_enable": False,
                "preset_enable": False,
                "preset_group": 1,
                "preset_team": 1,
                "green_enable": False,
                "green_mark_type": "choose",
                "green_mark": "green_left1",
                "green_mark_name": "",
                "random_click_swipt_enable": False,
                "battle_timeout": -1,
                "continuous_battle": False,
                "max_continuous": 0,
                "quick_exit": False,
            }
            template[task_key]["switch_soul_config"] = {
                "enable": False,
                "switch_group_team": "-1,-1",
                "enable_switch_by_name": False,
                "group_name": "",
                "team_name": "",
            }
        zh = json.loads(self.i18n_paths["zh-CN"].read_text(encoding="utf-8"))
        en = json.loads(self.i18n_paths["en-US"].read_text(encoding="utf-8"))
        zh[task_name] = chinese_name
        en[task_name] = english_name
        return {
            self.config_model_path: config_model,
            self.config_menu_path: config_menu,
            self.config_manual_path: config_manual,
            self.template_path: json.dumps(template, ensure_ascii=False, indent=2) + "\n",
            self.i18n_paths["zh-CN"]: json.dumps(zh, ensure_ascii=False, indent=2) + "\n",
            self.i18n_paths["en-US"]: json.dumps(en, ensure_ascii=False, indent=2) + "\n",
        }

    def create(
        self,
        task_name: str,
        chinese_name: str,
        category: str,
        scaffold_type: str,
        english_name: str | None = None,
        scheduler: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """创建任务骨架及全部运行时注册；已存在任务一律不覆盖。"""
        task_name, category, scaffold_type = self._validate(task_name, category, scaffold_type)
        chinese_name = str(chinese_name or "").strip()
        if not chinese_name:
            raise AnnotatorError("invalid_display_name", "中文显示名不能为空", 400)
        english_name = str(english_name or "").strip() or _english_display_name(task_name)
        task_key = inflection.underscore(task_name)
        scheduler_value = _scheduler_template(scheduler)
        self._validate_registrations(task_name, task_key)
        planned_sources = self._planned_sources(
            task_name, task_key, category, chinese_name, english_name, scaffold_type, scheduler_value
        )

        stage_dir = Path(tempfile.mkdtemp(prefix=f".{task_name}-", dir=self.tasks_root))
        task_root = self.tasks_root / task_name
        previous_sources = {path: path.read_text(encoding="utf-8") for path in planned_sources}
        try:
            (stage_dir / "res").mkdir()
            (stage_dir / "config.py").write_text(_config_source(task_name, scaffold_type), encoding="utf-8", newline="\n")
            (stage_dir / "script_task.py").write_text(_script_source(task_name, scaffold_type), encoding="utf-8", newline="\n")
            (stage_dir / "assets.py").write_text(_assets_source(task_name), encoding="utf-8", newline="\n")
            # 空规则文件可直接作为后续裁剪和保存规则的目标。
            (stage_dir / "res" / "rules.json").write_text("[]\n", encoding="utf-8", newline="\n")

            for path, content in planned_sources.items():
                _atomic_write_text(path, content)
            stage_dir.replace(task_root)
        except Exception:
            for path, content in previous_sources.items():
                try:
                    _atomic_write_text(path, content)
                except Exception:
                    pass
            raise
        finally:
            if stage_dir.exists():
                shutil.rmtree(stage_dir, ignore_errors=True)

        created_files = [
            task_root / "config.py",
            task_root / "script_task.py",
            task_root / "assets.py",
            task_root / "res" / "rules.json",
        ]
        return {
            "task_name": task_name,
            "task_key": task_key,
            "category": category,
            "scaffold_type": scaffold_type,
            "scheduler_enabled": False,
            "created_files": [str(path.relative_to(self.project_root).as_posix()) for path in created_files],
            "updated_files": [str(path.relative_to(self.project_root).as_posix()) for path in planned_sources],
        }
