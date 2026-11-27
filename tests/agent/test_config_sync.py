"""Android Agent 配置快照转换的独立回归测试。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from module.config.config_model import ConfigModel
from oas_android_agent.config_sync import ConfigSyncError, RuntimeConfigStore, native_task_keys


def snapshot() -> dict:
    """构造 Server 已确认的最小配置快照。"""
    return {
        "device_id": "redroid-01",
        "revision": 3,
        "required_agent_version": "2026.11.26",
        "device": {"transport": "root_shell", "game_package": "com.netease.onmyoji", "screen_size": [1280, 720]},
        "runtime": {"script": {"error": {"save_error": True}}},
        "tasks": {
            "Restart": {"scheduler": {"enable": True, "next_run": "2026-11-26 01:00:00", "priority": 0}},
            "LBS": {"scheduler": {"enable": False, "next_run": "2026-11-26 02:00:00", "priority": 5}},
        },
    }


class RuntimeConfigStoreTest(unittest.TestCase):
    """确保 RootShell 强制配置且只回传允许的调度字段。"""

    def test_confirmed_snapshot_becomes_local_root_shell_config_and_patch(self) -> None:
        """运行时配置变更只导出 enable/next_run，不导出截图或完整任务配置。"""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = RuntimeConfigStore(root / "runtime", root / "state", "redroid-01")
            self.assertEqual(store.save_confirmed(snapshot()), 3)
            native = store.load_confirmed()
            self.assertEqual(native["revision"], 3)
            generated = __import__("json").loads(store.runtime_config_path.read_text(encoding="utf-8"))
            self.assertEqual(store.config_name, "agent-redroid-01")
            self.assertEqual(generated["config_name"], "agent-redroid-01")
            self.assertEqual(generated["script"]["device"]["screenshot_method"], "root_shell")
            self.assertEqual(generated["script"]["device"]["control_method"], "root_shell")
            self.assertEqual(generated["lbs"]["scheduler"]["enable"], False)
            generated["restart"]["scheduler"]["next_run"] = "2026-11-27 01:00:00"
            store.runtime_config_path.write_text(__import__("json").dumps(generated), encoding="utf-8")
            self.assertEqual(store.schedule_patch_from_runtime(snapshot()), {
                "tasks": {"Restart": {"scheduler": {"next_run": "2026-11-27 01:00:00"}}}
            })

    def test_native_task_keys_are_the_task_call_allowlist(self) -> None:
        """Agent 子进程允许唤醒的键必须完整来自 Server 快照。"""
        self.assertEqual(native_task_keys(snapshot()), ["lbs", "restart"])

    def test_runner_failure_can_disable_current_runtime_task_for_server_patch(self) -> None:
        """子进程未启动时暂停任务也只能通过 schedule_patch 写回 Server。"""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = RuntimeConfigStore(root / "runtime", root / "state", "redroid-01")
            current = snapshot()
            self.assertEqual(store.save_confirmed(current), 3)
            self.assertTrue(store.disable_task_in_runtime("Restart"))
            self.assertEqual(
                store.schedule_patch_from_runtime(current),
                {"tasks": {"Restart": {"scheduler": {"enable": False}}}},
            )

    def test_restart_daily_setting_stays_explicit_in_local_runtime_file(self) -> None:
        """Server 关闭每日链路时，本地合并不得回退到 OAS 的 true 默认值。"""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = RuntimeConfigStore(root / "runtime", root / "state", "redroid-01")
            current = snapshot()
            current["tasks"]["Restart"]["restart_config"] = {"enable_daily": False}

            store.save_confirmed(current)

            generated = __import__("json").loads(store.runtime_config_path.read_text(encoding="utf-8"))
            self.assertFalse(generated["restart"]["restart_config"]["enable_daily"])

    def test_unknown_game_package_is_rejected_before_runtime_file_is_written(self) -> None:
        """旧 Server 或损坏快照不能让 Agent 写出 OAS 无法解析的配置。"""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = RuntimeConfigStore(root / "runtime", root / "state", "redroid-01")
            current = snapshot()
            current["device"]["game_package"] = "com.example.onmyoji"

            with self.assertRaisesRegex(ConfigSyncError, "当前 OAS 支持"):
                store.save_confirmed(current)

            self.assertFalse(store.runtime_config_path.exists())

    def test_known_oas_channel_game_package_stays_accepted(self) -> None:
        """已有渠道服包名应可生成 RootShell 本地运行时配置。"""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = RuntimeConfigStore(root / "runtime", root / "state", "redroid-01")
            current = snapshot()
            current["device"]["game_package"] = "com.netease.onmyoji.bili"

            store.save_confirmed(current)

            generated = __import__("json").loads(store.runtime_config_path.read_text(encoding="utf-8"))
            self.assertEqual(generated["script"]["device"]["package_name"], "com.netease.onmyoji.bili")

    def test_first_wave_template_generates_config_model_accepted_by_oas(self) -> None:
        """首批设备模板转换后必须能直接实例化既有 OAS ConfigModel。"""
        template_path = (
            Path(__file__).resolve().parents[2]
            / "server"
            / "config"
            / "template.agent-device.json"
        )
        template = json.loads(template_path.read_text(encoding="utf-8"))
        template["device_id"] = "template-model-redroid"
        template["revision"] = 1

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = RuntimeConfigStore(root / "runtime", root / "state", template["device_id"])
            store.save_confirmed(template)
            generated = json.loads(store.runtime_config_path.read_text(encoding="utf-8"))

        model = ConfigModel(**generated)

        self.assertEqual(model.script.device.package_name.value, "com.netease.onmyoji")
        self.assertEqual(model.script.device.screenshot_method.value, "root_shell")
        self.assertEqual(model.script.device.control_method.value, "root_shell")
        self.assertFalse(model.restart.restart_config.enable_daily)
        self.assertFalse(model.daily_trifles.scheduler.enable)


if __name__ == "__main__":
    unittest.main()
