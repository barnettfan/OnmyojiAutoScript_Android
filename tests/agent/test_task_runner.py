"""单任务 Agent 的调度兜底测试。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from oas_android_agent.config_sync import RuntimeConfigStore
from oas_android_agent.task_runner import (
    _write_result,
    _supervisor_is_alive,
    _shutdown_local_agent_rpc,
    disable_runtime_task_for_manual_handling,
    disable_task_for_manual_handling,
    ensure_task_runtime_ready,
    ensure_task_rescheduled,
    run_task,
)
from module.exception import TaskEnd


class _FakeConfig:
    """只实现调度兜底所需的 Config 接口，避免测试启动 OAS/Root shell。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def task_delay(self, task_name: str, *, start_time: datetime, success: bool) -> None:
        """模拟既有 Config.task_delay，并写入 OAS 运行时 JSON。"""
        data = json.loads(self.path.read_text(encoding="utf-8"))
        # 这个测试只覆盖 Restart，使用显式键避免复制 OAS 的命名转换实现。
        data["restart"]["scheduler"]["next_run"] = (
            start_time.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
        )
        self.path.write_text(json.dumps(data), encoding="utf-8")


class TaskRunnerScheduleTest(unittest.TestCase):
    """验证成功/失败任务不会因没有主动 set_next_run 而反复到期。"""

    @staticmethod
    def _snapshot(next_run: str = "2026-11-26 00:00:00") -> dict:
        """提供可由 RuntimeConfigStore 转换的最小 Server 任务快照。"""
        return {
            "device_id": "redroid-01",
            "revision": 1,
            "required_agent_version": "2026.11.26",
            "device": {"transport": "root_shell", "game_package": "com.netease.onmyoji"},
            "runtime": {},
            "tasks": {
                "Restart": {
                    "scheduler": {"enable": True, "next_run": next_run, "priority": 0}
                }
            },
        }

    def _runtime_config(self, path: Path, next_run: str = "2026-11-26 00:00:00") -> None:
        path.write_text(
            json.dumps(
                {
                    "restart": {
                        "scheduler": {
                            "enable": True,
                            "next_run": next_run,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

    def test_unchanged_scheduler_gets_failure_delay_and_schedule_patch(self) -> None:
        """失败任务的兜底 next_run 能被 Server 补丁提取，避免立即重复租约。"""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = RuntimeConfigStore(root / "runtime", root / "state", "redroid-01")
            snapshot = self._snapshot()
            store.save_confirmed(snapshot)
            runtime_path = store.runtime_config_path
            before = {"enable": True, "next_run": "2026-11-26 00:00:00"}
            applied = ensure_task_rescheduled(
                _FakeConfig(runtime_path),
                runtime_path,
                "Restart",
                before,
                datetime(2026, 11, 26, 1, 2, 3, 456789),
                False,
            )
            self.assertTrue(applied)
            changed = json.loads(runtime_path.read_text(encoding="utf-8"))
            self.assertEqual(changed["restart"]["scheduler"]["next_run"], "2026-11-26 01:02:03")
            self.assertEqual(
                store.schedule_patch_from_runtime(snapshot),
                {"tasks": {"Restart": {"scheduler": {"next_run": "2026-11-26 01:02:03"}}}},
            )

    def test_explicit_task_schedule_change_is_not_overwritten(self) -> None:
        """任务自行 set_next_run 时保留其成功/失败专用规则。"""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime_path = root / "agent-redroid-01.json"
            self._runtime_config(runtime_path, next_run="2099-01-01 00:00:00")
            before = {"enable": True, "next_run": "2026-11-26 00:00:00"}
            self.assertFalse(
                ensure_task_rescheduled(
                    _FakeConfig(runtime_path),
                    runtime_path,
                    "Restart",
                    before,
                    datetime(2026, 11, 26, 1, 2, 3),
                    True,
                )
            )
            self.assertEqual(
                json.loads(runtime_path.read_text(encoding="utf-8"))["restart"]["scheduler"]["next_run"],
                "2099-01-01 00:00:00",
            )

    def test_missing_scheduler_does_not_mutate_config(self) -> None:
        """没有可比较的调度字段时保持原配置，交由结果错误路径处理。"""
        with tempfile.TemporaryDirectory() as folder:
            runtime_path = Path(folder) / "agent-redroid-01.json"
            runtime_path.write_text("{}", encoding="utf-8")
            config = _FakeConfig(runtime_path)
            self.assertFalse(
                ensure_task_rescheduled(
                    config,
                    runtime_path,
                    "Restart",
                    None,
                    datetime(2026, 11, 26, 1, 2, 3),
                    True,
                )
            )

    def test_manual_required_disables_only_the_current_task(self) -> None:
        """人工接管会暂停当前任务，避免 Server 以过期 next_run 自动重派。"""
        scheduler = SimpleNamespace(enable=True)

        class ManualConfig:
            """提供 ConfigModel 与 save 的最小替身。"""

            model = SimpleNamespace(restart=SimpleNamespace(scheduler=scheduler))
            saved = False

            def save(self) -> None:
                """记录调度开关写入操作。"""
                self.saved = True

        config = ManualConfig()
        self.assertTrue(disable_task_for_manual_handling(config, "Restart"))
        self.assertFalse(scheduler.enable)
        self.assertTrue(config.saved)

    def test_early_failure_disables_runtime_task_without_config_instance(self) -> None:
        """OCR/导入阶段失败时也暂停任务，不能因缺少 Config 而无限重派。"""
        with tempfile.TemporaryDirectory() as folder:
            runtime_path = Path(folder) / "agent-redroid-01.json"
            self._runtime_config(runtime_path)
            self.assertTrue(disable_runtime_task_for_manual_handling(runtime_path, "Restart"))
            changed = json.loads(runtime_path.read_text(encoding="utf-8"))
            self.assertFalse(changed["restart"]["scheduler"]["enable"])

    def test_runtime_task_disable_keeps_previous_json_if_atomic_replace_fails(self) -> None:
        """失败路径写入中断时不能破坏旧配置并丢失后续 schedule_patch。"""
        with tempfile.TemporaryDirectory() as folder:
            runtime_path = Path(folder) / "agent-redroid-01.json"
            self._runtime_config(runtime_path)
            original = runtime_path.read_text(encoding="utf-8")

            with patch("oas_android_agent.task_runner.os.replace", side_effect=OSError("disk unavailable")):
                with self.assertRaisesRegex(OSError, "disk unavailable"):
                    disable_runtime_task_for_manual_handling(runtime_path, "Restart")

            self.assertEqual(runtime_path.read_text(encoding="utf-8"), original)
            self.assertEqual(list(Path(folder).glob(".agent-redroid-01.*.tmp")), [])

    def test_result_write_keeps_previous_json_if_atomic_replace_fails(self) -> None:
        """任务子进程中断写结果时，常驻 Agent 仍能读到上一次完整结果。"""
        with tempfile.TemporaryDirectory() as folder:
            result_path = Path(folder) / "result.json"
            result_path.write_text('{"status":"failure","summary":"previous"}', encoding="utf-8")
            original = result_path.read_text(encoding="utf-8")

            with patch("oas_android_agent.task_runner.os.replace", side_effect=OSError("disk unavailable")):
                with self.assertRaisesRegex(OSError, "disk unavailable"):
                    _write_result(result_path, {"status": "success", "summary": "new"})

            self.assertEqual(result_path.read_text(encoding="utf-8"), original)
            self.assertEqual(list(Path(folder).glob(".result.*.tmp")), [])

    def test_supervisor_watchdog_accepts_only_direct_parent(self) -> None:
        """看门逻辑只接受当前 Agent 进程作为直接父进程，避免误杀测试宿主。"""
        with patch("oas_android_agent.task_runner.os.getppid", return_value=1234):
            self.assertTrue(_supervisor_is_alive(1234))
            self.assertFalse(_supervisor_is_alive(5678))
            self.assertFalse(_supervisor_is_alive(1))
            self.assertFalse(_supervisor_is_alive(0))

    def test_runner_explicitly_releases_local_rpc_services(self) -> None:
        """单任务结果落盘前会回收其本机 OCR/图像 RPC 子服务。"""
        stopped: list[str] = []
        ocr_module = types.ModuleType("module.ocr.rpc")
        ocr_module.shutdown_ocr_server = lambda: stopped.append("ocr")
        image_module = types.ModuleType("module.image.rpc")
        image_module.shutdown_image_server = lambda: stopped.append("image")

        with patch.dict(sys.modules, {
            "module.ocr.rpc": ocr_module,
            "module.image.rpc": image_module,
        }):
            _shutdown_local_agent_rpc()

        self.assertEqual(stopped, ["ocr", "image"])

    def test_runtime_preparation_retries_once_after_existing_restart_recovery(self) -> None:
        """游戏级恢复请求重调度后，同一已租约任务重新检查并继续执行。"""
        class Runtime:
            """模拟 OAS 先恢复游戏、随后确认运行态正常的控制器。"""

            def __init__(self) -> None:
                self.calls: list[str] = []
                self.decisions = ["reschedule", "ready"]

            def prepare_task_execution(self, task_name: str) -> str:
                """依次返回恢复请求与恢复后的可执行状态。"""
                self.calls.append(task_name)
                return self.decisions.pop(0)

        runtime = Runtime()
        script = SimpleNamespace(runtime=runtime)

        self.assertTrue(ensure_task_runtime_ready(script, "Exploration"))
        self.assertEqual(runtime.calls, ["Exploration", "Exploration"])

    def test_runtime_preparation_does_not_run_task_when_existing_recovery_failed(self) -> None:
        """运行态恢复失败时，运行器应返回失败而不是直接尝试截图或触控。"""
        class Runtime:
            """模拟 OAS 已明确给出不可执行的恢复结论。"""

            def prepare_task_execution(self, _task_name: str) -> str:
                """返回失败，验证运行器不再重试或继续任务。"""
                return "failed"

        self.assertFalse(ensure_task_runtime_ready(SimpleNamespace(runtime=Runtime()), "Exploration"))

    def test_stop_during_runtime_preparation_reports_cancelled_without_disabling_task(self) -> None:
        """RootShell 停止标记在任务预检阶段也必须以 cancelled 收尾。"""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime_root = root / "runtime"
            config_root = runtime_root / "config"
            config_root.mkdir(parents=True)
            config_path = config_root / "agent-redroid-01.json"
            self._runtime_config(config_path)
            result_path = root / "result.json"
            stop_file = root / "stop"
            stop_file.touch()
            original_cwd = Path.cwd()

            class FakeConfig:
                """提供预检停止路径所需的最小 OAS 配置接口。"""

                def __init__(self) -> None:
                    self.model = SimpleNamespace(running_task="")

                def save(self) -> None:
                    """模拟任务开始前写入 running_task。"""

                def task_delay(self, _task_name: str, *, start_time: datetime, success: bool) -> None:
                    """模拟取消后的失败间隔回写。"""
                    data = json.loads(config_path.read_text(encoding="utf-8"))
                    data["restart"]["scheduler"]["next_run"] = start_time.replace(microsecond=0).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    config_path.write_text(json.dumps(data), encoding="utf-8")

            class FakeRuntime:
                """模拟 RootShell 在游戏恢复检查中观察到停止标记。"""

                def prepare_task_execution(self, _task_name: str) -> None:
                    """在 Script.run 前抛出 TaskEnd，覆盖原来遗漏的分支。"""
                    raise TaskEnd("Agent stop requested")

            class FakeScript:
                """避免测试启动真实 OAS、OCR 或 Root shell。"""

                def __init__(self, _config_name: str) -> None:
                    self.config = FakeConfig()
                    self.runtime = FakeRuntime()

            logger_module = types.ModuleType("module.logger")
            logger_module.logger = SimpleNamespace(set_file_logger=lambda *_args, **_kwargs: None)
            runtime_module = types.ModuleType("module.agent_runtime")
            runtime_module.configure_local_agent_rpc = lambda: None
            image_module = types.ModuleType("module.image.rpc")
            image_module.ensure_image_server_ready = lambda: True
            image_module.shutdown_image_server = lambda: True
            ocr_module = types.ModuleType("module.ocr.rpc")
            ocr_module.ensure_ocr_server_ready = lambda: True
            ocr_module.shutdown_ocr_server = lambda: True
            script_module = types.ModuleType("script")
            script_module.Script = FakeScript

            try:
                with patch.dict(sys.modules, {
                    "module.logger": logger_module,
                    "module.agent_runtime": runtime_module,
                    "module.image.rpc": image_module,
                    "module.ocr.rpc": ocr_module,
                    "script": script_module,
                }):
                    exit_code = run_task(runtime_root, "agent-redroid-01", "Restart", result_path, stop_file)
            finally:
                os.chdir(original_cwd)

            result = json.loads(result_path.read_text(encoding="utf-8"))
            runtime = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 1)
            self.assertEqual(result["status"], "cancelled")
            self.assertEqual(result["summary"], "stop_marker_observed_scheduler_fallback")
            self.assertTrue(runtime["restart"]["scheduler"]["enable"])


if __name__ == "__main__":
    unittest.main()
