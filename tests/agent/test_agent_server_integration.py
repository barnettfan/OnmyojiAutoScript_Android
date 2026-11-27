"""Android Agent 与实际 Server Agent 路由的 loopback 集成回归。"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
import unittest
from contextlib import suppress
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import uvicorn
import websockets
from fastapi import FastAPI

import oas_control_plane.agent_router as agent_router_module
from oas_control_plane.agent_control_plane import AgentControlPlane
from oas_control_plane.agent_router import agent_control_plane, agent_repository, agent_transport_app
from oas_android_agent.config import AgentSettings
from oas_android_agent.service import AndroidAgent, PendingLease


def device_config() -> dict:
    """构造仅用于 Agent 握手的最小 RootShell 设备配置。"""
    return {
        "device_id": "redroid-01",
        "display_name": "loopback integration",
        "revision": 0,
        "required_agent_version": "2026.11.26",
        "device": {
            "transport": "root_shell",
            "game_package": "com.netease.onmyoji",
            "screen_size": [1280, 720],
        },
        "runtime": {},
        "tasks": {
            "Restart": {
                "scheduler": {"enable": True, "next_run": "2000-01-01 00:00:00", "priority": 0},
            },
        },
    }


class AgentServerIntegrationTest(unittest.IsolatedAsyncioTestCase):
    """验证真实 WebSocket 帧能穿过 Agent、FastAPI 路由和控制面。"""

    async def asyncSetUp(self) -> None:
        """隔离模块级 Server 配置，避免写入开发设备 JSON。"""
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_root = agent_repository.root
        self.original_secret = os.environ.get("OAS_AGENT_SHARED_SECRET")
        self.environment = patch.dict(os.environ, {"OAS_AGENT_SHARED_SECRET": "agent-secret"})
        self.environment.start()
        agent_repository.root = Path(self.tempdir.name)
        agent_control_plane._devices.clear()
        agent_repository.replace("redroid-01", device_config(), expected_revision=None)

        runtime_root = Path(self.tempdir.name) / "runtime"
        runtime_root.mkdir()
        self.agent = AndroidAgent(AgentSettings(
            device_id="redroid-01",
            server_url="ws://127.0.0.1:1",
            shared_secret="agent-secret",
            runtime_root=runtime_root,
            state_root=Path(self.tempdir.name) / "state",
            agent_version="2026.11.26",
            resource_version="2026.11.26",
            heartbeat_seconds=15,
        ))

    async def asyncTearDown(self) -> None:
        """恢复模块级仓库根目录和部署密钥。"""
        agent_control_plane._devices.clear()
        agent_repository.root = self.original_root
        self.environment.stop()
        if self.original_secret is None:
            os.environ.pop("OAS_AGENT_SHARED_SECRET", None)
        else:
            os.environ["OAS_AGENT_SHARED_SECRET"] = self.original_secret
        self.tempdir.cleanup()

    async def test_agent_handshake_reaches_actual_fastapi_control_plane(self) -> None:
        """真实传输必须完成共享密钥校验、快照确认和健康报告。"""
        app = FastAPI()
        app.include_router(agent_transport_app)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.setblocking(False)
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off", access_log=False, log_level="critical"))
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        serve_task: asyncio.Task | None = None
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(server.started, "loopback FastAPI server did not start")

            self.agent.settings = replace(self.agent.settings, server_url=f"ws://127.0.0.1:{port}")
            health = {"status": "healthy", "reason": None, "checks": {"root": True}}
            with patch.object(self.agent, "_preflight", return_value=health):
                async with websockets.connect(self.agent._websocket_url()) as websocket:
                    self.agent.websocket = websocket
                    serve_task = asyncio.create_task(self.agent._serve_connection(websocket))
                    snapshot: dict | None = None
                    for _ in range(100):
                        snapshot = await agent_control_plane.snapshot("redroid-01")
                        if snapshot["health"]["status"] == "healthy":
                            break
                        await asyncio.sleep(0.01)
                    self.assertIsNotNone(snapshot)
                    self.assertTrue(snapshot["online"])
                    self.assertTrue(snapshot["compatible"])
                    self.assertEqual(snapshot["config_revision"], 1)
                    self.assertEqual(snapshot["confirmed_config_revision"], 1)
                    self.assertEqual(snapshot["health"]["status"], "healthy")
                    await websocket.close()
                    with self.assertRaises(websockets.ConnectionClosed):
                        await asyncio.wait_for(serve_task, timeout=2)
        finally:
            self.agent.websocket = None
            if serve_task is not None and not serve_task.done():
                serve_task.cancel()
                # close() 与 cancel() 的时间点竞争时，任务可能保留 WebSocket
                # 的正常关闭异常，而非先收到 CancelledError；两者都表示测试
                # 传输已停止，不应掩盖已验证的握手断言。
                with suppress(asyncio.CancelledError, websockets.ConnectionClosed):
                    await serve_task
            server.should_exit = True
            await asyncio.wait_for(server_task, timeout=2)
            listener.close()

    async def test_lease_schedule_patch_and_result_complete_over_actual_transport(self) -> None:
        """真实 WSS 往返应确认启动、原子调度补丁和最终结果。

        本测试替换本地 OAS 子进程，仅验证 Agent 与 FastAPI 控制面之间的协议
        顺序；因此不触发截图、OCR、Root shell 或任何游戏操作。
        """
        app = FastAPI()
        app.include_router(agent_transport_app)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.setblocking(False)
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off", access_log=False, log_level="critical"))
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        serve_task: asyncio.Task | None = None

        async def complete_without_game_operation(lease: object) -> None:
            """模拟本地任务结束，并保留 Agent 的真实 outbox 发送流程。"""
            # ``task_started`` 必须先被服务端确认，再允许结果进入 outbox，确保
            # 本测试覆盖正式协议的状态顺序，而不是直接伪造最终消息。
            await asyncio.sleep(0.01)
            started_at = datetime.now().isoformat()
            self.agent._write_outbox(
                lease,
                {
                    "status": "success",
                    "summary": "loopback task completed",
                    "started_at": started_at,
                    "finished_at": datetime.now().isoformat(),
                    "duration_seconds": 0.01,
                },
                {
                    "tasks": {
                        "Restart": {
                            "scheduler": {
                                "next_run": "2099-01-01 00:00:00",
                            },
                        },
                    },
                },
            )
            await self.agent._flush_outbox()

        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(server.started, "loopback FastAPI server did not start")

            self.agent.settings = replace(self.agent.settings, server_url=f"ws://127.0.0.1:{port}")
            health = {"status": "healthy", "reason": None, "checks": {"root": True}}
            with (
                patch.object(self.agent, "_preflight", return_value=health),
                patch.object(self.agent, "_run_task_process", side_effect=complete_without_game_operation),
            ):
                async with websockets.connect(self.agent._websocket_url()) as websocket:
                    self.agent.websocket = websocket
                    serve_task = asyncio.create_task(self.agent._serve_connection(websocket))
                    for _ in range(100):
                        snapshot = await agent_control_plane.snapshot("redroid-01")
                        if snapshot["health"]["status"] == "healthy":
                            break
                        await asyncio.sleep(0.01)
                    execution_id = await agent_control_plane.lease_task(
                        "redroid-01", "Restart", requested_by="loopback-test"
                    )

                    snapshot = None
                    for _ in range(200):
                        snapshot = await agent_control_plane.snapshot("redroid-01")
                        if snapshot["last_result"] and snapshot["last_result"]["execution_id"] == execution_id:
                            break
                        await asyncio.sleep(0.01)
                    self.assertIsNotNone(snapshot)
                    self.assertIsNone(snapshot["lease"])
                    self.assertEqual(snapshot["last_result"]["status"], "success")
                    self.assertEqual(snapshot["last_result"]["summary"], "loopback task completed")
                    self.assertEqual(snapshot["config_revision"], 2)
                    self.assertEqual(snapshot["confirmed_config_revision"], 2)
                    self.assertEqual(
                        snapshot["tasks"]["Restart"]["scheduler"]["next_run"],
                        "2099-01-01 00:00:00",
                    )
                    events = await agent_control_plane.control_events("redroid-01")
                    self.assertTrue(any(event["event"] == "task_started" for event in events))
                    self.assertTrue(any(event["event"] == "schedule_patch_applied" for event in events))
                    self.assertTrue(any(event["event"] == "task_result" for event in events))
                    await websocket.close()
                    with self.assertRaises(websockets.ConnectionClosed):
                        await asyncio.wait_for(serve_task, timeout=2)
        finally:
            self.agent.websocket = None
            if serve_task is not None and not serve_task.done():
                serve_task.cancel()
                with suppress(asyncio.CancelledError, websockets.ConnectionClosed):
                    await serve_task
            server.should_exit = True
            await asyncio.wait_for(server_task, timeout=2)
            listener.close()

    async def test_failure_uploads_error_bundle_before_result_over_actual_transports(self) -> None:
        """失败现场应先经实际 HTTP 落盘，再经实际 WSS 提交失败结果。

        本测试不运行游戏或 Root 操作：它只预置一次任务失败时会产生的本地错误
        目录，并替换任务子进程。由真实 ``ErrorBundleUploader`` 请求同一
        Uvicorn Server，验证错误包不是普通 WebSocket 截图通道的一部分。
        """
        error_root = Path(self.tempdir.name) / "server-error"
        original_error_root = agent_router_module.ERROR_LOG_ROOT
        agent_router_module.ERROR_LOG_ROOT = error_root
        started_at = datetime.now().isoformat()
        error_directory = self.agent.settings.runtime_root / "log" / "error" / (
            f"agent-redroid-01_{int(datetime.now().timestamp() * 1000)}"
        )
        error_directory.mkdir(parents=True)
        (error_directory / "last.png").write_bytes(b"failure-screenshot")
        (error_directory / "log.txt").write_text("task failed locally", encoding="utf-8")
        (error_directory / "context.json").write_text('{"page":"battle"}', encoding="utf-8")

        async def fail_without_game_operation(lease: PendingLease) -> None:
            """模拟 OAS 子进程失败，并保留 Agent 的真实 outbox 上传顺序。"""
            self.agent._write_outbox(
                lease,
                {
                    "status": "failure",
                    "summary": "loopback task failed",
                    "started_at": started_at,
                    "finished_at": datetime.now().isoformat(),
                    "duration_seconds": 0.01,
                },
                None,
            )
            await self.agent._flush_outbox()

        app = FastAPI()
        app.include_router(agent_transport_app)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.setblocking(False)
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off", access_log=False, log_level="critical"))
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        serve_task: asyncio.Task | None = None
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(server.started, "loopback FastAPI server did not start")

            self.agent.settings = replace(self.agent.settings, server_url=f"ws://127.0.0.1:{port}")
            health = {"status": "healthy", "reason": None, "checks": {"root": True}}
            with (
                patch.object(self.agent, "_preflight", return_value=health),
                patch.object(self.agent, "_run_task_process", side_effect=fail_without_game_operation),
            ):
                async with websockets.connect(self.agent._websocket_url()) as websocket:
                    self.agent.error_uploader.settings = self.agent.settings
                    self.agent._set_connection(websocket)
                    serve_task = asyncio.create_task(self.agent._serve_connection(websocket))
                    for _ in range(100):
                        snapshot = await agent_control_plane.snapshot("redroid-01")
                        if snapshot["health"]["status"] == "healthy":
                            break
                        await asyncio.sleep(0.01)
                    execution_id = await agent_control_plane.lease_task(
                        "redroid-01", "Restart", requested_by="loopback-failure-test"
                    )

                    snapshot: dict | None = None
                    for _ in range(300):
                        snapshot = await agent_control_plane.snapshot("redroid-01")
                        if snapshot["last_result"] and snapshot["last_result"]["execution_id"] == execution_id:
                            break
                        await asyncio.sleep(0.01)
                    self.assertIsNotNone(snapshot)
                    self.assertIsNone(snapshot["lease"])
                    self.assertEqual(snapshot["last_result"]["status"], "failure")
                    error_id = snapshot["last_result"]["error_id"]
                    self.assertTrue(error_id)
                    # HTTP 上传生成的权威上下文是 task_result 之前的强制前置；
                    # 如果上传失败，Server 不会有 error_id，也不会接受本结果。
                    context = json.loads((error_root / error_id / "context.json").read_text(encoding="utf-8"))
                    self.assertEqual(context["execution_id"], execution_id)
                    self.assertEqual(context["result"]["status"], "failure")
                    self.assertTrue(list((error_root / error_id).glob("*_last.png")))
                    # Server 落盘结果与 Agent 消费 result ACK 是两个事件；等待
                    # ACK 完成后再检查 outbox，避免把正常 WebSocket 调度时序当成
                    # 失败现场链路的问题。
                    for _ in range(100):
                        if not self.agent._outbox_path(execution_id).exists():
                            break
                        await asyncio.sleep(0.01)
                    self.assertFalse(self.agent._outbox_path(execution_id).exists())
                    await websocket.close()
                    with self.assertRaises(websockets.ConnectionClosed):
                        await asyncio.wait_for(serve_task, timeout=2)
        finally:
            self.agent.websocket = None
            if serve_task is not None and not serve_task.done():
                serve_task.cancel()
                with suppress(asyncio.CancelledError, websockets.ConnectionClosed):
                    await serve_task
            server.should_exit = True
            await asyncio.wait_for(server_task, timeout=2)
            listener.close()
            agent_router_module.ERROR_LOG_ROOT = original_error_root

    async def test_server_restart_recovers_error_bundle_before_failure_result(self) -> None:
        """重启后的 Server 应从错误目录恢复确认，再接收失败结果。

        此测试把控制面替换为全新的实例来模拟 Server 进程重启。Agent 本地状态
        已停留在旧 Server 确认错误包后的 ``result`` 阶段；它重连后必须先重申
        ``error_id``，路由从权威 context.json 恢复关联并 ACK，随后才可释放
        恢复出的租约。
        """
        execution_id = "execution-error-server-restart"
        error_id = "redroid-01_1700000000000"
        error_root = Path(self.tempdir.name) / "error"
        error_folder = error_root / error_id
        error_folder.mkdir(parents=True)
        (error_folder / "context.json").write_text(
            json.dumps({
                "device_id": "redroid-01",
                "execution_id": execution_id,
                "task_name": "Restart",
                "config_revision": 1,
                "result": {"status": "failure", "summary": "persisted failure"},
            }),
            encoding="utf-8",
        )

        # 模拟 Agent 已在旧 Server 收到错误包确认，并在持久化 outbox 中等待
        # 最终结果回执。新 Server 只能从 hello 和错误目录恢复该执行上下文。
        self.agent.state.current_execution_id = execution_id
        self.agent.state.current_task_name = "Restart"
        self.agent.state.current_config_revision = 1
        self.agent.state_store.save(self.agent.state)
        self.agent._write_outbox(
            PendingLease(execution_id, "Restart", 1, {}),
            {"status": "failure", "summary": "loopback task failed", "error_id": error_id},
            None,
        )
        outbox = self.agent._read_outbox(execution_id)
        self.assertIsNotNone(outbox)
        outbox["phase"] = "result"
        self.agent._atomic_write_json(self.agent._outbox_path(execution_id), outbox)

        original_control_plane = agent_router_module.agent_control_plane
        original_error_root = agent_router_module.ERROR_LOG_ROOT
        restarted_control_plane = AgentControlPlane(agent_repository)
        agent_router_module.agent_control_plane = restarted_control_plane
        agent_router_module.ERROR_LOG_ROOT = error_root
        app = FastAPI()
        app.include_router(agent_transport_app)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.setblocking(False)
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off", access_log=False, log_level="critical"))
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        serve_task: asyncio.Task | None = None
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(server.started, "loopback FastAPI server did not start")

            self.agent.settings = replace(self.agent.settings, server_url=f"ws://127.0.0.1:{port}")
            health = {"status": "healthy", "reason": None, "checks": {"root": True}}
            with patch.object(self.agent, "_preflight", return_value=health):
                async with websockets.connect(self.agent._websocket_url()) as websocket:
                    self.agent._set_connection(websocket)
                    serve_task = asyncio.create_task(self.agent._serve_connection(websocket))
                    snapshot: dict | None = None
                    for _ in range(200):
                        snapshot = await restarted_control_plane.snapshot("redroid-01")
                        if snapshot["last_result"] and snapshot["last_result"]["execution_id"] == execution_id:
                            break
                        await asyncio.sleep(0.01)
                    self.assertIsNotNone(snapshot)
                    self.assertIsNone(snapshot["lease"])
                    self.assertEqual(snapshot["last_result"]["status"], "failure")
                    self.assertEqual(snapshot["last_result"]["error_id"], error_id)
                    events = await restarted_control_plane.control_events("redroid-01")
                    self.assertTrue(any(event["event"] == "error_bundle_recovered" for event in events))
                    self.assertTrue(any(event["event"] == "error_bundle_acknowledged" for event in events))
                    self.assertTrue(any(event["event"] == "task_result" for event in events))
                    # Server 记录结果与 Agent 收到 result ACK 是两次独立的网络
                    # 事件；必须等待 ACK 被 Agent 消费后，才断言本地 outbox 已
                    # 清理，否则这里会把正常的 WebSocket 调度时序误判为失败。
                    for _ in range(100):
                        if not self.agent._outbox_path(execution_id).exists() and self.agent.state.current_execution_id is None:
                            break
                        await asyncio.sleep(0.01)
                    self.assertFalse(self.agent._outbox_path(execution_id).exists())
                    self.assertIsNone(self.agent.state.current_execution_id)
                    await websocket.close()
                    with self.assertRaises(websockets.ConnectionClosed):
                        await asyncio.wait_for(serve_task, timeout=2)
        finally:
            self.agent.websocket = None
            if serve_task is not None and not serve_task.done():
                serve_task.cancel()
                with suppress(asyncio.CancelledError, websockets.ConnectionClosed):
                    await serve_task
            server.should_exit = True
            await asyncio.wait_for(server_task, timeout=2)
            listener.close()
            agent_router_module.agent_control_plane = original_control_plane
            agent_router_module.ERROR_LOG_ROOT = original_error_root


if __name__ == "__main__":
    unittest.main()
