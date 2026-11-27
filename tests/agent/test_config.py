"""Agent 环境配置边界测试。"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oas_android_agent.config import AgentConfigurationError, AgentSettings, StateStore


class AgentSettingsTest(unittest.TestCase):
    """验证部署环境变量不能逃逸 Agent 的状态目录。"""

    def test_rejects_unsafe_device_id(self) -> None:
        """设备编号同时是本地文件名，不能含路径或下划线。"""
        with tempfile.TemporaryDirectory() as folder:
            environment = {
                "OAS_AGENT_DEVICE_ID": "../escape",
                "OAS_AGENT_SERVER_URL": "wss://server.example",
                "OAS_AGENT_SHARED_SECRET": "secret",
                "OAS_AGENT_RUNTIME_ROOT": str(Path(folder)),
            }
            with patch.dict(os.environ, environment, clear=True):
                with self.assertRaisesRegex(AgentConfigurationError, "只能包含"):
                    AgentSettings.from_environment()

    def test_invalid_persisted_execution_state_is_discarded(self) -> None:
        """Agent 不能信任可被损坏或手工编辑的 state.json 文件名字段。"""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            root.joinpath("state.json").write_text(
                '{"confirmed_revision":1,"current_execution_id":"../../escape","current_task_name":"Restart","current_config_revision":1}',
                encoding="utf-8",
            )
            state = StateStore(root).load()

        self.assertIsNone(state.current_execution_id)
        self.assertIsNone(state.current_task_name)

    def test_rejects_non_numeric_heartbeat_interval(self) -> None:
        """部署环境的心跳间隔错误必须转成明确的 Agent 配置异常。"""
        with tempfile.TemporaryDirectory() as folder:
            environment = {
                "OAS_AGENT_DEVICE_ID": "redroid-01",
                "OAS_AGENT_SERVER_URL": "wss://server.example",
                "OAS_AGENT_SHARED_SECRET": "secret",
                "OAS_AGENT_RUNTIME_ROOT": str(Path(folder)),
                "OAS_AGENT_HEARTBEAT_SECONDS": "not-a-number",
            }
            with patch.dict(os.environ, environment, clear=True):
                with self.assertRaisesRegex(AgentConfigurationError, "必须是整数"):
                    AgentSettings.from_environment()

    def test_rejects_empty_or_relative_state_directory(self) -> None:
        """状态 outbox 不能因空变量或相对路径悄悄写入 Agent 当前目录。"""
        with tempfile.TemporaryDirectory() as folder:
            base_environment = {
                "OAS_AGENT_DEVICE_ID": "redroid-01",
                "OAS_AGENT_SERVER_URL": "wss://server.example",
                "OAS_AGENT_SHARED_SECRET": "secret",
                "OAS_AGENT_RUNTIME_ROOT": str(Path(folder)),
            }
            for state_root in ("", "relative-state"):
                with self.subTest(state_root=state_root), patch.dict(
                    os.environ,
                    {**base_environment, "OAS_AGENT_STATE_ROOT": state_root},
                    clear=True,
                ):
                    with self.assertRaisesRegex(AgentConfigurationError, "STATE_ROOT 必须是非空绝对路径"):
                        AgentSettings.from_environment()

    def test_rejects_credentials_embedded_in_server_url(self) -> None:
        """WSS 地址不能把用户名密码带入进程环境或连接日志。"""
        with tempfile.TemporaryDirectory() as folder:
            environment = {
                "OAS_AGENT_DEVICE_ID": "redroid-01",
                "OAS_AGENT_SERVER_URL": "wss://user:password@server.example",
                "OAS_AGENT_SHARED_SECRET": "secret",
                "OAS_AGENT_RUNTIME_ROOT": str(Path(folder)),
            }
            with patch.dict(os.environ, environment, clear=True):
                with self.assertRaisesRegex(AgentConfigurationError, "不能包含用户名或密码"):
                    AgentSettings.from_environment()

    def test_rejects_url_path_query_and_invalid_port(self) -> None:
        """统一的 WSS/HTTPS 根地址不能含会破坏固定路径拼接的额外部分。"""
        with tempfile.TemporaryDirectory() as folder:
            base_environment = {
                "OAS_AGENT_DEVICE_ID": "redroid-01",
                "OAS_AGENT_SHARED_SECRET": "secret",
                "OAS_AGENT_RUNTIME_ROOT": str(Path(folder)),
            }
            for url, message in (
                ("wss://server.example/custom-prefix", "path"),
                ("wss://server.example?tenant=1", "query"),
                ("wss://server.example:not-a-port", "端口无效"),
            ):
                with self.subTest(url=url):
                    environment = {**base_environment, "OAS_AGENT_SERVER_URL": url}
                    with patch.dict(os.environ, environment, clear=True):
                        with self.assertRaisesRegex(AgentConfigurationError, message):
                            AgentSettings.from_environment()


if __name__ == "__main__":
    unittest.main()
