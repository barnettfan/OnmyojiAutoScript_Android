"""固定镜像任务清单的静态一致性测试。"""
from __future__ import annotations

import unittest
import ast
import json
from pathlib import Path

from oas_android_agent.task_manifest import TASK_MANIFEST, task_minimum_resource_version


PROJECT_ROOT = Path(__file__).resolve().parents[2] / "agent" / "runtime" / "oas"


class TaskManifestTest(unittest.TestCase):
    """避免镜像清单与仓库中的可执行 OAS 任务目录漂移。"""

    def test_manifest_names_are_unique_and_have_task_implementations(self) -> None:
        """每个可派发名称必须对应现有任务目录和 script_task.py。"""
        names = [str(item.get("name") or "") for item in TASK_MANIFEST]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(names)
        for item in TASK_MANIFEST:
            name = str(item["name"])
            self.assertRegex(name, r"^[A-Za-z][A-Za-z0-9]{0,63}$")
            self.assertTrue(str(item.get("minimum_resource_version") or "").strip())
            task_path = PROJECT_ROOT / "tasks" / name
            self.assertTrue(task_path.is_dir(), name)
            self.assertTrue((task_path / "script_task.py").is_file(), name)

    def test_manifest_lookup_does_not_accept_unknown_tasks(self) -> None:
        """Server/Agent 清单查询对未知任务必须返回 None，不能动态导入。"""
        for item in TASK_MANIFEST:
            self.assertEqual(task_minimum_resource_version(item["name"]), item["minimum_resource_version"])
        self.assertIsNone(task_minimum_resource_version("UnknownTask"))

    def test_device_template_declares_every_first_wave_task(self) -> None:
        """首批镜像任务必须在设备模板中显式声明，供 Server 授权和调度补丁回写。"""
        template = json.loads(
            (
                PROJECT_ROOT.parents[2]
                / "server"
                / "config"
                / "template.agent-device.json"
            ).read_text(encoding="utf-8")
        )
        tasks = template.get("tasks")
        self.assertIsInstance(tasks, dict)
        manifest_names = {item["name"] for item in TASK_MANIFEST}
        self.assertEqual(set(tasks), manifest_names)
        for task_name, config in tasks.items():
            self.assertIsInstance(config, dict, task_name)
            self.assertIsInstance(config.get("scheduler"), dict, task_name)
            self.assertFalse(
                config["scheduler"].get("enable"),
                f"{task_name} must stay disabled until its Root redroid acceptance is complete",
            )

    def test_first_wave_task_imports_do_not_bypass_root_shell(self) -> None:
        """首批任务的 tasks 依赖不能重新引入 ADB 或 uiautomator2 旁路。

        ``module.device`` 仍保留桌面/模拟器后端实现，但首批任务本身只能通过
        RootShell 统一接口操作设备。这里沿着 tasks 内的静态 import 闭包检查，
        允许兼容分支存在时也必须在同一个方法内明确判断 ``is_root_shell``。
        """
        source_paths = self._first_wave_task_sources()
        for path in source_paths:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            parents = {
                child: parent
                for parent in ast.walk(tree)
                for child in ast.iter_child_nodes(parent)
            }

            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in {
                    "adbutils", "uiautomator2"
                }:
                    self.fail(f"{path.relative_to(PROJECT_ROOT)} imports {node.module}")
                if isinstance(node, ast.Import) and any(
                    alias.name.split(".")[0] in {"adbutils", "uiautomator2"}
                    for alias in node.names
                ):
                    self.fail(f"{path.relative_to(PROJECT_ROOT)} imports an ADB transport module")
                if not isinstance(node, ast.Attribute) or node.attr not in {
                    "adb", "adb_shell", "adb_command", "draw_adb", "swipe_adb",
                    "click_adb", "long_click_adb", "uiautomator2", "u2",
                }:
                    continue
                self.assertTrue(
                    self._is_legacy_transport_compatibility_branch(node, parents),
                    f"{path.relative_to(PROJECT_ROOT)}:{node.lineno} uses {node.attr} outside a non-RootShell branch",
                )

    @staticmethod
    def _is_legacy_transport_compatibility_branch(
        node: ast.AST,
        parents: dict[ast.AST, ast.AST],
    ) -> bool:
        """确认遗留调用实际受 ``is_root_shell`` 条件分流，而非只同函数出现判断。"""
        cursor = node
        while cursor in parents:
            parent = parents[cursor]
            if isinstance(parent, ast.If) and any(
                isinstance(item, ast.Attribute) and item.attr == "is_root_shell"
                for item in ast.walk(parent.test)
            ):
                in_else = any(cursor is item for branch in parent.orelse for item in ast.walk(branch))
                test_is_negated = isinstance(parent.test, ast.UnaryOp) and isinstance(parent.test.op, ast.Not)
                in_body = any(cursor is item for branch in parent.body for item in ast.walk(branch))
                if in_else or (test_is_negated and in_body):
                    return True
            cursor = parent
        return False

    @staticmethod
    def _first_wave_task_sources() -> set[Path]:
        """解析首批任务的 tasks-only import 闭包，避免漏掉间接组件调用。"""
        queue = [PROJECT_ROOT / "tasks" / item["name"] / "script_task.py" for item in TASK_MANIFEST]
        seen: set[Path] = set()

        def resolve(module: str, current: Path, level: int = 0) -> Path | None:
            if level:
                base = current.parent
                for _ in range(level - 1):
                    base = base.parent
                if module:
                    base = base.joinpath(*module.split("."))
            else:
                if not module.startswith("tasks"):
                    return None
                base = PROJECT_ROOT.joinpath(*module.split("."))
            for candidate in (base.with_suffix(".py"), base / "__init__.py"):
                if candidate.is_file():
                    return candidate
            return None

        while queue:
            path = queue.pop()
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                candidates: list[Path | None] = []
                if isinstance(node, ast.ImportFrom):
                    candidates.append(resolve(node.module or "", path, node.level))
                elif isinstance(node, ast.Import):
                    candidates.extend(resolve(alias.name, path) for alias in node.names)
                for candidate in candidates:
                    if candidate is not None and candidate not in seen:
                        queue.append(candidate)
        return seen


if __name__ == "__main__":
    unittest.main()
