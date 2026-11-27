"""Android Agent 控制面回归测试，可用 ``toolkit/python.exe -m unittest`` 运行。"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
import json
from datetime import datetime, timedelta
from pathlib import Path

from oas_control_plane.agent_control_plane import AgentControlPlane
from oas_control_plane.agent_device_repository import (
    DeviceConfigConflictError,
    DeviceConfigError,
    DeviceConfigRepository,
)


def device_config() -> dict:
    """构造一份最小、有效的 RootShell 设备配置。"""
    return {
        "device_id": "redroid-01",
        "display_name": "测试设备",
        "revision": 0,
        "required_agent_version": "2026.11.26",
        "device": {
            "transport": "root_shell",
            "game_package": "com.netease.onmyoji",
            "screen_size": [1280, 720],
        },
        "runtime": {},
        "tasks": {
            "Restart": {"scheduler": {"enable": True, "next_run": "2020-01-01 00:00:00", "priority": 0}},
            "DailyTrifles": {"scheduler": {"enable": True, "next_run": "2020-01-01 00:00:00", "priority": 3}},
            "Exploration": {"scheduler": {"enable": True, "next_run": "2020-01-01 00:00:00", "priority": 5}},
            "SoulsTidy": {"scheduler": {"enable": False, "next_run": "2020-01-01 00:00:00", "priority": 5}},
            "GotoMain": {"scheduler": {"enable": False, "next_run": "2020-01-01 00:00:00", "priority": 0}},
        },
    }


def healthy_health() -> dict:
    """构造 RootShell Agent 完成预检后的健康报告。"""
    return {
        "status": "healthy",
        "checks": {
            "runtime": True,
            "state_root": True,
            "root": True,
            "screen": True,
            "game_package": True,
        },
    }


class FakeWebSocket:
    """收集 Server 下发消息，不需要真实网络或 FastAPI 测试客户端。"""

    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send_json(self, message: dict) -> None:
        """记录命令顺序，供单租约断言使用。"""
        self.messages.append(message)


class AgentControlPlaneTest(unittest.IsolatedAsyncioTestCase):
    """验证文件配置、租约幂等和 Agent 重连协调。"""

    async def asyncSetUp(self) -> None:
        """每个测试使用独立的设备 JSON 临时目录。"""
        self.tempdir = tempfile.TemporaryDirectory()
        self.repository = DeviceConfigRepository(Path(self.tempdir.name))
        self.repository.replace("redroid-01", device_config(), expected_revision=None)
        self.plane = AgentControlPlane(self.repository)

    async def asyncTearDown(self) -> None:
        """清理测试创建的 JSON 文件。"""
        self.tempdir.cleanup()

    async def _register(self, version: str = "2026.11.26", task_names: tuple[str, ...] = ("Restart", "DailyTrifles", "Exploration", "SoulsTidy")) -> FakeWebSocket:
        """模拟已通过设备预检的 Agent hello。"""
        websocket = FakeWebSocket()
        await self.plane.register_hello(websocket, {
            "protocol_version": 1,
            "device_id": "redroid-01",
            "sent_at": "2026-11-26T00:00:00",
            "sequence": 1,
            "agent_version": version,
            "resource_version": version,
            "capabilities": ["root_shell"],
            "task_manifest": [
                {"name": name, "minimum_resource_version": "2026.11.26"}
                for name in task_names
            ],
            # 正常 Agent 会在 hello 后单独上报预检；测试也显式补发该回报，
            # 使租约测试聚焦于各自的调度语义。
            "health": healthy_health(),
        })
        # hello 的健康字段不作为本次会话的可信预检结果；模拟 Agent
        # 紧接着发送独立 health_report。
        await self.plane.report_health("redroid-01", {
            "config_revision": 1,
            "health": healthy_health(),
        })
        return websocket

    async def test_hello_health_is_not_trusted_before_health_report(self) -> None:
        """重连携带旧 healthy 状态时，Server 仍须等待本次预检回报。"""
        websocket = FakeWebSocket()
        await self.plane.register_hello(websocket, {
            "protocol_version": 1,
            "device_id": "redroid-01",
            "sent_at": "2026-11-26T00:00:00",
            "sequence": 1,
            "agent_version": "2026.11.26",
            "resource_version": "2026.11.26",
            "capabilities": ["root_shell"],
            "task_manifest": [{"name": "Restart", "minimum_resource_version": "2026.11.26"}],
            "health": healthy_health(),
        })
        with self.assertRaisesRegex(ValueError, "health_report"):
            await self.plane.lease_task("redroid-01", "Restart", "test")
        await self.plane.report_health("redroid-01", {
            "config_revision": 1,
            "health": healthy_health(),
        })
        await self.plane.lease_task("redroid-01", "Restart", "test")
        self.assertEqual(websocket.messages[-1]["type"], "start_task")

    async def test_task_lease_uses_utc_expiry_for_cross_timezone_agents(self) -> None:
        """租约命令必须携带 UTC 偏移，redroid 不依赖 Server 本地时区。"""
        websocket = await self._register(task_names=("Restart",))

        await self.plane.lease_task("redroid-01", "Restart", "test")

        lease = websocket.messages[-2]
        self.assertEqual(lease["type"], "task_lease")
        self.assertTrue(lease["expires_at"].endswith("+00:00"))

    async def test_server_send_envelope_overrides_caller_identity_and_sequence(self) -> None:
        """下行发送层必须固定设备身份和单调序列，不能被命令 payload 覆盖。"""
        websocket = await self._register(task_names=("Restart",))

        await self.plane.send_to_device("redroid-01", {
            "type": "test_command",
            "device_id": "redroid-02",
            "sent_at": "caller-controlled",
            "sequence": 999,
        })

        message = websocket.messages[-1]
        self.assertEqual(message["device_id"], "redroid-01")
        self.assertNotEqual(message["sent_at"], "caller-controlled")
        self.assertEqual(message["sequence"], 1)

    async def test_unhealthy_agent_cannot_receive_lease_until_health_report(self) -> None:
        """预检未完成的设备不能领任务，成功上报后才允许派发。"""
        websocket = FakeWebSocket()
        await self.plane.register_hello(websocket, {
            "protocol_version": 1,
            "device_id": "redroid-01",
            "sent_at": "2026-11-26T00:00:00",
            "sequence": 1,
            "agent_version": "2026.11.26",
            "resource_version": "2026.11.26",
            "capabilities": ["root_shell"],
            "task_manifest": [{"name": "Restart", "minimum_resource_version": "2026.11.26"}],
        })
        with self.assertRaisesRegex(ValueError, "health_report"):
            await self.plane.lease_task("redroid-01", "Restart", "test")

        response = await self.plane.report_health("redroid-01", {
            "config_revision": 1,
            "health": healthy_health(),
        })
        self.assertEqual(response["health"]["status"], "healthy")
        snapshot = await self.plane.snapshot("redroid-01")
        self.assertEqual(snapshot["health"]["checks"]["root"], True)
        # 当前 revision 的健康报告同时确认 Agent 已成功保存该配置快照，管理
        # 页面不必等待下一次 heartbeat 才显示已确认版本。
        self.assertEqual(snapshot["confirmed_config_revision"], 1)
        await self.plane.lease_task("redroid-01", "Restart", "test")

    async def test_health_report_requires_current_config_revision(self) -> None:
        """不带 revision 或携带非正 revision 的预检不能让设备变为健康。"""
        websocket = FakeWebSocket()
        await self.plane.register_hello(websocket, {
            "protocol_version": 1,
            "device_id": "redroid-01",
            "sent_at": "2026-11-26T00:00:00",
            "sequence": 1,
            "agent_version": "2026.11.26",
            "resource_version": "2026.11.26",
            "capabilities": ["root_shell"],
            "task_manifest": [{"name": "Restart", "minimum_resource_version": "2026.11.26"}],
        })

        with self.assertRaisesRegex(ValueError, "正整数"):
            await self.plane.report_health("redroid-01", {"health": healthy_health()})
        with self.assertRaisesRegex(ValueError, "正整数"):
            await self.plane.report_health("redroid-01", {
                "config_revision": 0,
                "health": healthy_health(),
            })

    async def test_heartbeat_records_foreground_package(self) -> None:
        """心跳中的前台包名进入管理快照，便于定位游戏是否在前台。"""
        await self._register()
        await self.plane.heartbeat("redroid-01", {
            "sequence": 2,
            "sent_at": "2026-11-26T00:00:01",
            "foreground_package": "com.netease.onmyoji",
            "health": healthy_health(),
        })
        snapshot = await self.plane.snapshot("redroid-01")
        self.assertEqual(snapshot["foreground_package"], "com.netease.onmyoji")

    async def test_heartbeat_returns_newer_config_snapshot_without_reconnect(self) -> None:
        """在线 Agent 可经 heartbeat ACK 同步管理员的新设备配置。"""
        await self._register()
        unchanged = await self.plane.heartbeat("redroid-01", {
            "cached_config_revision": 1,
        })
        self.assertEqual(unchanged["config_revision"], 1)
        self.assertNotIn("config", unchanged)

        updated = self.repository.read("redroid-01")
        updated["display_name"] = "已更新的测试设备"
        self.repository.replace("redroid-01", updated, expected_revision=1)
        response = await self.plane.heartbeat("redroid-01", {
            "cached_config_revision": 1,
        })

        snapshot = await self.plane.snapshot("redroid-01")
        self.assertEqual(response["config_revision"], 2)
        self.assertEqual(response["config"]["display_name"], "已更新的测试设备")
        self.assertEqual(snapshot["confirmed_config_revision"], 1)

    async def test_config_edit_marks_agent_unhealthy_until_current_revision_preflight(self) -> None:
        """管理员改设备参数后，旧 revision 的健康报告不能继续领任务。"""
        await self._register()
        updated = self.repository.read("redroid-01")
        updated["display_name"] = "需要重新预检"
        await self.plane.replace_config("redroid-01", updated, expected_revision=1)

        with self.assertRaisesRegex(ValueError, "预检"):
            await self.plane.lease_task("redroid-01", "Restart", "test")
        stale = await self.plane.report_health("redroid-01", {
            "config_revision": 1,
            "health": healthy_health(),
        })
        self.assertEqual(stale["health"]["status"], "initializing")
        await self.plane.report_health("redroid-01", {
            "config_revision": 2,
            "health": healthy_health(),
        })
        await self.plane.lease_task("redroid-01", "Restart", "test")

    async def test_stale_heartbeat_cannot_restore_health_after_config_edit(self) -> None:
        """旧配置 heartbeat 不能绕过管理员修改后的版本化预检。"""
        await self._register()
        updated = self.repository.read("redroid-01")
        updated["display_name"] = "等待新预检"
        await self.plane.replace_config("redroid-01", updated, expected_revision=1)

        await self.plane.heartbeat("redroid-01", {
            "cached_config_revision": 1,
            "config_revision": 1,
            "health": healthy_health(),
        })

        snapshot = await self.plane.snapshot("redroid-01")
        self.assertEqual(snapshot["health"]["status"], "initializing")
        with self.assertRaisesRegex(ValueError, "预检"):
            await self.plane.lease_task("redroid-01", "Restart", "test")

    async def test_stale_heartbeat_marks_session_offline_and_keeps_lease(self) -> None:
        """卡死 Agent 的陈旧心跳不能继续派发，但既有租约必须等待原编号收尾。"""
        self.plane = AgentControlPlane(self.repository, heartbeat_timeout_seconds=1)
        await self._register()
        execution_id = await self.plane.lease_task("redroid-01", "Restart", "test")
        runtime = self.plane._devices["redroid-01"]
        runtime.last_heartbeat = datetime.now() - timedelta(seconds=2)

        await self.plane.expire_leases()

        snapshot = await self.plane.snapshot("redroid-01")
        self.assertFalse(snapshot["online"])
        self.assertEqual(snapshot["lease"]["execution_id"], execution_id)
        self.assertIsNone(runtime.websocket)
        with self.assertRaisesRegex(ValueError, "不在线"):
            await self.plane.lease_task("redroid-01", "Exploration", "test")

    async def test_schedule_patch_then_duplicate_result_is_idempotent(self) -> None:
        """补丁先更新文件版本，结果以原租约版本回传且可重复确认。"""
        websocket = await self._register()
        execution_id = await self.plane.lease_task("redroid-01", "Restart", "test")
        self.assertEqual([item["type"] for item in websocket.messages], ["task_lease", "start_task"])
        self.assertEqual([item["sequence"] for item in websocket.messages], [1, 2])
        self.assertEqual([item["device_id"] for item in websocket.messages], ["redroid-01", "redroid-01"])
        self.assertTrue(all(item["sent_at"] for item in websocket.messages))

        saved = await self.plane.apply_schedule_patch("redroid-01", {
            "execution_id": execution_id,
            "config_revision": 1,
            "patch": {"tasks": {"Restart": {"scheduler": {"next_run": "2099-01-01 00:00:00"}}}},
        })
        self.assertEqual(saved["config_revision"], 2)
        result = {"execution_id": execution_id, "config_revision": 1, "status": "success", "summary": "ok"}
        self.assertFalse((await self.plane.report_result("redroid-01", result))["duplicate"])
        self.assertTrue((await self.plane.report_result("redroid-01", result))["duplicate"])

    async def test_schedule_patch_revalidates_full_device_constraints(self) -> None:
        """调度补丁不能绕过 GotoMain 只能手动执行的配置约束。"""
        await self._register(task_names=("Restart",))
        execution_id = await self.plane.lease_task("redroid-01", "Restart", "test")

        with self.assertRaisesRegex(ValueError, "只能手动执行"):
            await self.plane.apply_schedule_patch("redroid-01", {
                "execution_id": execution_id,
                "config_revision": 1,
                "patch": {
                    "tasks": {
                        "GotoMain": {"scheduler": {"enable": True}},
                    },
                },
            })

        saved = self.repository.read("redroid-01")
        self.assertEqual(saved["revision"], 1)
        self.assertFalse(saved["tasks"]["GotoMain"]["scheduler"]["enable"])

    async def test_scheduler_dispatches_due_task_for_healthy_idle_agent(self) -> None:
        """模拟在线 Agent 验证 Server 会按设备 JSON 自动下发到期任务。"""
        websocket = await self._register()

        await self.plane.dispatch_due_tasks()

        self.assertEqual([message["type"] for message in websocket.messages], ["task_lease", "start_task"])
        snapshot = await self.plane.snapshot("redroid-01")
        self.assertEqual(snapshot["lease"]["requested_by"], "scheduler")

    async def test_scheduler_skips_unsupported_due_task_and_runs_next_candidate(self) -> None:
        """镜像升级中的不支持任务不能阻塞同机其它已验证到期任务。"""
        websocket = await self._register(task_names=("Exploration",))

        await self.plane.dispatch_due_tasks()

        self.assertEqual([message["type"] for message in websocket.messages], ["task_lease", "start_task"])
        self.assertEqual(websocket.messages[0]["task_name"], "Exploration")
        snapshot = await self.plane.snapshot("redroid-01")
        self.assertEqual(snapshot["lease"]["task_name"], "Exploration")

    async def test_stop_command_then_cancelled_result_releases_lease(self) -> None:
        """管理员停止请求会指定当前租约，Agent 取消回报后设备恢复空闲。"""
        websocket = await self._register()
        execution_id = await self.plane.lease_task("redroid-01", "Restart", "admin")

        stopped_id = await self.plane.request_stop("redroid-01")

        self.assertEqual(stopped_id, execution_id)
        self.assertEqual(websocket.messages[-1]["type"], "stop_task")
        self.assertEqual(websocket.messages[-1]["execution_id"], execution_id)
        await self.plane.report_result("redroid-01", {
            "execution_id": execution_id,
            "config_revision": 1,
            "status": "cancelled",
            "summary": "stop marker observed",
        })
        snapshot = await self.plane.snapshot("redroid-01")
        self.assertIsNone(snapshot["lease"])
        self.assertEqual(snapshot["last_result"]["status"], "cancelled")

    async def test_lease_and_start_frames_cannot_be_interleaved_by_stop(self) -> None:
        """并发停止只能排在 task_lease/start_task 两帧之后。"""
        await self._register(task_names=("Restart",))
        runtime = self.plane._devices["redroid-01"]
        sent: list[str] = []

        class FakeWebSocket:
            """让连续发送主动让出事件循环以暴露锁竞争。"""

            async def send_json(self, payload: dict[str, object]) -> None:
                sent.append(str(payload["type"]))
                await asyncio.sleep(0)

        runtime.websocket = FakeWebSocket()
        lease_task = asyncio.create_task(self.plane.lease_task("redroid-01", "Restart", "admin"))
        await asyncio.sleep(0)
        await self.plane.request_stop("redroid-01")
        await lease_task

        self.assertEqual(sent, ["task_lease", "start_task", "stop_task"])

    async def test_task_started_confirms_current_lease_idempotently(self) -> None:
        """Agent 启动确认必须匹配当前租约，重复确认不能生成第二个事件。"""
        await self._register(task_names=("Restart",))
        execution_id = await self.plane.lease_task("redroid-01", "Restart", "test")

        first = await self.plane.acknowledge_task_started("redroid-01", {
            "execution_id": execution_id,
            "task_name": "Restart",
            "config_revision": 1,
        })
        second = await self.plane.acknowledge_task_started("redroid-01", {
            "execution_id": execution_id,
            "task_name": "Restart",
            "config_revision": 1,
        })

        snapshot = await self.plane.snapshot("redroid-01")
        events = await self.plane.control_events("redroid-01")
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertIsNotNone(snapshot["lease"]["started_at"])
        self.assertEqual(sum(event["event"] == "task_started" for event in events), 1)

    async def test_scheduler_stops_after_busy_device_instead_of_scanning_candidates(self) -> None:
        """同机已有租约时，调度循环不能把后续到期任务也视为可尝试项。"""
        websocket = await self._register(task_names=("Restart", "DailyTrifles", "Exploration"))
        execution_id = await self.plane.lease_task("redroid-01", "Restart", "test")

        await self.plane.dispatch_due_tasks()

        self.assertEqual([item["type"] for item in websocket.messages], ["task_lease", "start_task"])
        self.assertEqual((await self.plane.snapshot("redroid-01"))["lease"]["execution_id"], execution_id)

    async def test_incompatible_agent_cannot_receive_lease(self) -> None:
        """镜像版本不匹配时 Server 只返回原因，不执行任务。"""
        websocket = await self._register(version="old")
        snapshot = await self.plane.snapshot("redroid-01")
        self.assertFalse(snapshot["compatible"])
        self.assertEqual(websocket.messages, [])
        with self.assertRaisesRegex(ValueError, "required"):
            await self.plane.lease_task("redroid-01", "Restart", "test")

    async def test_agent_without_root_shell_capability_is_rejected(self) -> None:
        """首版 RootShell 设备拒绝没有本机 Root 后端的错误 Agent 镜像。"""
        websocket = FakeWebSocket()
        ack = await self.plane.register_hello(websocket, {
            "protocol_version": 1,
            "device_id": "redroid-01",
            "sent_at": "2026-11-26T00:00:00",
            "sequence": 1,
            "agent_version": "2026.11.26",
            "resource_version": "2026.11.26",
            "capabilities": ["local_tasks"],
            "task_manifest": [{"name": "Restart", "minimum_resource_version": "2026.11.26"}],
        })

        self.assertFalse(ack["accepted"])
        self.assertIn("root_shell", ack["reason"])

    async def test_hello_replays_unstarted_lease(self) -> None:
        """断线发生在 start_task 前时，重连 hello 会重发同一 execution_id。"""
        first = await self._register()
        execution_id = await self.plane.lease_task("redroid-01", "Restart", "test")
        await self.plane.unregister(first, "redroid-01")
        second = FakeWebSocket()
        ack = await self.plane.register_hello(second, {
            "protocol_version": 1,
            "device_id": "redroid-01",
            "sent_at": "2026-11-26T00:00:01",
            "sequence": 1,
            "agent_version": "2026.11.26",
            "resource_version": "2026.11.26",
            "capabilities": ["root_shell"],
            "task_manifest": [{"name": "Restart", "minimum_resource_version": "2026.11.26"}],
            "health": healthy_health(),
        })
        self.assertEqual(ack["resume_lease"]["execution_id"], execution_id)
        self.assertEqual(ack["resume_start"]["execution_id"], execution_id)

    async def test_hello_does_not_replay_started_lease_without_execution_state(self) -> None:
        """已启动租约缺少执行态时不能交给新 Agent，避免重复触控。"""
        first = await self._register(task_names=("Restart",))
        execution_id = await self.plane.lease_task("redroid-01", "Restart", "test")
        await self.plane.acknowledge_task_started("redroid-01", {
            "execution_id": execution_id,
            "task_name": "Restart",
            "config_revision": 1,
        })
        await self.plane.unregister(first, "redroid-01")

        second = FakeWebSocket()
        ack = await self.plane.register_hello(second, {
            "protocol_version": 1,
            "device_id": "redroid-01",
            "sent_at": "2026-11-26T00:00:01",
            "sequence": 1,
            "agent_version": "2026.11.26",
            "resource_version": "2026.11.26",
            "capabilities": ["root_shell"],
            "task_manifest": [{"name": "Restart", "minimum_resource_version": "2026.11.26"}],
            "health": healthy_health(),
        })

        self.assertNotIn("resume_lease", ack)
        self.assertNotIn("resume_start", ack)
        snapshot = await self.plane.snapshot("redroid-01")
        self.assertEqual(snapshot["lease"]["execution_id"], execution_id)
        self.assertIn("阻止自动重放", snapshot["recovery_reason"])

    async def test_admin_recovery_resolution_requires_matching_blocked_lease(self) -> None:
        """人工收尾只能释放明确处于恢复阻塞态且编号匹配的已启动租约。"""
        first = await self._register(task_names=("Restart",))
        execution_id = await self.plane.lease_task("redroid-01", "Restart", "test")
        await self.plane.acknowledge_task_started("redroid-01", {
            "execution_id": execution_id,
            "task_name": "Restart",
            "config_revision": 1,
        })
        await self.plane.unregister(first, "redroid-01")
        second = FakeWebSocket()
        await self.plane.register_hello(second, {
            "protocol_version": 1,
            "device_id": "redroid-01",
            "sent_at": "2026-11-26T00:00:01",
            "sequence": 1,
            "agent_version": "2026.11.26",
            "resource_version": "2026.11.26",
            "capabilities": ["root_shell"],
            "task_manifest": [{"name": "Restart", "minimum_resource_version": "2026.11.26"}],
        })
        with self.assertRaisesRegex(ValueError, "不一致"):
            await self.plane.resolve_recovery("redroid-01", "wrong-execution")
        resolved = await self.plane.resolve_recovery("redroid-01", execution_id)
        self.assertEqual(resolved, execution_id)
        self.assertIsNone((await self.plane.snapshot("redroid-01"))["lease"])
        events = await self.plane.control_events("redroid-01")
        self.assertEqual(events[0]["event"], "recovery_resolved")

    async def test_hello_blocks_mismatched_agent_execution_id_without_replay(self) -> None:
        """新 Agent 报告另一执行编号时必须显式阻塞，不能静默保留旧租约。"""
        first = await self._register(task_names=("Restart",))
        execution_id = await self.plane.lease_task("redroid-01", "Restart", "test")
        await self.plane.unregister(first, "redroid-01")
        second = FakeWebSocket()
        ack = await self.plane.register_hello(second, {
            "protocol_version": 1,
            "device_id": "redroid-01",
            "sent_at": "2026-11-26T00:00:01",
            "sequence": 1,
            "agent_version": "2026.11.26",
            "resource_version": "2026.11.26",
            "capabilities": ["root_shell"],
            "task_manifest": [{"name": "Restart", "minimum_resource_version": "2026.11.26"}],
            "current_execution_id": "unexpected-execution",
            "current_task_name": "Restart",
            "current_config_revision": 1,
        })

        self.assertNotIn("resume_lease", ack)
        snapshot = await self.plane.snapshot("redroid-01")
        self.assertEqual(snapshot["lease"]["execution_id"], execution_id)
        self.assertIn("不一致", snapshot["recovery_reason"])

    async def test_second_healthy_connection_for_same_device_is_rejected(self) -> None:
        """健康旧会话仍在时拒绝同 device_id 接管，避免两套 Agent 并发触控。"""
        first = await self._register(task_names=("Restart",))
        second = FakeWebSocket()

        ack = await self.plane.register_hello(second, {
            "protocol_version": 1,
            "device_id": "redroid-01",
            "sent_at": "2026-11-26T00:00:01",
            "sequence": 1,
            "agent_version": "2026.11.26",
            "resource_version": "2026.11.26",
            "capabilities": ["root_shell"],
            "task_manifest": [{"name": "Restart", "minimum_resource_version": "2026.11.26"}],
        })

        self.assertFalse(ack["accepted"])
        self.assertIn("并发连接", ack["reason"])
        execution_id = await self.plane.lease_task("redroid-01", "Restart", "test")
        self.assertEqual(first.messages[-1]["execution_id"], execution_id)
        self.assertEqual(second.messages, [])

    async def test_compare_and_swap_rejects_stale_device_editor(self) -> None:
        """管理员并发编辑必须使用当前 revision，避免覆盖 Agent 调度补丁。"""
        config = self.repository.read("redroid-01")
        self.repository.replace("redroid-01", config, expected_revision=1)
        with self.assertRaises(DeviceConfigConflictError):
            self.repository.replace("redroid-01", config, expected_revision=1)

    async def test_shared_device_template_is_a_valid_first_agent_config(self) -> None:
        """交付模板包含首批每日任务并默认关闭周期任务和手动恢复命令。"""
        # 设备模板由独立控制面管理，不属于 Agent 的 OAS 运行时目录。
        template_path = (
            Path(__file__).resolve().parents[2]
            / "server"
            / "config"
            / "template.agent-device.json"
        )
        template = json.loads(template_path.read_text(encoding="utf-8"))
        template["device_id"] = "template-redroid-01"
        saved = self.repository.replace("template-redroid-01", template, expected_revision=None)
        self.assertEqual(saved["revision"], 1)
        self.assertFalse(saved["tasks"]["Restart"]["scheduler"]["enable"])
        self.assertFalse(saved["tasks"]["Restart"]["restart_config"]["enable_daily"])
        self.assertFalse(saved["tasks"]["GotoMain"]["scheduler"]["enable"])
        self.assertIn("DailyTrifles", saved["tasks"])
        self.assertIn("SoulsTidy", saved["tasks"])
        self.assertTrue({"Orochi", "EvoZone", "GoldYoukai", "ExperienceYoukai", "Sougenbi"}.issubset(saved["tasks"]))

    async def test_restart_daily_chain_requires_declared_daily_trifles_task(self) -> None:
        """设备 JSON 不能开启 Restart 的关联任务链却遗漏调度目标。"""
        config = device_config()
        config["tasks"].pop("DailyTrifles")
        config["tasks"]["Restart"]["restart_config"] = {"enable_daily": True}
        with self.assertRaisesRegex(ValueError, "DailyTrifles"):
            self.repository.replace("redroid-01", config, expected_revision=1)

        config["tasks"]["DailyTrifles"] = {
            "scheduler": {"enable": False, "next_run": "2020-01-01 00:00:00", "priority": 5}
        }
        saved = self.repository.replace("redroid-01", config, expected_revision=1)
        self.assertIn("DailyTrifles", saved["tasks"])

    async def test_restart_daily_chain_is_disabled_when_device_json_omits_setting(self) -> None:
        """缺少开关时不能继承桌面 OAS 的每日琐事默认任务链。"""
        config = device_config()
        config["tasks"]["Restart"].pop("restart_config", None)

        saved = self.repository.replace("redroid-01", config, expected_revision=1)

        self.assertEqual(saved["tasks"]["Restart"]["restart_config"], {"enable_daily": False})

    async def test_restart_daily_setting_must_be_boolean(self) -> None:
        """配置编辑器不能用字符串等宽松值意外开启关联任务。"""
        config = device_config()
        config["tasks"]["Restart"]["restart_config"] = {"enable_daily": "true"}

        with self.assertRaisesRegex(DeviceConfigError, "enable_daily 必须是 bool"):
            self.repository.replace("redroid-01", config, expected_revision=1)

    async def test_device_config_requires_restart_for_game_level_recovery(self) -> None:
        """任何 Agent 任务都需要同快照中的 Restart 完成游戏级恢复。"""
        config = device_config()
        config["tasks"].pop("Restart")

        with self.assertRaisesRegex(DeviceConfigError, "必须包含 Restart"):
            self.repository.replace("redroid-01", config, expected_revision=1)

    async def test_device_config_rejects_game_package_unknown_to_oas(self) -> None:
        """Server 不能保存 OAS 的 PackageName 枚举无法加载的游戏包名。"""
        config = device_config()
        config["device"]["game_package"] = "com.example.onmyoji"

        with self.assertRaisesRegex(DeviceConfigError, "当前 OAS 支持"):
            self.repository.replace("redroid-01", config, expected_revision=1)

    async def test_device_config_accepts_known_oas_channel_game_package(self) -> None:
        """既有 OAS 已支持的渠道服包名应能作为 Root Agent 目标。"""
        config = device_config()
        config["device"]["game_package"] = "com.netease.onmyoji.bili"

        saved = self.repository.replace("redroid-01", config, expected_revision=1)

        self.assertEqual(saved["device"]["game_package"], "com.netease.onmyoji.bili")

    async def test_soul_overflow_source_requires_declared_souls_tidy_task(self) -> None:
        """首批副本的御魂溢出收尾任务必须存在于同一 Server 快照。"""
        config = device_config()
        config["tasks"].pop("SoulsTidy")

        with self.assertRaisesRegex(DeviceConfigError, "SoulsTidy"):
            self.repository.replace("redroid-01", config, expected_revision=1)

        config["tasks"]["SoulsTidy"] = {
            "scheduler": {"enable": False, "next_run": "2020-01-01 00:00:00", "priority": 5}
        }
        saved = self.repository.replace("redroid-01", config, expected_revision=1)
        self.assertIn("SoulsTidy", saved["tasks"])

    async def test_first_wave_rejects_unmigrated_exploration_and_orochi_branches(self) -> None:
        """首批清单外的关联任务不能由配置开关间接安排到设备本地。"""
        config = device_config()
        config["tasks"]["Exploration"]["scrolls"] = {"scrolls_enable": True}
        with self.assertRaisesRegex(DeviceConfigError, "RealmRaid.*MemoryScrolls"):
            self.repository.replace("redroid-01", config, expected_revision=1)

        config = device_config()
        config["tasks"]["Orochi"] = {
            "scheduler": {"enable": False, "next_run": "2020-01-01 00:00:00", "priority": 5},
            "orochi_config": {"check_true_orochi_enable": True},
        }
        with self.assertRaisesRegex(DeviceConfigError, "TrueOrochi"):
            self.repository.replace("redroid-01", config, expected_revision=1)

    async def test_device_config_rejects_task_name_agent_cannot_materialize(self) -> None:
        """Server 必须在保存时拒绝 Agent 无法转换为本地键的任务名。"""
        config = device_config()
        config["tasks"]["Bad-Task"] = {
            "scheduler": {"enable": False, "next_run": "2020-01-01 00:00:00", "priority": 5}
        }

        with self.assertRaisesRegex(DeviceConfigError, "只能包含字母和数字"):
            self.repository.replace("redroid-01", config, expected_revision=1)

    async def test_goto_main_cannot_be_enabled_for_periodic_dispatch(self) -> None:
        """回主页只允许管理员手动恢复，不能成为 Server 周期任务。"""
        config = device_config()
        config["tasks"]["GotoMain"] = {
            "scheduler": {"enable": True, "next_run": "2020-01-01 00:00:00", "priority": 0}
        }

        with self.assertRaisesRegex(DeviceConfigError, "只能手动执行"):
            self.repository.replace("redroid-01", config, expected_revision=1)

    async def test_rejected_schedule_patch_keeps_original_lease_result_path(self) -> None:
        """管理员改配置造成补丁冲突时，Agent 仍能回传原租约的结果。"""
        await self._register(task_names=("Restart",))
        execution_id = await self.plane.lease_task("redroid-01", "Restart", "test")
        current = self.repository.read("redroid-01")
        self.repository.replace("redroid-01", current, expected_revision=1)
        patch_message = {
            "execution_id": execution_id,
            "config_revision": 1,
            "patch": {"tasks": {"Restart": {"scheduler": {"next_run": "2099-01-01 00:00:00"}}}},
        }
        with self.assertRaisesRegex(ValueError, "revision"):
            await self.plane.apply_schedule_patch("redroid-01", patch_message)
        rejected = await self.plane.reject_schedule_patch("redroid-01", patch_message, "revision conflict")
        self.assertFalse(rejected["accepted"])
        self.assertEqual(rejected["config_revision"], 2)
        accepted = await self.plane.report_result("redroid-01", {
            "execution_id": execution_id,
            "config_revision": 1,
            "status": "success",
            "summary": "task completed with stale schedule patch",
        })
        self.assertFalse(accepted["duplicate"])

    async def test_server_restart_reconstructs_agent_reported_lease_for_result(self) -> None:
        """Server 重启丢失内存态后，只恢复 Agent 报告的执行编号用于收尾。"""
        recovered_plane = AgentControlPlane(self.repository)
        websocket = FakeWebSocket()
        ack = await recovered_plane.register_hello(websocket, {
            "protocol_version": 1,
            "device_id": "redroid-01",
            "sent_at": "2026-11-26T00:00:00",
            "sequence": 1,
            "agent_version": "2026.11.26",
            "resource_version": "2026.11.26",
            "capabilities": ["root_shell"],
            "task_manifest": [{"name": "Restart", "minimum_resource_version": "2026.11.26"}],
            "health": healthy_health(),
            "current_execution_id": "recovered-execution",
            "current_task_name": "Restart",
            "current_config_revision": 1,
        })
        self.assertTrue(ack["accepted"])
        snapshot = await recovered_plane.snapshot("redroid-01")
        self.assertEqual(snapshot["lease"]["requested_by"], "agent-recovery")
        result = await recovered_plane.report_result("redroid-01", {
            "execution_id": "recovered-execution",
            "config_revision": 1,
            "status": "success",
            "summary": "completed while Server was restarting",
        })
        self.assertFalse(result["duplicate"])

    async def test_server_restart_recovers_result_when_task_was_removed_from_new_config(self) -> None:
        """任务被管理员删除后，Server 重启仍须收尾旧 Agent execution。"""
        config = self.repository.read("redroid-01")
        execution_id = "recovered-daily"
        config["tasks"].pop("DailyTrifles")
        self.repository.replace("redroid-01", config, expected_revision=1)

        recovered_plane = AgentControlPlane(self.repository)
        websocket = FakeWebSocket()
        ack = await recovered_plane.register_hello(websocket, {
            "protocol_version": 1,
            "device_id": "redroid-01",
            "sent_at": "2026-11-26T00:00:00",
            "sequence": 1,
            "agent_version": "2026.11.26",
            "resource_version": "2026.11.26",
            "capabilities": ["root_shell"],
            "task_manifest": [{"name": "DailyTrifles", "minimum_resource_version": "2026.11.26"}],
            "current_execution_id": execution_id,
            "current_task_name": "DailyTrifles",
            "current_config_revision": 1,
        })
        self.assertTrue(ack["accepted"])
        snapshot = await recovered_plane.snapshot("redroid-01")
        self.assertEqual(snapshot["lease"]["execution_id"], execution_id)
        result = await recovered_plane.report_result("redroid-01", {
            "execution_id": execution_id,
            "config_revision": 1,
            "status": "success",
            "summary": "completed before task removal",
        })
        self.assertFalse(result["duplicate"])

    async def test_error_bundle_ack_requires_uploaded_matching_error_and_revision(self) -> None:
        """错误包通知必须绑定已落盘目录、当前租约和配置版本。"""
        await self._register(task_names=("Restart",))
        execution_id = await self.plane.lease_task("redroid-01", "Restart", "test")

        with self.assertRaisesRegex(ValueError, "尚未完成 HTTP 上传"):
            await self.plane.acknowledge_error_bundle("redroid-01", {
                "execution_id": execution_id,
                "config_revision": 1,
                "error_id": "redroid-01_1",
            })

        task_name, existing_error_id = await self.plane.reserve_error_bundle_upload(
            "redroid-01", execution_id, 1,
        )
        self.assertEqual(task_name, "Restart")
        self.assertIsNone(existing_error_id)
        await self.plane.complete_error_bundle_upload("redroid-01", execution_id, "redroid-01_1")

        with self.assertRaisesRegex(ValueError, "配置修订号"):
            await self.plane.acknowledge_error_bundle("redroid-01", {
                "execution_id": execution_id,
                "config_revision": 2,
                "error_id": "redroid-01_1",
            })
        with self.assertRaisesRegex(ValueError, "不一致"):
            await self.plane.acknowledge_error_bundle("redroid-01", {
                "execution_id": execution_id,
                "config_revision": 1,
                "error_id": "redroid-01_2",
            })

        ack = await self.plane.acknowledge_error_bundle("redroid-01", {
            "execution_id": execution_id,
            "config_revision": 1,
            "error_id": "redroid-01_1",
        })
        self.assertFalse(ack["duplicate"])
        duplicate = await self.plane.acknowledge_error_bundle("redroid-01", {
            "execution_id": execution_id,
            "config_revision": 1,
            "error_id": "redroid-01_1",
        })
        self.assertTrue(duplicate["duplicate"])

    async def test_failure_result_cannot_skip_error_bundle_ack(self) -> None:
        """已上传错误包但未确认时，失败结果不能释放当前租约。"""
        await self._register(task_names=("Restart",))
        execution_id = await self.plane.lease_task("redroid-01", "Restart", "test")
        await self.plane.reserve_error_bundle_upload("redroid-01", execution_id, 1)
        await self.plane.complete_error_bundle_upload("redroid-01", execution_id, "redroid-01_1")
        with self.assertRaisesRegex(ValueError, "尚未收到 Server 确认"):
            await self.plane.report_result("redroid-01", {
                "execution_id": execution_id,
                "config_revision": 1,
                "status": "failure",
                "summary": "error bundle was not acknowledged",
                "error_id": "redroid-01_1",
            })
        self.assertIsNotNone((await self.plane.snapshot("redroid-01"))["lease"])

    async def test_failure_result_requires_confirmed_error_bundle(self) -> None:
        """失败或人工接管不能跳过错误包，确认后才允许释放租约。"""
        await self._register(task_names=("Restart",))
        execution_id = await self.plane.lease_task("redroid-01", "Restart", "test")
        with self.assertRaisesRegex(ValueError, "必须关联已确认错误包"):
            await self.plane.report_result("redroid-01", {
                "execution_id": execution_id,
                "config_revision": 1,
                "status": "manual_required",
                "summary": "local task requested manual handling",
            })

        await self.plane.reserve_error_bundle_upload("redroid-01", execution_id, 1)
        await self.plane.complete_error_bundle_upload("redroid-01", execution_id, "redroid-01_2")
        await self.plane.acknowledge_error_bundle("redroid-01", {
            "execution_id": execution_id,
            "config_revision": 1,
            "error_id": "redroid-01_2",
        })
        accepted = await self.plane.report_result("redroid-01", {
            "execution_id": execution_id,
            "config_revision": 1,
            "status": "manual_required",
            "summary": "local task requested manual handling",
            "error_id": "redroid-01_2",
        })
        self.assertFalse(accepted["duplicate"])
        snapshot = await self.plane.snapshot("redroid-01")
        self.assertIsNone(snapshot["lease"])
        self.assertEqual(snapshot["last_result"]["error_id"], "redroid-01_2")

    async def test_control_events_expose_metadata_without_agent_log_contents(self) -> None:
        """管理事件窗口只包含控制面元数据，不会混入设备任务日志内容。"""
        await self._register(task_names=("Restart",))
        execution_id = await self.plane.lease_task("redroid-01", "Restart", "admin")
        await self.plane.request_stop("redroid-01")
        await self.plane.report_result("redroid-01", {
            "execution_id": execution_id,
            "config_revision": 1,
            "status": "cancelled",
            "summary": "device-local log must not be copied",
        })
        events = await self.plane.control_events("redroid-01")
        event_names = [item["event"] for item in events]
        self.assertIn("connected", event_names)
        self.assertIn("task_leased", event_names)
        self.assertIn("stop_requested", event_names)
        self.assertIn("task_result", event_names)
        self.assertNotIn("device-local log must not be copied", str(events))
        self.assertEqual(events[0]["event"], "task_result")

    async def test_result_summary_is_sanitized_before_management_display(self) -> None:
        """取消结果的摘要也不能向 Web UI 泄漏 Agent 工作路径。"""
        await self._register(task_names=("Restart",))
        execution_id = await self.plane.lease_task("redroid-01", "Restart", "admin")
        await self.plane.report_result("redroid-01", {
            "execution_id": execution_id,
            "config_revision": 1,
            "status": "cancelled",
            "summary": 'File "F:\\Git\\Onmyoji\\OnmyojiAutoScript-Self\\secret.py", line 1',
        })

        summary = (await self.plane.snapshot("redroid-01"))["last_result"]["summary"]
        self.assertIn("C:\\fakepath\\OnmyojiAutoScript", summary)
        self.assertNotIn("F:\\Git\\Onmyoji", summary)

    async def test_task_missing_from_agent_manifest_is_rejected_with_reason(self) -> None:
        """Server 不向镜像未声明的任务下发租约，并在状态中暴露原因。"""
        await self._register(task_names=("Restart",))
        with self.assertRaisesRegex(ValueError, "未声明支持任务"):
            await self.plane.lease_task("redroid-01", "Exploration", "test")
        snapshot = await self.plane.snapshot("redroid-01")
        self.assertFalse(snapshot["task_support"]["Exploration"])
        self.assertIn("未声明支持任务", snapshot["task_support_reasons"]["Exploration"])


if __name__ == "__main__":
    unittest.main()
