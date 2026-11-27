"""版本化 redroid Agent 镜像的静态交付约束测试。"""
from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2] / "agent"


class ImageContractTest(unittest.TestCase):
    """防止后续修改破坏镜像固化运行时和 init 密钥注入边界。"""

    def test_dockerfile_copies_fixed_runtime_without_server_state(self) -> None:
        """镜像应固化任务/OCR 资源，但绝不能带入设备 JSON 或错误日志。"""
        dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY agent/runtime/oas /opt/oas", dockerfile)
        self.assertIn("COPY agent/oas_android_agent /opt/oas-agent/oas_android_agent", dockerfile)
        self.assertIn("COPY agent/init/oas_android_agent.rc /system/etc/init/oas_android_agent.rc", dockerfile)
        self.assertIn("COPY agent/init/run-agent.sh /opt/oas-agent/run-agent.sh", dockerfile)
        self.assertNotIn("COPY agent/runtime/oas/config/devices", dockerfile)
        self.assertNotIn("COPY agent/runtime/oas/log", dockerfile)
        self.assertNotIn("COPY agent/runtime/oas/.git", dockerfile)
        self.assertIn("OAS_AGENT_NO_RUNTIME_DOWNLOAD=true", dockerfile)
        self.assertIn("pip3 install --no-cache-dir -r /opt/oas-agent/requirements.txt", dockerfile)

    def test_agent_transport_dependency_tree_is_fully_pinned(self) -> None:
        """HTTP/WSS 依赖必须在镜像构建时稳定解析，不能随 PyPI 更新漂移。"""
        requirements = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
        for dependency in (
            "certifi==2023.11.17",
            "charset-normalizer==3.3.2",
            "idna==3.6",
            "requests==2.34.2",
            "urllib3==2.1.0",
            "websockets==13.1",
        ):
            self.assertIn(dependency, requirements)

    def test_init_runner_supports_secret_mount_without_baking_secret(self) -> None:
        """init 环境被清空时仍能读取密钥、导入 Agent 且保持离线运行策略。"""
        runner = (PROJECT_ROOT / "init" / "run-agent.sh").read_text(encoding="utf-8")
        self.assertIn("OAS_AGENT_ENV_FILE", runner)
        self.assertIn("while IFS='=' read -r agent_env_name agent_env_value", runner)
        self.assertIn("OAS_AGENT_SHARED_SECRET", runner)
        self.assertNotIn('. "$agent_env_file"', runner)
        self.assertIn('export OAS_AGENT_NO_RUNTIME_DOWNLOAD=true', runner)
        self.assertIn('export PYTHONPATH="/opt/oas-agent:${OAS_AGENT_RUNTIME_ROOT}', runner)
        self.assertIn('export OAS_AGENT_RUNTIME_ROOT="/opt/oas"', runner)
        self.assertNotIn('OAS_AGENT_RUNTIME_ROOT|', runner)
        self.assertIn('OAS_AGENT_ALLOW_INSECURE_WS)', runner)
        self.assertIn('unset OAS_AGENT_ALLOW_INSECURE_WS', runner)
        self.assertIn('cd "$OAS_AGENT_RUNTIME_ROOT" || exit 70', runner)
        self.assertIn("exec python3 -m oas_android_agent", runner)
        self.assertNotIn("OAS_AGENT_SHARED_SECRET=", runner)

    def test_android_init_waits_for_boot_completed_before_starting_agent(self) -> None:
        """Agent 不能被 late_start 类提前拉起，必须等待 Android 完整启动。"""
        init_script = (PROJECT_ROOT / "init" / "oas_android_agent.rc").read_text(encoding="utf-8")
        self.assertIn("service oas_android_agent /system/bin/sh /opt/oas-agent/run-agent.sh", init_script)
        self.assertIn("    class late_start", init_script)
        self.assertIn("    disabled", init_script)
        self.assertIn("on property:sys.boot_completed=1\n    start oas_android_agent", init_script)


if __name__ == "__main__":
    unittest.main()
