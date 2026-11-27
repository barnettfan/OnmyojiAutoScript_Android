"""常驻 Agent 的配置激活与 Server 序列校验测试。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import websockets

from oas_android_agent.config import AgentSettings
from oas_android_agent.service import AndroidAgent


def config_snapshot(revision: int) -> dict:
    """返回用于 Agent hello/lease 的最小 RootShell 配置快照。"""
    return {
        "device_id": "redroid-01",
        "revision": revision,
        "required_agent_version": "2026.11.26",
        "device": {"transport": "root_shell", "game_package": "com.netease.onmyoji", "screen_size": [1280, 720]},
        "runtime": {},
        "tasks": {"Restart": {"scheduler": {"enable": True, "next_run": "2026-11-26 00:00:00", "priority": 0}}},
    }


class AgentProtocolTest(unittest.IsolatedAsyncioTestCase):
    """验证 Agent 不接受重放命令，也不在任务执行中覆写本地配置。"""

    async def asyncSetUp(self) -> None:
        """在临时目录初始化不连接网络的 Agent。"""
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        runtime = root / "runtime"
        runtime.mkdir()
        settings = AgentSettings(
            device_id="redroid-01",
            server_url="ws://127.0.0.1:9999",
            shared_secret="test-secret",
            runtime_root=runtime,
            state_root=root / "state",
            agent_version="2026.11.26",
            resource_version="2026.11.26",
            heartbeat_seconds=15,
        )
        self.agent = AndroidAgent(settings)

    async def asyncTearDown(self) -> None:
        """删除测试状态目录。"""
        self.tempdir.cleanup()

    async def test_server_sequence_replay_is_rejected(self) -> None:
        """同一 WebSocket 会话中相同序列号不能重复执行命令。"""
        hello = {
            "type": "hello_ack",
            "accepted": True,
            "sent_at": "2026-11-26T00:00:00",
            "sequence": 1,
            "config_revision": 1,
            "config": config_snapshot(1),
        }
        await self.agent._handle_message(hello)
        with self.assertRaisesRegex(ValueError, "increase strictly"):
            await self.agent._handle_message({"type": "heartbeat_ack", "sent_at": "2026-11-26T00:00:01", "sequence": 1})

    async def test_server_message_for_another_device_is_rejected_before_sequence_update(self) -> None:
        """Agent 不得因 Server 路由错误接受其它设备的命令。"""
        with self.assertRaisesRegex(ValueError, "device_id does not match"):
            await self.agent._handle_message({
                "type": "heartbeat_ack",
                "device_id": "redroid-02",
                "sent_at": "2026-11-26T00:00:00",
                "sequence": 1,
            })

        self.assertEqual(self.agent.last_server_sequence, -1)

    async def test_live_websocket_handshake_sends_secret_snapshot_and_health_revision(self) -> None:
        """真实 loopback 帧必须包含共享密钥，并按确认快照回报预检版本。"""
        received: list[dict] = []

        async def protocol_server(websocket: object) -> None:
            """模拟最小 Server 协议端点，不涉及游戏、ADB 或真实外网。"""
            hello = json.loads(await websocket.recv())
            received.append(hello)
            config = config_snapshot(1)
            await websocket.send(json.dumps({
                "type": "hello_ack",
                "accepted": True,
                "device_id": "redroid-01",
                "sent_at": "2026-11-26T00:00:00",
                "sequence": 1,
                "config_revision": 1,
                "config": config,
            }))
            health = json.loads(await websocket.recv())
            received.append(health)
            await websocket.send(json.dumps({
                "type": "health_report_ack",
                "device_id": "redroid-01",
                "sent_at": "2026-11-26T00:00:01",
                "sequence": 2,
            }))
            # 关闭连接令 _serve_connection 正常离开循环；生产 run_forever 会
            # 捕获该断线并执行重连，这里只验证单次真实握手的载荷和顺序。
            await websocket.close()

        server = await websockets.serve(protocol_server, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        agent = AndroidAgent(replace(
            self.agent.settings,
            server_url=f"ws://127.0.0.1:{port}",
        ))
        healthy = {"status": "healthy", "reason": None, "checks": {"root": True}}
        try:
            with patch.object(agent, "_preflight", return_value=healthy):
                async with websockets.connect(agent.settings.server_url) as websocket:
                    agent.websocket = websocket
                    with self.assertRaises(websockets.ConnectionClosed):
                        await agent._serve_connection(websocket)
        finally:
            agent.websocket = None
            server.close()
            await server.wait_closed()

        self.assertEqual(received[0]["type"], "hello")
        self.assertEqual(received[0]["secret"], "test-secret")
        self.assertEqual(received[0]["device_id"], "redroid-01")
        self.assertEqual(received[1]["type"], "health_report")
        self.assertEqual(received[1]["config_revision"], 1)
        self.assertEqual(agent.state.confirmed_revision, 1)
        self.assertEqual(agent.config_store.load_confirmed()["revision"], 1)

    async def test_second_agent_cannot_acquire_the_same_device_state_lock(self) -> None:
        """同一 state_root 的第二个 Agent 必须在建 WSS 前失败，避免并发触控。"""
        second = AndroidAgent(self.agent.settings)
        locked = False

        def flock(_fd: int, operation: int) -> None:
            """模拟同一 POSIX 文件锁在第二次独占获取时失败。"""
            nonlocal locked
            if operation & 4:
                locked = False
                return
            if locked:
                raise OSError("lock is already held")
            locked = True

        fake_fcntl = SimpleNamespace(LOCK_EX=1, LOCK_NB=2, LOCK_UN=4, flock=flock)
        with patch.dict(sys.modules, {"fcntl": fake_fcntl}):
            self.agent._acquire_instance_lock()
            try:
                with self.assertRaisesRegex(RuntimeError, "already owns device"):
                    second._acquire_instance_lock()
            finally:
                self.agent._release_instance_lock()
            second._acquire_instance_lock()
            second._release_instance_lock()

    async def test_agent_rejects_task_outside_fixed_manifest(self) -> None:
        """即使收到错误租约，Agent 也不会把未知任务交给本地 OAS。"""
        sent: list[dict] = []

        class FakeWebSocket:
            """收集拒绝结果。"""

            async def send(self, raw: str) -> None:
                """解析 Agent 上行消息。"""
                sent.append(json.loads(raw))

        self.agent.websocket = FakeWebSocket()
        await self.agent._receive_lease({
            "execution_id": "execution-unknown",
            "task_name": "NotInManifest",
            "config_revision": 1,
            "expires_at": "2099-01-01T00:00:00+00:00",
            "config": config_snapshot(1),
        })

        self.assertEqual(sent[0]["type"], "task_result")
        self.assertEqual(sent[0]["status"], "rejected")
        self.assertIsNone(self.agent.pending_lease)
        self.assertFalse(self.agent.config_store.snapshot_path.exists())

    async def test_agent_rejects_task_when_server_requires_newer_resources(self) -> None:
        """Agent 本地资源版本低于设备任务要求时不启动任务。"""
        sent: list[dict] = []

        class FakeWebSocket:
            """收集拒绝结果。"""

            async def send(self, raw: str) -> None:
                """解析 Agent 上行消息。"""
                sent.append(json.loads(raw))

        self.agent.websocket = FakeWebSocket()
        config = config_snapshot(1)
        config["tasks"]["Restart"]["agent"] = {"minimum_resource_version": "2099.01.01"}
        await self.agent._receive_lease({
            "execution_id": "execution-new-resource",
            "task_name": "Restart",
            "config_revision": 1,
            "expires_at": "2099-01-01T00:00:00+00:00",
            "config": config,
        })

        self.assertEqual(sent[0]["status"], "rejected")
        self.assertIn("below required", sent[0]["summary"])
        self.assertIsNone(self.agent.pending_lease)

    async def test_agent_rejects_lease_task_absent_from_configuration(self) -> None:
        """固定镜像支持任务也必须存在于租约快照，不能落入 OAS 默认配置。"""
        sent: list[dict] = []

        class FakeWebSocket:
            """收集 Agent 的租约拒绝结果。"""

            async def send(self, raw: str) -> None:
                """解析上行 JSON，供断言检查。"""
                sent.append(json.loads(raw))

        self.agent.websocket = FakeWebSocket()
        await self.agent._receive_lease({
            "execution_id": "execution-absent-config",
            "task_name": "GotoMain",
            "config_revision": 1,
            "expires_at": "2099-01-01T00:00:00+00:00",
            "config": config_snapshot(1),
        })

        self.assertEqual(sent[0]["status"], "rejected")
        self.assertIn("absent from the lease configuration", sent[0]["summary"])
        self.assertIsNone(self.agent.pending_lease)
        self.assertFalse(self.agent.config_store.snapshot_path.exists())

    async def test_agent_rejects_lease_with_mismatched_snapshot_revision(self) -> None:
        """命令 revision 与随附快照不一致时不能启动，避免版本混用。"""
        sent: list[dict] = []

        class FakeWebSocket:
            """收集 Agent 的租约拒绝结果。"""

            async def send(self, raw: str) -> None:
                """解析上行 JSON，供断言检查。"""
                sent.append(json.loads(raw))

        self.agent.websocket = FakeWebSocket()
        await self.agent._receive_lease({
            "execution_id": "execution-revision-mismatch",
            "task_name": "Restart",
            "config_revision": 2,
            "expires_at": "2099-01-01T00:00:00+00:00",
            "config": config_snapshot(1),
        })

        self.assertEqual(sent[0]["status"], "rejected")
        self.assertIn("does not match", sent[0]["summary"])
        self.assertIsNone(self.agent.pending_lease)

    async def test_agent_rejects_expired_task_lease_before_caching_config(self) -> None:
        """过期租约不能写入快照或等待后续 start_task。"""
        sent: list[dict] = []

        class FakeWebSocket:
            """收集过期租约的拒绝结果。"""

            async def send(self, raw: str) -> None:
                """解析 Agent 上行消息。"""
                sent.append(json.loads(raw))

        self.agent.websocket = FakeWebSocket()
        await self.agent._receive_lease({
            "execution_id": "execution-expired-lease",
            "task_name": "Restart",
            "config_revision": 1,
            "expires_at": "2000-01-01T00:00:00+00:00",
            "config": config_snapshot(1),
        })

        self.assertEqual(sent[0]["status"], "rejected")
        self.assertIn("already expired", sent[0]["summary"])
        self.assertIsNone(self.agent.pending_lease)
        self.assertFalse(self.agent.config_store.snapshot_path.exists())

        # Server 通常已将 start_task 写入同一条连接。第一份 rejected result
        # 足以释放租约；Agent 必须忽略该延迟帧，避免第二次结果断开 WSS。
        await self.agent._start_task({
            "execution_id": "execution-expired-lease",
            "task_name": "Restart",
            "config_revision": 1,
        })
        self.assertEqual(len(sent), 1)

    def test_lease_expiry_requires_timezone_and_normalizes_to_utc(self) -> None:
        """跨时区部署必须拒绝无偏移租约，并按 UTC 比较有效租约。"""
        with self.assertRaisesRegex(ValueError, "timezone offset"):
            self.agent._parse_lease_expiry("2099-01-01T00:00:00")
        expires_at = self.agent._parse_lease_expiry("2099-01-01T08:00:00+08:00")
        self.assertEqual(expires_at.tzinfo, timezone.utc)
        self.assertEqual(expires_at.isoformat(), "2099-01-01T00:00:00+00:00")

    async def test_new_confirmed_config_does_not_overwrite_active_task_file(self) -> None:
        """任务执行中收到更高 revision 时，子进程仍读取租约开始时的配置。"""
        self.agent._confirm_config(config_snapshot(1), 1)
        original = json.loads(self.agent.config_store.runtime_config_path.read_text(encoding="utf-8"))
        self.agent.state.current_execution_id = "execution-1"
        self.agent.state.current_config_revision = 1
        self.agent._confirm_config(config_snapshot(2), 2)
        active = json.loads(self.agent.config_store.runtime_config_path.read_text(encoding="utf-8"))
        self.assertEqual(original, active)
        self.assertEqual(self.agent.config_store.load_confirmed()["revision"], 2)

    async def test_new_config_does_not_overwrite_pending_lease_runtime_file(self) -> None:
        """管理员更新配置不能改变已接收但尚未启动租约的本地快照。"""
        leased = config_snapshot(1)
        leased["runtime"] = {"script": {"optimization": {"screenshot_interval": 0.5}}}
        newer = config_snapshot(2)
        newer["runtime"] = {"script": {"optimization": {"screenshot_interval": 0.25}}}
        self.agent._confirm_config(leased, 1)
        self.agent.pending_lease = __import__("oas_android_agent.service", fromlist=["PendingLease"]).PendingLease(
            "execution-pending-config", "Restart", 1, leased, datetime.now(timezone.utc) + timedelta(days=1)
        )

        with patch.object(self.agent, "_report_preflight") as report_preflight:
            await self.agent._handle_message({
                "type": "heartbeat_ack",
                "sent_at": "2026-11-26T00:00:01",
                "sequence": 1,
                "config_revision": 2,
                "config": newer,
            })

        runtime = json.loads(self.agent.config_store.runtime_config_path.read_text(encoding="utf-8"))
        self.assertEqual(runtime["script"]["optimization"]["screenshot_interval"], 0.5)
        self.assertEqual(self.agent.config_store.load_confirmed()["revision"], 2)
        self.assertEqual(self.agent._pending_preflight_config, newer)
        report_preflight.assert_not_awaited()

    async def test_second_pending_lease_cannot_replace_first_lease(self) -> None:
        """另一执行编号的 task_lease 不得覆盖已经固定的待启动租约。"""
        sent: list[dict] = []

        class FakeWebSocket:
            """收集 Agent 对第二份租约的拒绝结果。"""

            async def send(self, raw: str) -> None:
                """解析并保存协议消息。"""
                sent.append(json.loads(raw))

        self.agent.websocket = FakeWebSocket()
        first = config_snapshot(1)
        self.agent.pending_lease = __import__("oas_android_agent.service", fromlist=["PendingLease"]).PendingLease(
            "execution-first-pending", "Restart", 1, first, datetime.now(timezone.utc) + timedelta(days=1)
        )

        await self.agent._receive_lease({
            "execution_id": "execution-second-pending",
            "task_name": "Restart",
            "config_revision": 1,
            "expires_at": "2099-01-01T00:00:00+00:00",
            "config": first,
        })

        self.assertEqual(self.agent.pending_lease.execution_id, "execution-first-pending")
        self.assertEqual(sent[-1]["type"], "task_result")
        self.assertEqual(sent[-1]["status"], "rejected")
        self.assertIn("pending", sent[-1]["summary"])

    async def test_agent_restart_converts_unpersisted_execution_to_result_outbox(self) -> None:
        """Agent 在子进程写结果前重启时，不能让 Server 租约永久悬挂。"""
        self.agent.state.current_execution_id = "execution-interrupted"
        self.agent.state.current_task_name = "Restart"
        self.agent.state.current_config_revision = 1
        self.agent.state_store.save(self.agent.state)

        restarted = AndroidAgent(self.agent.settings)
        # 恢复 outbox 是持有单实例锁后的状态变更；构造第二个对象本身不应在
        # 锁竞争发生前改写状态目录。
        self.assertFalse(restarted._outbox_path("execution-interrupted").exists())
        restarted._instance_lock_fd = 1
        restarted._recover_interrupted_execution_after_lock()

        payload = json.loads(restarted._outbox_path("execution-interrupted").read_text(encoding="utf-8"))
        # 恢复的 failure 同样先尝试上传错误现场；没有可用截图时上传器会
        # 回退到任务日志尾部，成功后才允许 task_result 释放 Server 租约。
        self.assertEqual(payload["phase"], "error_bundle")
        self.assertEqual(payload["result"]["status"], "failure")
        self.assertEqual(payload["config_revision"], 1)

    async def test_agent_restart_replaces_corrupted_current_outbox(self) -> None:
        """重启遇到半写 outbox 时要以失败结果收尾，不能永久挂住 Server 租约。"""
        self.agent.state.current_execution_id = "execution-corrupted"
        self.agent.state.current_task_name = "Restart"
        self.agent.state.current_config_revision = 1
        self.agent.state_store.save(self.agent.state)
        corrupted = self.agent._outbox_path("execution-corrupted")
        corrupted.parent.mkdir(parents=True, exist_ok=True)
        corrupted.write_text("{not-json", encoding="utf-8")

        restarted = AndroidAgent(self.agent.settings)
        restarted._instance_lock_fd = 1
        restarted._recover_interrupted_execution_after_lock()

        payload = json.loads(restarted._outbox_path("execution-corrupted").read_text(encoding="utf-8"))
        self.assertEqual(payload["result"]["status"], "failure")
        self.assertIn("outbox", payload["result"]["summary"])

    async def test_interrupted_execution_recovery_requires_the_instance_lock(self) -> None:
        """第二个误启动 Agent 不得在锁竞争前写入恢复 outbox。"""
        self.agent.state.current_execution_id = "execution-lock-required"
        self.agent.state.current_task_name = "Restart"
        self.agent.state.current_config_revision = 1
        self.agent.state_store.save(self.agent.state)

        restarted = AndroidAgent(self.agent.settings)

        with self.assertRaisesRegex(RuntimeError, "requires the Agent instance lock"):
            restarted._recover_interrupted_execution_after_lock()
        self.assertFalse(restarted._outbox_path("execution-lock-required").exists())

    async def test_invalid_execution_id_cannot_escape_agent_state_paths(self) -> None:
        """Server 命令的 execution_id 不能被解释为 Agent 状态目录之外的文件路径。"""
        with self.assertRaisesRegex(ValueError, "only letters"):
            self.agent._outbox_path("../../unexpected")

    async def test_preflight_accepts_redroid_override_screen_size(self) -> None:
        """redroid 同时报告 Physical/Override size 时任一匹配即可通过。"""
        for directory in ("module", "tasks", "assets"):
            (self.agent.settings.runtime_root / directory).mkdir()

        def shell(command: str) -> str:
            if command == "id":
                return "uid=0(root) gid=0(root)"
            if command == "wm size":
                return "Physical size: 1080x1920\nOverride size: 1280x720"
            if command == "input help":
                return "tap <x> <y>\nswipe <x1> <y1> <x2> <y2>\nmotionevent <DOWN|MOVE|UP>"
            if command.startswith("uiautomator dump --compressed"):
                return "<?xml version='1.0' encoding='UTF-8'?><hierarchy />"
            return "package:/data/app/com.netease.onmyoji/base.apk"

        with patch.object(AndroidAgent, "_shell", side_effect=shell), patch.object(
            AndroidAgent, "_shell_bytes", return_value=b"\x89PNG\r\n\x1a\nfixture"
        ), patch.object(
            AndroidAgent, "_ocr_model_issues", return_value=[]
        ), patch.object(
            AndroidAgent, "_runtime_dependency_issues", return_value=[]
        ):
            health = self.agent._preflight(config_snapshot(1))

        self.assertEqual(health["status"], "healthy")
        self.assertTrue(health["checks"]["screen"])
        self.assertTrue(health["checks"]["root_screenshot"])
        self.assertTrue(health["checks"]["root_input"])
        self.assertTrue(health["checks"]["root_hierarchy"])
        self.assertTrue(health["checks"]["ocr_models"])
        self.assertTrue(health["checks"]["python_dependencies"])

    async def test_preflight_rejects_missing_fixed_ocr_models(self) -> None:
        """版本化镜像缺少 OCR 资源时，Agent 不能依赖运行期下载继续领任务。"""
        for directory in ("module", "tasks", "assets"):
            (self.agent.settings.runtime_root / directory).mkdir()

        def shell(command: str) -> str:
            if command == "id":
                return "uid=0(root) gid=0(root)"
            if command == "wm size":
                return "Physical size: 1280x720"
            if command == "input help":
                return "tap swipe motionevent"
            if command.startswith("uiautomator dump --compressed"):
                return "<?xml version='1.0'?><hierarchy />"
            return "package:/data/app/com.netease.onmyoji/base.apk"

        with patch.object(AndroidAgent, "_shell", side_effect=shell), patch.object(
            AndroidAgent, "_shell_bytes", return_value=b"\x89PNG\r\n\x1a\nfixture"
        ), patch.object(
            AndroidAgent, "_ocr_model_issues", return_value=["missing:det/inference.onnx"]
        ), patch.object(
            AndroidAgent, "_runtime_dependency_issues", return_value=[]
        ):
            health = self.agent._preflight(config_snapshot(1))

        self.assertEqual(health["status"], "unhealthy")
        self.assertFalse(health["checks"]["ocr_models"])
        self.assertIn("OCR 模型资源不完整", health["reason"])

    async def test_preflight_rejects_missing_fixed_python_dependency(self) -> None:
        """固定镜像缺少 OAS 导入依赖时，Agent 必须保持不健康而不试图在线安装。"""
        for directory in ("module", "tasks", "assets"):
            (self.agent.settings.runtime_root / directory).mkdir()

        def shell(command: str) -> str:
            if command == "id":
                return "uid=0(root) gid=0(root)"
            if command == "wm size":
                return "Physical size: 1280x720"
            if command == "input help":
                return "tap swipe motionevent"
            if command.startswith("uiautomator dump --compressed"):
                return "<?xml version='1.0'?><hierarchy />"
            return "package:/data/app/com.netease.onmyoji/base.apk"

        with patch.object(AndroidAgent, "_shell", side_effect=shell), patch.object(
            AndroidAgent, "_shell_bytes", return_value=b"\x89PNG\r\n\x1a\nfixture"
        ), patch.object(
            AndroidAgent, "_ocr_model_issues", return_value=[]
        ), patch.object(
            AndroidAgent, "_runtime_dependency_issues", return_value=["cv2:ImportError"]
        ):
            health = self.agent._preflight(config_snapshot(1))

        self.assertEqual(health["status"], "unhealthy")
        self.assertFalse(health["checks"]["python_dependencies"])
        self.assertIn("Python 运行时依赖不可用", health["reason"])

    async def test_heartbeat_reports_root_shell_foreground_package(self) -> None:
        """心跳包含由本机 Root shell 查询出的前台包名。"""
        sent: list[dict] = []

        class FakeWebSocket:
            """收集 Agent 发出的单条 JSON 消息。"""

            async def send(self, raw: str) -> None:
                """解析并保存发送载荷。"""
                sent.append(json.loads(raw))

        self.agent.websocket = FakeWebSocket()
        with patch.object(AndroidAgent, "_foreground_package", return_value="com.netease.onmyoji"):
            await self.agent._send_heartbeat()

        self.assertEqual(sent[0]["type"], "heartbeat")
        self.assertEqual(sent[0]["foreground_package"], "com.netease.onmyoji")

    async def test_preflight_rejects_root_shell_without_usable_screencap(self) -> None:
        """uid=0 但截图命令不可用时，Agent 不能错误标记为可派发。"""
        for directory in ("module", "tasks", "assets"):
            (self.agent.settings.runtime_root / directory).mkdir()

        def shell(command: str) -> str:
            if command == "id":
                return "uid=0(root) gid=0(root)"
            if command == "wm size":
                return "Physical size: 1280x720"
            if command == "input help":
                return "tap swipe motionevent"
            if command.startswith("uiautomator dump --compressed"):
                return "<?xml version='1.0'?><hierarchy />"
            return "package:/data/app/com.netease.onmyoji/base.apk"

        with patch.object(AndroidAgent, "_shell", side_effect=shell), patch.object(
            AndroidAgent, "_shell_bytes", return_value=b"not-a-png"
        ), patch.object(
            AndroidAgent, "_ocr_model_issues", return_value=[]
        ), patch.object(
            AndroidAgent, "_runtime_dependency_issues", return_value=[]
        ):
            health = self.agent._preflight(config_snapshot(1))

        self.assertEqual(health["status"], "unhealthy")
        self.assertFalse(health["checks"]["root_screenshot"])
        self.assertIn("screencap", health["reason"])

    async def test_heartbeat_ack_activates_newer_server_config_without_reconnect(self) -> None:
        """管理员保存新配置后，在线 Agent 通过心跳确认快照而无需重连。"""
        newer = config_snapshot(2)
        newer["display_name"] = "updated device"

        with patch.object(self.agent, "_report_preflight") as report_preflight:
            await self.agent._handle_message({
                "type": "heartbeat_ack",
                "sent_at": "2026-11-26T00:00:01",
                "sequence": 1,
                "config_revision": 2,
                "config": newer,
            })

        self.assertEqual(self.agent.state.confirmed_revision, 2)
        self.assertEqual(self.agent.config_store.load_confirmed()["display_name"], "updated device")
        report_preflight.assert_awaited_once_with(newer)

    async def test_active_task_defers_new_config_preflight_until_result_ack(self) -> None:
        """运行中任务保留租约快照，确认结果后切换并复检管理员的新配置。"""
        self.agent.state.current_execution_id = "execution-active"
        self.agent.state.current_task_name = "Restart"
        self.agent.state.current_config_revision = 1
        self.agent.config_store.save_confirmed(config_snapshot(1))
        newer = config_snapshot(2)
        newer["runtime"] = {"script": {"optimization": {"screenshot_interval": 0.25}}}

        with patch.object(self.agent, "_report_preflight") as report_preflight:
            await self.agent._handle_message({
                "type": "heartbeat_ack",
                "sent_at": "2026-11-26T00:00:01",
                "sequence": 1,
                "config_revision": 2,
                "config": newer,
            })
            report_preflight.assert_not_awaited()
            await self.agent._handle_message({
                "type": "task_result_ack",
                "sent_at": "2026-11-26T00:00:02",
                "sequence": 2,
                "execution_id": "execution-active",
                "config_revision": 1,
            })

        report_preflight.assert_awaited_once_with(newer)
        runtime = json.loads(self.agent.config_store.runtime_config_path.read_text(encoding="utf-8"))
        self.assertEqual(runtime["script"]["optimization"]["screenshot_interval"], 0.25)

    async def test_rejected_schedule_patch_defers_authoritative_snapshot_until_result_ack(self) -> None:
        """补丁冲突返回的新快照也必须在旧租约完成后成为下一轮运行配置。"""
        self.agent.state.current_execution_id = "execution-schedule-conflict"
        self.agent.state.current_task_name = "Restart"
        self.agent.state.current_config_revision = 1
        self.agent.config_store.save_confirmed(config_snapshot(1))
        newer = config_snapshot(2)
        newer["runtime"] = {"script": {"optimization": {"screenshot_interval": 0.5}}}
        self.agent._write_outbox(
            __import__("oas_android_agent.service", fromlist=["PendingLease"]).PendingLease(
                "execution-schedule-conflict", "Restart", 1, config_snapshot(1)
            ),
            {"status": "success", "summary": "done"},
            {"tasks": {"Restart": {"scheduler": {"next_run": "2026-11-27 01:00:00"}}}},
        )

        self.agent._confirm_schedule_patch({
            "execution_id": "execution-schedule-conflict",
            "accepted": False,
            "reason": "revision conflict",
            "config_revision": 2,
            "config": newer,
        })

        old_runtime = json.loads(self.agent.config_store.runtime_config_path.read_text(encoding="utf-8"))
        old_interval = old_runtime.get("script", {}).get("optimization", {}).get("screenshot_interval")
        self.assertNotEqual(old_interval, 0.5)
        self.assertEqual(self.agent._read_outbox("execution-schedule-conflict")["phase"], "result")
        self.assertEqual(self.agent._pending_preflight_config, newer)

        self.agent._confirm_result({"execution_id": "execution-schedule-conflict", "config_revision": 1})

        runtime = json.loads(self.agent.config_store.runtime_config_path.read_text(encoding="utf-8"))
        self.assertEqual(runtime["script"]["optimization"]["screenshot_interval"], 0.5)

    async def test_outbox_phase_is_sent_once_until_server_acknowledges(self) -> None:
        """心跳与任务完成同时触发时，同一 outbox 阶段不会重复发送。"""
        sent: list[dict] = []

        class FakeWebSocket:
            """收集 Agent 上行消息。"""

            async def send(self, raw: str) -> None:
                """解析 Agent 上行消息。"""
                sent.append(json.loads(raw))

        self.agent.websocket = FakeWebSocket()
        service_module = __import__("oas_android_agent.service", fromlist=["PendingLease"])
        lease = service_module.PendingLease("execution-outbox", "Restart", 1, config_snapshot(1))
        self.agent._write_outbox(lease, {"status": "success", "summary": "done"}, None)
        await self.agent._flush_outbox()
        await self.agent._flush_outbox()
        self.assertEqual([message["type"] for message in sent], ["task_result"])

        self.agent._confirm_result({"execution_id": "execution-outbox", "config_revision": 1})
        self.assertFalse(self.agent._outbox_path("execution-outbox").exists())

    async def test_outbox_phase_retries_after_websocket_reconnect(self) -> None:
        """旧 WSS 在 ACK 前断开时，重连必须重新发送未确认阶段。"""
        sent: list[dict] = []

        class FakeWebSocket:
            """记录新连接上的 Agent 上行消息。"""

            async def send(self, raw: str) -> None:
                sent.append(json.loads(raw))

        self.agent.websocket = FakeWebSocket()
        service_module = __import__("oas_android_agent.service", fromlist=["PendingLease"])
        lease = service_module.PendingLease("execution-reconnect", "Restart", 1, config_snapshot(1))
        self.agent._write_outbox(lease, {"status": "success", "summary": "done"}, None)
        await self.agent._flush_outbox()
        self.assertEqual(self.agent._outbox_inflight, ("execution-reconnect", "result"))

        # 模拟 run_forever 建立新 WSS 时清理旧会话的 ACK 状态。
        self.agent._set_connection(FakeWebSocket())
        await self.agent._flush_outbox()
        self.assertEqual([message["type"] for message in sent], ["task_result", "task_result"])

    async def test_reconnect_reasserts_confirmed_error_bundle_before_failure_result(self) -> None:
        """Server 重启丢失错误包确认时，Agent 必须先重申 error_id 再重传失败结果。"""
        sent: list[dict] = []

        class FakeWebSocket:
            """收集 Agent 在新连接上的上行消息。"""

            async def send(self, raw: str) -> None:
                """解析并保存协议帧。"""
                sent.append(json.loads(raw))

        service_module = __import__("oas_android_agent.service", fromlist=["PendingLease"])
        execution_id = "execution-error-server-restart"
        lease = service_module.PendingLease(execution_id, "Restart", 1, config_snapshot(1))
        self.agent.state.current_execution_id = execution_id
        self.agent.state.current_task_name = "Restart"
        self.agent.state.current_config_revision = 1
        self.agent._write_outbox(
            lease,
            {"status": "failure", "summary": "failed", "error_id": "redroid-01_789"},
            None,
        )
        payload = self.agent._read_outbox(execution_id)
        payload["phase"] = "result"
        self.agent._atomic_write_json(self.agent._outbox_path(execution_id), payload)

        # 模拟 error_bundle ACK 已在旧 Server 会话收到，随后 Server 重启并丢失
        # 内存态；重连不能直接发送 task_result。
        self.agent._set_connection(FakeWebSocket())
        await self.agent._flush_outbox()
        self.assertEqual([message["type"] for message in sent], ["error_bundle"])
        self.assertEqual(sent[0]["error_id"], "redroid-01_789")
        self.assertEqual(self.agent._outbox_inflight, (execution_id, "error_bundle_reassert"))

        await self.agent._flush_outbox()
        self.assertEqual([message["type"] for message in sent], ["error_bundle"])

        self.agent._confirm_error_bundle_notice({
            "execution_id": execution_id,
            "config_revision": 1,
            "error_id": "redroid-01_789",
        })
        self.assertEqual(self.agent._read_outbox(execution_id)["phase"], "result")
        await self.agent._flush_outbox()
        self.assertEqual([message["type"] for message in sent], ["error_bundle", "task_result"])

    async def test_invalid_task_result_ack_does_not_clear_outbox(self) -> None:
        """结果 ACK 的租约版本不匹配时必须继续等待正确 ACK。"""
        service_module = __import__("oas_android_agent.service", fromlist=["PendingLease"])
        lease = service_module.PendingLease("execution-result-ack", "Restart", 1, config_snapshot(1))
        self.agent._write_outbox(lease, {"status": "success", "summary": "done"}, None)
        self.agent.state.current_execution_id = "execution-result-ack"
        self.agent.state.current_config_revision = 1

        self.agent._confirm_result({"execution_id": "execution-result-ack", "config_revision": 2})

        self.assertTrue(self.agent._outbox_path("execution-result-ack").exists())
        self.assertEqual(self.agent.state.current_execution_id, "execution-result-ack")

    async def test_invalid_result_ack_does_not_activate_deferred_config(self) -> None:
        """无效结果 ACK 不能提前结束旧租约的配置固定窗口。"""
        self.agent.state.current_execution_id = "execution-result-deferred"
        self.agent.state.current_task_name = "Restart"
        self.agent.state.current_config_revision = 1
        self.agent.config_store.save_confirmed(config_snapshot(1))
        newer = config_snapshot(2)
        newer["runtime"] = {"script": {"optimization": {"screenshot_interval": 0.75}}}
        self.agent._pending_preflight_config = newer
        service_module = __import__("oas_android_agent.service", fromlist=["PendingLease"])
        self.agent._write_outbox(
            service_module.PendingLease("execution-result-deferred", "Restart", 1, config_snapshot(1)),
            {"status": "success", "summary": "done"},
            None,
        )

        with patch.object(self.agent, "_report_preflight") as report_preflight:
            await self.agent._handle_message({
                "type": "task_result_ack",
                "sent_at": "2026-11-26T00:00:01",
                "sequence": 1,
                "execution_id": "execution-result-deferred",
                "config_revision": 2,
            })

        report_preflight.assert_not_awaited()
        self.assertEqual(self.agent._pending_preflight_config, newer)
        self.assertEqual(self.agent.state.current_execution_id, "execution-result-deferred")

    async def test_agent_send_envelope_cannot_be_overridden_by_payload_fields(self) -> None:
        """业务 payload 不能覆盖 Agent 的身份、序列和共享密钥信封。"""
        sent: list[dict] = []

        class FakeWebSocket:
            """记录序列化后的单帧消息。"""

            async def send(self, raw: str) -> None:
                sent.append(json.loads(raw))

        self.agent.websocket = FakeWebSocket()
        self.agent.sequence = 10
        await self.agent._send({
            "type": "heartbeat",
            "device_id": "forged-device",
            "sequence": 999,
            "sent_at": "forged-time",
            "secret": "forged-secret",
        })

        self.assertEqual(sent[0]["device_id"], self.agent.settings.device_id)
        self.assertEqual(sent[0]["sequence"], 11)
        self.assertNotEqual(sent[0]["sent_at"], "forged-time")
        self.assertEqual(sent[0]["secret"], self.agent.settings.shared_secret)

    async def test_failed_task_retries_error_bundle_before_its_result(self) -> None:
        """断线后的失败现场必须先重传；task_result 不能先释放 Server 租约。"""
        sent: list[dict] = []

        class FakeWebSocket:
            """收集 Agent 按阶段发出的协议消息。"""

            async def send(self, raw: str) -> None:
                """解析并保存上行 JSON。"""
                sent.append(json.loads(raw))

        self.agent.websocket = FakeWebSocket()
        service_module = __import__("oas_android_agent.service", fromlist=["PendingLease"])
        lease = service_module.PendingLease("execution-error-retry", "Restart", 1, config_snapshot(1))
        self.agent._write_outbox(lease, {"status": "failure", "summary": "failed"}, None)

        with patch.object(self.agent.error_uploader, "upload", return_value="redroid-01_123") as upload:
            await self.agent._flush_outbox()
            await self.agent._flush_outbox()

        self.assertEqual(upload.call_count, 1)
        self.assertEqual([message["type"] for message in sent], ["error_bundle"])
        payload = self.agent._read_outbox("execution-error-retry")
        self.assertEqual(payload["phase"], "error_bundle_notice")
        self.assertEqual(payload["result"]["error_id"], "redroid-01_123")

        self.agent._confirm_error_bundle_notice({
            "execution_id": "execution-error-retry",
            "config_revision": 1,
            "error_id": "redroid-01_123",
        })
        await self.agent._flush_outbox()
        self.assertEqual([message["type"] for message in sent], ["error_bundle", "task_result"])

    async def test_invalid_error_bundle_ack_does_not_advance_outbox(self) -> None:
        """错误包 ACK 的执行版本或目录编号不匹配时必须继续等待。"""
        sent: list[dict] = []

        class FakeWebSocket:
            """收集 Agent 持续等待 ACK 期间发出的消息。"""

            async def send(self, raw: str) -> None:
                """解析并保存上行 JSON。"""
                sent.append(json.loads(raw))

        self.agent.websocket = FakeWebSocket()
        service_module = __import__("oas_android_agent.service", fromlist=["PendingLease"])
        lease = service_module.PendingLease("execution-error-ack", "Restart", 1, config_snapshot(1))
        self.agent._write_outbox(lease, {"status": "failure", "summary": "failed"}, None)
        with patch.object(self.agent.error_uploader, "upload", return_value="redroid-01_456"):
            await self.agent._flush_outbox()

        self.agent._confirm_error_bundle_notice({
            "execution_id": "execution-error-ack",
            "config_revision": 999,
            "error_id": "redroid-01_456",
        })
        self.assertEqual(self.agent._read_outbox("execution-error-ack")["phase"], "error_bundle_notice")
        await self.agent._flush_outbox()
        self.assertEqual([message["type"] for message in sent], ["error_bundle"])

    async def test_failed_error_bundle_network_failure_keeps_outbox_before_result(self) -> None:
        """错误包 HTTP 失败时必须保留首阶段，不能越过现场直接提交结果。"""
        sent: list[dict] = []

        class FakeWebSocket:
            """记录任何意外的结果或控制面上行消息。"""

            async def send(self, raw: str) -> None:
                """解析并保存上行 JSON。"""
                sent.append(json.loads(raw))

        self.agent.websocket = FakeWebSocket()
        service_module = __import__("oas_android_agent.service", fromlist=["PendingLease"])
        lease = service_module.PendingLease("execution-error-network", "Restart", 1, config_snapshot(1))
        self.agent._write_outbox(lease, {"status": "manual_required", "summary": "failed"}, None)

        with patch.object(self.agent.error_uploader, "upload", side_effect=OSError("network unavailable")):
            await self.agent._flush_outbox()

        self.assertEqual(sent, [])
        self.assertEqual(self.agent._read_outbox("execution-error-network")["phase"], "error_bundle")

    async def test_task_process_exports_server_task_allowlist(self) -> None:
        """子进程必须把关联 task_call 限制为当前租约的 Server 快照任务。"""
        config = config_snapshot(1)
        config["tasks"]["Exploration"] = {
            "scheduler": {"enable": False, "next_run": "2026-11-26 00:00:00", "priority": 5}
        }
        service_module = __import__("oas_android_agent.service", fromlist=["PendingLease"])
        lease = service_module.PendingLease("execution-allowlist", "Restart", 1, config)
        result_path = self.agent.settings.state_root / "results" / "execution-allowlist.json"
        captured: dict[str, str] = {}

        class FakeProcess:
            """模拟完成的 OAS 子进程。"""

            async def wait(self) -> None:
                result_path.parent.mkdir(parents=True, exist_ok=True)
                result_path.write_text('{"status":"success","summary":"done"}', encoding="utf-8")

        async def create_process(*command: str, **kwargs: object) -> FakeProcess:
            captured["allowlist"] = str(kwargs["env"]["OAS_AGENT_SCHEDULED_TASKS"])
            captured["local_rpc_only"] = str(kwargs["env"]["OAS_AGENT_LOCAL_RPC_ONLY"])
            captured["execution_lease"] = str(kwargs["env"]["OAS_AGENT_EXECUTION_LEASE"])
            return FakeProcess()

        with patch("asyncio.create_subprocess_exec", side_effect=create_process), patch.object(
            self.agent, "_flush_outbox"
        ):
            await self.agent._run_task_process(lease)

        self.assertEqual(json.loads(captured["allowlist"]), ["exploration", "restart"])
        self.assertEqual(captured["local_rpc_only"], "true")
        self.assertEqual(captured["execution_lease"], "true")

    async def test_start_task_reports_persisted_execution_before_running_process(self) -> None:
        """Agent 在创建本地任务后应回传 task_started，供 Server 展示已启动状态。"""
        sent: list[dict] = []

        class FakeWebSocket:
            """收集 Agent 对启动命令的协议回包。"""

            async def send(self, raw: str) -> None:
                """解析并记录 Agent 上行 JSON。"""
                sent.append(json.loads(raw))

        self.agent.websocket = FakeWebSocket()
        self.agent.health = {"status": "healthy"}
        self.agent.pending_lease = __import__("oas_android_agent.service", fromlist=["PendingLease"]).PendingLease(
            "execution-started", "Restart", 1, config_snapshot(1)
        )
        def capture_task(coroutine):
            """关闭测试替身未执行的协程，避免留下未 await 的运行时告警。"""
            coroutine.close()
            return object()

        with patch("asyncio.create_task", side_effect=capture_task) as create_task:
            await self.agent._start_task({
                "execution_id": "execution-started",
                "task_name": "Restart",
                "config_revision": 1,
            })

        self.assertEqual(create_task.call_count, 1)
        self.assertEqual(sent[0]["type"], "task_started")
        self.assertEqual(sent[0]["execution_id"], "execution-started")
        self.assertEqual(self.agent.state.current_execution_id, "execution-started")

    async def test_start_task_rejects_name_mismatch_with_accepted_lease(self) -> None:
        """同一执行编号也必须使用已经接收的任务名，不能替换租约内容。"""
        sent: list[dict] = []

        class FakeWebSocket:
            """收集 Agent 对错配启动命令的拒绝结果。"""

            async def send(self, raw: str) -> None:
                """保存 Agent 发出的 JSON 消息。"""
                sent.append(json.loads(raw))

        self.agent.websocket = FakeWebSocket()
        self.agent.health = {"status": "healthy"}
        await self.agent._receive_lease({
            "execution_id": "execution-name-mismatch",
            "task_name": "Restart",
            "config_revision": 1,
            "expires_at": "2099-01-01T00:00:00+00:00",
            "config": config_snapshot(1),
        })
        await self.agent._start_task({
            "execution_id": "execution-name-mismatch",
            "task_name": "Exploration",
            "config_revision": 1,
        })

        self.assertEqual(sent[-1]["type"], "task_result")
        self.assertEqual(sent[-1]["status"], "rejected")
        self.assertEqual(self.agent.pending_lease.task_name, "Restart")

    async def test_expired_pending_lease_cannot_start_task(self) -> None:
        """租约在两帧之间过期时，Agent 不应创建本地任务进程。"""
        sent: list[dict] = []

        class FakeWebSocket:
            """收集启动拒绝结果。"""

            async def send(self, raw: str) -> None:
                """解析 Agent 上行消息。"""
                sent.append(json.loads(raw))

        self.agent.websocket = FakeWebSocket()
        self.agent.health = {"status": "healthy"}
        service_module = __import__("oas_android_agent.service", fromlist=["PendingLease"])
        self.agent.pending_lease = service_module.PendingLease(
            "execution-expired-before-start",
            "Restart",
            1,
            config_snapshot(1),
            datetime.now(timezone.utc).replace(microsecond=0),
        )

        with patch("asyncio.create_task") as create_task:
            await self.agent._start_task({
                "execution_id": "execution-expired-before-start",
                "task_name": "Restart",
                "config_revision": 1,
            })

        create_task.assert_not_called()
        self.assertEqual(sent[0]["status"], "rejected")
        self.assertIn("expired before", sent[0]["summary"])
        self.assertIsNone(self.agent.pending_lease)

    async def test_unhealthy_start_rejection_discards_pending_lease(self) -> None:
        """预检失败拒绝启动后，旧 start_task 重放不能恢复已释放租约。"""
        sent: list[dict] = []

        class FakeWebSocket:
            """收集 Agent 的拒绝结果。"""

            async def send(self, raw: str) -> None:
                """解析上行消息。"""
                sent.append(json.loads(raw))

        self.agent.websocket = FakeWebSocket()
        self.agent.health = {"status": "unhealthy", "reason": "Root unavailable"}
        service_module = __import__("oas_android_agent.service", fromlist=["PendingLease"])
        self.agent.pending_lease = service_module.PendingLease(
            "execution-unhealthy-start",
            "Restart",
            1,
            config_snapshot(1),
            datetime.now(timezone.utc) + timedelta(days=1),
        )

        await self.agent._start_task({
            "execution_id": "execution-unhealthy-start",
            "task_name": "Restart",
            "config_revision": 1,
        })

        self.assertEqual(sent[0]["status"], "rejected")
        self.assertIsNone(self.agent.pending_lease)

    async def test_stale_stop_command_cannot_cancel_current_lease(self) -> None:
        """停止命令必须同时匹配 execution_id 和租约版本，防止陈旧命令生效。"""
        self.agent.state.current_execution_id = "execution-stop-version"
        self.agent.state.current_config_revision = 3

        self.agent._request_stop({
            "execution_id": "execution-stop-version",
            "config_revision": 2,
        })

        self.assertFalse(self.agent._stop_file("execution-stop-version").exists())

    async def test_unhealthy_preflight_rejects_recovered_unstarted_lease(self) -> None:
        """重连预检失败时主动释放尚未启动的租约，恢复健康后才能重新调度。"""
        sent: list[dict] = []

        class FakeWebSocket:
            """记录 Agent 为旧租约回送的拒绝结果。"""

            async def send(self, raw: str) -> None:
                """保存 JSON 上行消息。"""
                sent.append(json.loads(raw))

        self.agent.websocket = FakeWebSocket()
        self.agent.health = {"status": "unhealthy", "reason": "Root unavailable"}
        self.agent._resume_commands = [
            {"type": "task_lease", "execution_id": "execution-recovered", "task_name": "Restart", "config_revision": 1,
             "expires_at": "2099-01-01T00:00:00+00:00"},
            {"type": "start_task", "execution_id": "execution-recovered", "task_name": "Restart", "config_revision": 1},
        ]

        await self.agent._handle_resume_commands_after_preflight()

        self.assertEqual([message["type"] for message in sent], ["task_result"])
        self.assertEqual(sent[0]["status"], "rejected")
        self.assertEqual(sent[0]["execution_id"], "execution-recovered")

    async def test_healthy_preflight_runs_embedded_resume_commands_without_sequences(self) -> None:
        """hello_ack 的内嵌恢复命令继承外层序列号，健康 Agent 可恢复收尾。"""
        sent: list[dict] = []

        class FakeWebSocket:
            """收集恢复路径的启动确认。"""

            async def send(self, raw: str) -> None:
                """保存 Agent 回送的协议消息。"""
                sent.append(json.loads(raw))

        def capture_task(coroutine):
            """关闭测试中不实际运行的任务协程，避免未 await 告警。"""
            coroutine.close()
            return object()

        self.agent.websocket = FakeWebSocket()
        self.agent.health = {"status": "healthy"}
        self.agent._resume_commands = [
            {"type": "task_lease", "execution_id": "execution-resume", "task_name": "Restart",
             "config_revision": 1, "expires_at": "2099-01-01T00:00:00+00:00", "config": config_snapshot(1)},
            {"type": "start_task", "execution_id": "execution-resume", "task_name": "Restart",
             "config_revision": 1},
        ]
        with patch("asyncio.create_task", side_effect=capture_task):
            await self.agent._handle_resume_commands_after_preflight()

        self.assertEqual(self.agent.state.current_execution_id, "execution-resume")
        self.assertEqual([message["type"] for message in sent], ["task_started"])

    async def test_manual_required_result_is_not_downgraded_to_failure(self) -> None:
        """OAS 的人工接管退出必须原样上报，避免服务器立即重派同一任务。"""
        service_module = __import__("oas_android_agent.service", fromlist=["PendingLease"])
        lease = service_module.PendingLease("execution-manual", "Restart", 1, config_snapshot(1))
        result_path = self.agent.settings.state_root / "results" / "execution-manual.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            '{"status":"manual_required","summary":"OAS requires manual handling"}',
            encoding="utf-8",
        )
        result = self.agent._load_result(result_path, lease)
        self.assertEqual(result["status"], "manual_required")

    async def test_missing_subprocess_result_disables_current_task(self) -> None:
        """子进程异常退出没有结果文件时暂停任务，避免 Server 无限重派。"""
        snapshot = config_snapshot(1)
        self.agent.config_store.save_confirmed(snapshot)
        service_module = __import__("oas_android_agent.service", fromlist=["PendingLease"])
        lease = service_module.PendingLease("execution-missing-result", "Restart", 1, snapshot)
        result = self.agent._load_result(self.agent.settings.state_root / "results" / "missing.json", lease)

        self.assertEqual(result["status"], "manual_required")
        self.assertIn("task_disabled", result["summary"])
        patch = self.agent.config_store.schedule_patch_from_runtime(snapshot)
        self.assertEqual(patch, {"tasks": {"Restart": {"scheduler": {"enable": False}}}})

    async def test_souls_tidy_is_available_for_battle_overflow_schedule_patch(self) -> None:
        """基础副本因御魂溢出唤醒 SoulsTidy 时，清单必须允许其后续租约。"""
        config = config_snapshot(1)
        config["tasks"]["SoulsTidy"] = {
            "scheduler": {"enable": False, "next_run": "2026-11-26 00:00:00", "priority": 5}
        }
        self.assertIsNone(self.agent._task_manifest_rejection("SoulsTidy", config))


if __name__ == "__main__":
    unittest.main()
