"""Android Agent HTTP/WSS 鉴权与握手回归测试。"""
from __future__ import annotations

import os
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import oas_control_plane.agent_router as agent_router_module
import oas_control_plane.log_service as log_service_module
from oas_control_plane.agent_router import agent_app, agent_control_plane, agent_repository, agent_transport_app
from oas_control_plane.api_logger import log_http_access, log_ws_event


def device_config() -> dict:
    """构造用于 WSS hello 的最小 RootShell 设备配置。"""
    return {
        "device_id": "redroid-01",
        "display_name": "测试设备",
        "revision": 0,
        "required_agent_version": "2026.11.26",
        "device": {"transport": "root_shell", "game_package": "com.netease.onmyoji", "screen_size": [1280, 720]},
        "runtime": {},
        "tasks": {"Restart": {"scheduler": {"enable": True, "next_run": "2099-01-01 00:00:00", "priority": 0}}},
    }


class AgentRouterTest(unittest.TestCase):
    """证明管理面与 Agent 传输面使用互不相同的凭据。"""

    def setUp(self) -> None:
        """给模块级控制面切换测试目录和短生命周期凭据。"""
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_root = agent_repository.root
        self.original_agent_secret = os.environ.get("OAS_AGENT_SHARED_SECRET")
        self.original_admin_password = os.environ.get("OAS_AGENT_ADMIN_PASSWORD")
        self.original_router_error_root = agent_router_module.ERROR_LOG_ROOT
        self.original_log_error_root = log_service_module.ERROR_LOG_ROOT
        os.environ["OAS_AGENT_SHARED_SECRET"] = "agent-secret"
        os.environ["OAS_AGENT_ADMIN_PASSWORD"] = "admin-password"
        agent_repository.root = Path(self.tempdir.name)
        error_root = Path(self.tempdir.name) / "error"
        agent_router_module.ERROR_LOG_ROOT = error_root
        log_service_module.ERROR_LOG_ROOT = error_root
        agent_control_plane._devices.clear()
        agent_repository.replace("redroid-01", device_config(), expected_revision=None)
        self.app = FastAPI()
        self.app.include_router(agent_app)
        self.app.include_router(agent_transport_app)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        """恢复全局配置，避免影响同进程内其他服务端测试。"""
        self.client.close()
        agent_control_plane._devices.clear()
        agent_repository.root = self.original_root
        agent_router_module.ERROR_LOG_ROOT = self.original_router_error_root
        log_service_module.ERROR_LOG_ROOT = self.original_log_error_root
        self._restore_environment("OAS_AGENT_SHARED_SECRET", self.original_agent_secret)
        self._restore_environment("OAS_AGENT_ADMIN_PASSWORD", self.original_admin_password)
        self.tempdir.cleanup()

    @staticmethod
    def _restore_environment(name: str, value: str | None) -> None:
        """按测试前状态恢复一个环境变量。"""
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

    def test_management_api_requires_independent_admin_password(self) -> None:
        """Agent 共享密钥不能访问设备管理 API。"""
        self.assertEqual(self.client.get("/agents/devices").status_code, 401)
        self.assertEqual(self.client.get("/agents/devices", auth=("admin", "agent-secret")).status_code, 401)
        response = self.client.get("/agents/devices", auth=("admin", "admin-password"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["device_id"], "redroid-01")

    def test_websocket_event_logging_does_not_raise_when_api_log_is_locked(self) -> None:
        """控制面审计日志不可写时不能成为 Agent WSS 断线原因。"""
        with patch(
            "oas_control_plane.api_logger.ensure_api_logger",
            side_effect=PermissionError("api log is locked"),
        ):
            log_ws_event("agent[redroid-01] connected")

    def test_http_access_logging_does_not_turn_management_api_into_500(self) -> None:
        """API 日志文件锁定时仍应返回管理接口本身的响应。"""
        with patch(
            "oas_control_plane.api_logger.ensure_api_logger",
            side_effect=PermissionError("api log is locked"),
        ):
            log_http_access({"response": {"status_code": 200}, "request": {"path": "/agents/devices"}})

    def test_control_events_endpoint_is_admin_only_and_does_not_pull_agent_logs(self) -> None:
        """设备事件接口只返回控制面元数据，且沿用独立管理员鉴权。"""
        self.assertEqual(self.client.get("/agents/devices/redroid-01/events").status_code, 401)
        response = self.client.get("/agents/devices/redroid-01/events", auth=("admin", "admin-password"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"items": []})

    def test_recovery_resolution_endpoint_requires_admin_and_blocked_lease(self) -> None:
        """人工恢复接口不能绕过管理员鉴权，也不能释放普通空闲设备。"""
        path = "/agents/devices/redroid-01/recovery/resolve"
        self.assertEqual(
            self.client.post(path, json={"execution_id": "execution-1"}).status_code,
            401,
        )
        response = self.client.post(
            path,
            auth=("admin", "admin-password"),
            json={"execution_id": "execution-1"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("没有待人工确认", response.json()["detail"])

    def test_recovery_resolution_endpoint_releases_matching_blocked_lease(self) -> None:
        """管理员确认准确的执行编号后，HTTP 接口应释放恢复阻塞租约。"""
        execution_id = self._create_recovery_blocked_lease()

        response = self.client.post(
            "/agents/devices/redroid-01/recovery/resolve",
            auth=("admin", "admin-password"),
            json={"execution_id": execution_id},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "device_id": "redroid-01",
            "execution_id": execution_id,
            "resolved": True,
        })

        import asyncio

        snapshot = asyncio.run(agent_control_plane.snapshot("redroid-01"))
        self.assertIsNone(snapshot["lease"])

    def test_admin_can_register_a_new_device_from_revision_zero(self) -> None:
        """管理面可以以模板 JSON 原子创建第二台设备。"""
        config = device_config()
        config["device_id"] = "redroid-02"
        response = self.client.post("/agents/devices", auth=("admin", "admin-password"), json={"config": config})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["revision"], 1)

    def test_agent_wss_uses_shared_secret_and_returns_versioned_snapshot(self) -> None:
        """正确密钥的 hello 获取配置，错误密钥会被关闭。"""
        with self.client.websocket_connect("/agent/ws") as websocket:
            websocket.send_json({
                "type": "hello",
                "protocol_version": 1,
                "device_id": "redroid-01",
                "sent_at": "2026-11-26T00:00:00",
                "sequence": 1,
                "secret": "agent-secret",
                "agent_version": "2026.11.26",
                "resource_version": "2026.11.26",
                "capabilities": ["root_shell"],
                "task_manifest": [{"name": "Restart", "minimum_resource_version": "2026.11.26"}],
            })
            ack = websocket.receive_json()
            websocket.send_json({
                "type": "health_report",
                "device_id": "redroid-01",
                "sent_at": "2026-11-26T00:00:01",
                "sequence": 2,
                "secret": "agent-secret",
                "config_revision": 1,
                "health": {
                    "status": "healthy",
                    "checks": {"runtime": True, "state_root": True, "root": True, "screen": True, "game_package": True},
                },
            })
            health_ack = websocket.receive_json()
        self.assertTrue(ack["accepted"])
        self.assertEqual(ack["config_revision"], 1)
        self.assertEqual(ack["sequence"], 1)
        self.assertIn("sent_at", ack)
        self.assertEqual(health_ack["type"], "health_report_ack")
        self.assertEqual(health_ack["health"]["status"], "healthy")
        with self.client.websocket_connect("/agent/ws") as websocket:
            websocket.send_json({
                "type": "hello",
                "protocol_version": 1,
                "device_id": "redroid-01",
                "sent_at": "2026-11-26T00:00:00",
                "sequence": 1,
                "secret": "wrong-secret",
                "agent_version": "2026.11.26",
                "resource_version": "2026.11.26",
            })
            with self.assertRaises(WebSocketDisconnect):
                websocket.receive_json()

    def test_wss_rejects_non_string_shared_secret_without_server_error(self) -> None:
        """畸形 JSON 密钥必须稳定关闭连接，不能触发 compare_digest 类型异常。"""
        with self.client.websocket_connect("/agent/ws") as websocket:
            websocket.send_json({
                "type": "hello",
                "protocol_version": 1,
                "device_id": "redroid-01",
                "sent_at": "2026-11-26T00:00:00",
                "sequence": 1,
                "secret": {"not": "a string"},
            })
            with self.assertRaises(WebSocketDisconnect):
                websocket.receive_json()

    def test_wss_rejects_media_fields_outside_error_bundle_upload(self) -> None:
        """普通 WSS 控制消息不能携带截图字段，失败现场仍走独立 HTTP 上传。"""
        with self.client.websocket_connect("/agent/ws") as websocket:
            websocket.send_json({
                "type": "hello",
                "protocol_version": 1,
                "device_id": "redroid-01",
                "sent_at": "2026-11-26T00:00:00",
                "sequence": 1,
                "secret": "agent-secret",
                "agent_version": "2026.11.26",
                "resource_version": "2026.11.26",
                "capabilities": ["root_shell"],
                "task_manifest": [{"name": "Restart", "minimum_resource_version": "2026.11.26"}],
            })
            self.assertTrue(websocket.receive_json()["accepted"])
            websocket.send_json({
                "type": "health_report",
                "device_id": "redroid-01",
                "sent_at": "2026-11-26T00:00:00.500",
                "sequence": 2,
                "secret": "agent-secret",
                "config_revision": 1,
                "health": {
                    "status": "healthy",
                    "checks": {"root": True, "root_screenshot": True},
                },
            })
            self.assertEqual(websocket.receive_json()["type"], "health_report_ack")
            websocket.send_json({
                "type": "heartbeat",
                "device_id": "redroid-01",
                "sent_at": "2026-11-26T00:00:01",
                "sequence": 3,
                "secret": "agent-secret",
                # 即使未来客户端把诊断信息包在嵌套对象中，也不能借此把画面
                # 携带进控制面 WSS。
                "diagnostics": {"screenshot_base64": "iVBORw0KGgo="},
            })
            error = websocket.receive_json()
            self.assertEqual(error["type"], "error")
            self.assertIn("媒体字段", error["reason"])
            with self.assertRaises(WebSocketDisconnect):
                websocket.receive_json()

    def test_wss_agent_can_receive_manual_task_and_return_result(self) -> None:
        """真实 FastAPI WebSocket 路由覆盖租约、启动和结果 ACK 的完整往返。"""
        with self.client.websocket_connect("/agent/ws") as websocket:
            websocket.send_json({
                "type": "hello",
                "protocol_version": 1,
                "device_id": "redroid-01",
                "sent_at": "2026-11-26T00:00:00",
                "sequence": 1,
                "secret": "agent-secret",
                "agent_version": "2026.11.26",
                "resource_version": "2026.11.26",
                "capabilities": ["root_shell"],
                "task_manifest": [{"name": "Restart", "minimum_resource_version": "2026.11.26"}],
                "health": {
                    "status": "healthy",
                    "checks": {"runtime": True, "state_root": True, "root": True, "screen": True, "game_package": True},
                },
            })
            hello_ack = websocket.receive_json()
            self.assertTrue(hello_ack["accepted"])
            websocket.send_json({
                "type": "health_report",
                "device_id": "redroid-01",
                "sent_at": "2026-11-26T00:00:01",
                "sequence": 2,
                "secret": "agent-secret",
                "config_revision": 1,
                "health": {
                    "status": "healthy",
                    "checks": {"runtime": True, "state_root": True, "root": True, "screen": True, "game_package": True},
                },
            })
            health_ack = websocket.receive_json()
            self.assertEqual(health_ack["type"], "health_report_ack")

            # TestClient 的 WebSocket 会话正在当前线程占用 portal；管理 HTTP
            # 请求放到独立线程和独立 TestClient，才能覆盖真实双通道并发。
            def request_manual_task() -> object:
                with TestClient(self.app) as management_client:
                    return management_client.post(
                        "/agents/devices/redroid-01/execute",
                        auth=("admin", "admin-password"),
                        json={"task_name": "Restart"},
                    )

            with ThreadPoolExecutor(max_workers=1) as executor:
                execute = executor.submit(request_manual_task).result(timeout=5)
            self.assertEqual(execute.status_code, 200)
            execution_id = execute.json()["execution_id"]
            lease = websocket.receive_json()
            start = websocket.receive_json()
            self.assertEqual(lease["type"], "task_lease")
            self.assertEqual(start["type"], "start_task")
            self.assertEqual(start["execution_id"], execution_id)
            websocket.send_json({
                "type": "task_started",
                "device_id": "redroid-01",
                "sent_at": "2026-11-26T00:00:01.500",
                "sequence": 3,
                "secret": "agent-secret",
                "execution_id": execution_id,
                "task_name": "Restart",
                "config_revision": lease["config_revision"],
            })
            started_ack = websocket.receive_json()
            self.assertEqual(started_ack["type"], "task_started_ack")
            self.assertFalse(started_ack["duplicate"])
            websocket.send_json({
                "type": "task_result",
                "device_id": "redroid-01",
                "sent_at": "2026-11-26T00:00:02",
                "sequence": 4,
                "secret": "agent-secret",
                "execution_id": execution_id,
                "config_revision": lease["config_revision"],
                "status": "success",
                "summary": "completed in WSS integration test",
                "duration_seconds": 1.25,
            })
            result_ack = websocket.receive_json()
            self.assertEqual(result_ack["type"], "task_result_ack")
            self.assertFalse(result_ack["duplicate"])

    def test_wss_error_bundle_ack_binds_http_upload_before_failure_result(self) -> None:
        """模拟 Agent 必须在 HTTP 错误包落盘并确认后才能完成失败任务。"""
        with self.client.websocket_connect("/agent/ws") as websocket:
            websocket.send_json({
                "type": "hello",
                "protocol_version": 1,
                "device_id": "redroid-01",
                "sent_at": "2026-11-26T00:00:00",
                "sequence": 1,
                "secret": "agent-secret",
                "agent_version": "2026.11.26",
                "resource_version": "2026.11.26",
                "capabilities": ["root_shell"],
                "task_manifest": [{"name": "Restart", "minimum_resource_version": "2026.11.26"}],
            })
            websocket.receive_json()
            websocket.send_json({
                "type": "health_report",
                "device_id": "redroid-01",
                "sent_at": "2026-11-26T00:00:01",
                "sequence": 2,
                "secret": "agent-secret",
                "config_revision": 1,
                "health": {"status": "healthy", "checks": {"root": True}},
            })
            websocket.receive_json()

            def request_manual_task() -> object:
                """在独立 TestClient 中请求任务，避免 WebSocket portal 被占用。"""
                with TestClient(self.app) as management_client:
                    return management_client.post(
                        "/agents/devices/redroid-01/execute",
                        auth=("admin", "admin-password"),
                        json={"task_name": "Restart"},
                    )

            with ThreadPoolExecutor(max_workers=1) as executor:
                execute = executor.submit(request_manual_task).result(timeout=5)
            self.assertEqual(execute.status_code, 200)
            execution_id = execute.json()["execution_id"]
            lease = websocket.receive_json()
            websocket.receive_json()

            def upload_bundle() -> object:
                """模拟 Agent 的 HTTPS 错误上传，保持 WSS 会话在线。"""
                with TestClient(self.app) as upload_client:
                    return upload_client.post(
                        f"/agent/error-bundles/redroid-01/{execution_id}",
                        headers={"x-oas-agent-secret": "agent-secret"},
                        data={"metadata": self._error_bundle_metadata(execution_id)},
                        files=[("files", ("log.txt", b"local failure", "text/plain"))],
                    )

            with ThreadPoolExecutor(max_workers=1) as executor:
                uploaded = executor.submit(upload_bundle).result(timeout=5)
            self.assertEqual(uploaded.status_code, 200)
            error_id = uploaded.json()["error_id"]

            websocket.send_json({
                "type": "error_bundle",
                "device_id": "redroid-01",
                "sent_at": "2026-11-26T00:00:02",
                "sequence": 3,
                "secret": "agent-secret",
                "execution_id": execution_id,
                "config_revision": lease["config_revision"],
                "error_id": error_id,
            })
            error_ack = websocket.receive_json()
            self.assertEqual(error_ack["type"], "error_bundle_ack")
            self.assertEqual(error_ack["execution_id"], execution_id)
            self.assertEqual(error_ack["config_revision"], lease["config_revision"])
            self.assertEqual(error_ack["error_id"], error_id)

            websocket.send_json({
                "type": "task_result",
                "device_id": "redroid-01",
                "sent_at": "2026-11-26T00:00:03",
                "sequence": 4,
                "secret": "agent-secret",
                "execution_id": execution_id,
                "config_revision": lease["config_revision"],
                "status": "failure",
                "summary": "task failed after captured error bundle",
                "error_id": error_id,
            })
            result_ack = websocket.receive_json()
            self.assertEqual(result_ack["type"], "task_result_ack")
            self.assertFalse(result_ack["duplicate"])

    def test_error_bundle_only_accepts_agent_secret_and_is_visible_to_admin(self) -> None:
        """失败截图和日志进入既有错误目录，并可由管理员专用接口读取。"""
        execution_id = self._lease_error_bundle_execution()
        files = [
            ("files", ("last.png", b"not-a-real-png-for-route-test", "image/png")),
            ("files", ("log.txt", b"local task failed", "text/plain")),
            ("files", ("context.json", b'{"task_name":"Restart","status":"failure"}', "application/json")),
        ]
        forbidden = self.client.post(
            f"/agent/error-bundles/redroid-01/{execution_id}",
            data={"metadata": self._error_bundle_metadata(execution_id)},
            files=files,
        )
        self.assertEqual(forbidden.status_code, 401)
        uploaded = self.client.post(
            f"/agent/error-bundles/redroid-01/{execution_id}",
            headers={"x-oas-agent-secret": "agent-secret"},
            data={"metadata": self._error_bundle_metadata(execution_id)},
            files=files,
        )
        self.assertEqual(uploaded.status_code, 200)
        error_id = uploaded.json()["error_id"]
        # Agent 可能在服务端已落盘、但 HTTP 响应丢失后用同一 execution_id 重传。
        # 服务端必须返回同一个目录而非再保存一套截图和日志。
        duplicated = self.client.post(
            f"/agent/error-bundles/redroid-01/{execution_id}",
            headers={"x-oas-agent-secret": "agent-secret"},
            data={"metadata": self._error_bundle_metadata(execution_id)},
            files=files,
        )
        self.assertEqual(duplicated.status_code, 200)
        self.assertTrue(duplicated.json()["duplicate"])
        self.assertEqual(duplicated.json()["error_id"], error_id)
        # 模拟 Server 重启后内存去重表丢失：必须从同设备落盘的权威
        # context.json 复用原目录，不能再次保存同一失败截图和日志。
        runtime = agent_control_plane._devices["redroid-01"]
        runtime.error_bundle_ids.clear()
        recovered = self.client.post(
            f"/agent/error-bundles/redroid-01/{execution_id}",
            headers={"x-oas-agent-secret": "agent-secret"},
            data={"metadata": self._error_bundle_metadata(execution_id)},
            files=files,
        )
        self.assertEqual(recovered.status_code, 200)
        self.assertTrue(recovered.json()["duplicate"])
        self.assertEqual(recovered.json()["error_id"], error_id)
        detail = self.client.get(
            f"/agents/devices/redroid-01/errors/{error_id}",
            auth=("admin", "admin-password"),
        )
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["log"]["content"], "local task failed")
        # attachment 中的 context.json 不能覆盖 Server 用已验证租约写入的元数据。
        self.assertEqual(detail.json()["context"]["task_name"], "Restart")
        self.assertEqual(detail.json()["context"]["execution_id"], execution_id)
        self.assertEqual(detail.json()["context"]["result"]["status"], "failure")
        self.assertEqual(detail.json()["task_context"]["status"], "failure")
        self.assertEqual(len(detail.json()["images"]), 1)
        listing = self.client.get(
            "/agents/devices/redroid-01/errors",
            auth=("admin", "admin-password"),
        )
        self.assertEqual(len(listing.json()["items"]), 1)

    def test_error_bundle_rejects_normal_task_result(self) -> None:
        """错误上传接口不能被正常任务拿来传输截图。"""
        execution_id = self._lease_error_bundle_execution()
        response = self.client.post(
            f"/agent/error-bundles/redroid-01/{execution_id}",
            headers={"x-oas-agent-secret": "agent-secret"},
            data={"metadata": self._error_bundle_metadata(execution_id, status="success")},
            files=[("files", ("normal.png", b"not-an-error", "image/png"))],
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("failure", response.json()["detail"])

    def test_error_bundle_rejects_oversized_metadata(self) -> None:
        """错误现场元数据受独立上限保护，不能绕过文件大小限制撑大请求。"""
        execution_id = self._lease_error_bundle_execution()
        metadata = self._error_bundle_metadata(execution_id)[:-1] + (',"extra":"' + "x" * 300_000 + '"}')
        response = self.client.post(
            f"/agent/error-bundles/redroid-01/{execution_id}",
            headers={"x-oas-agent-secret": "agent-secret"},
            data={"metadata": metadata},
            files=[("files", ("log.txt", b"failure", "text/plain"))],
        )
        self.assertEqual(response.status_code, 413)
        self.assertIn("metadata", response.json()["detail"])

    def test_error_bundle_is_sanitized_again_on_server(self) -> None:
        """Server 不信任 Agent 的脱敏结果，并只把已验证字段写入权威上下文。"""
        execution_id = self._lease_error_bundle_execution()
        metadata = {
            "device_id": "redroid-01",
            "execution_id": execution_id,
            "task_name": "Restart",
            "config_revision": 1,
            "agent_secret_path": "/data/oas/OnmyojiAutoScript-Self/secret.txt",
            "result": {
                "status": "failure",
                "summary": 'File "/data/oas/OnmyojiAutoScript-Self/failure.py"',
            },
        }
        response = self.client.post(
            f"/agent/error-bundles/redroid-01/{execution_id}",
            headers={"x-oas-agent-secret": "agent-secret"},
            data={"metadata": json.dumps(metadata)},
            files=[
                ("files", ("log.txt", b'File "/data/oas/OnmyojiAutoScript-Self/task.log"', "text/plain")),
                ("files", ("context.json", b'{"trace":"File \\"/data/oas/OnmyojiAutoScript-Self/context.py\\""}', "application/json")),
                ("files", ("details.json", b'{"trace":"File \\"/data/oas/OnmyojiAutoScript-Self/broken.py\\"', "application/json")),
            ],
        )
        self.assertEqual(response.status_code, 200)
        error_id = response.json()["error_id"]
        folder = agent_router_module.ERROR_LOG_ROOT / error_id
        authority = json.loads((folder / "context.json").read_text(encoding="utf-8"))
        self.assertNotIn("agent_secret_path", authority)
        self.assertNotIn("/data/oas/OnmyojiAutoScript-Self", (folder / "context.json").read_text(encoding="utf-8"))
        self.assertIn("C:\\fakepath\\OnmyojiAutoScript", authority["result"]["summary"])
        self.assertNotIn("/data/oas/OnmyojiAutoScript-Self", (folder / "log.txt").read_text(encoding="utf-8"))
        self.assertNotIn("/data/oas/OnmyojiAutoScript-Self", (folder / "task-context.json").read_text(encoding="utf-8"))
        self.assertNotIn("/data/oas/OnmyojiAutoScript-Self", (folder / "02_details.json").read_text(encoding="utf-8"))

    def test_error_bundle_rejects_posix_and_windows_path_names(self) -> None:
        """错误包文件名不能利用另一平台的路径分隔符逃逸目录。"""
        for filename in ("../escape.txt", r"..\escape.txt", r"C:\escape.txt"):
            with self.assertRaises(HTTPException):
                agent_router_module._validate_error_bundle_filename(filename)
        # multipart 解析器会规范化 POSIX ``../``；这里额外验证实际 HTTP 边界
        # 仍会拒绝 Windows 风格路径，避免部署在 Linux 时漏掉反斜杠逃逸。
        execution_id = self._lease_error_bundle_execution()
        response = self.client.post(
            f"/agent/error-bundles/redroid-01/{execution_id}",
            headers={"x-oas-agent-secret": "agent-secret"},
            data={"metadata": self._error_bundle_metadata(execution_id)},
            files=[("files", (r"..\escape.txt", b"failure", "text/plain"))],
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("文件名", response.json()["detail"])

    def test_error_bundle_rejects_duplicate_context_files(self) -> None:
        """错误上下文只能有一份，不能允许同名文件在服务端相互覆盖。"""
        execution_id = self._lease_error_bundle_execution()
        response = self.client.post(
            f"/agent/error-bundles/redroid-01/{execution_id}",
            headers={"x-oas-agent-secret": "agent-secret"},
            data={"metadata": self._error_bundle_metadata(execution_id)},
            files=[
                ("files", ("context.json", b"{}", "application/json")),
                ("files", ("context.json", b"{}", "application/json")),
            ],
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("context.json", response.json()["detail"])

    def test_error_bundle_rejects_duplicate_task_log_files(self) -> None:
        """重复 log.txt 不能在同一错误目录中覆盖先前上传的任务日志。"""
        execution_id = self._lease_error_bundle_execution()
        response = self.client.post(
            f"/agent/error-bundles/redroid-01/{execution_id}",
            headers={"x-oas-agent-secret": "agent-secret"},
            data={"metadata": self._error_bundle_metadata(execution_id)},
            files=[
                ("files", ("log.txt", b"first", "text/plain")),
                ("files", ("log.txt", b"second", "text/plain")),
            ],
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("log.txt", response.json()["detail"])

    def test_error_bundle_rejects_task_name_different_from_lease(self) -> None:
        """错误包任务名必须与当前租约一致，防止现场归档到错误任务。"""
        execution_id = self._lease_error_bundle_execution()
        metadata = self._error_bundle_metadata(execution_id).replace('"task_name":"Restart"', '"task_name":"GotoMain"')
        response = self.client.post(
            f"/agent/error-bundles/redroid-01/{execution_id}",
            headers={"x-oas-agent-secret": "agent-secret"},
            data={"metadata": metadata},
            files=[("files", ("last.png", b"not-an-error", "image/png"))],
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("任务租约", response.json()["detail"])

        missing_task_name = self._error_bundle_metadata(execution_id).replace('"task_name":"Restart",', '')
        response = self.client.post(
            f"/agent/error-bundles/redroid-01/{execution_id}",
            headers={"x-oas-agent-secret": "agent-secret"},
            data={"metadata": missing_task_name},
            files=[("files", ("last.png", b"not-an-error", "image/png"))],
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("task_name", response.json()["detail"])

    def test_error_bundle_rejects_unknown_or_stale_execution(self) -> None:
        """共享密钥不足以给任意执行编号写入截图，必须匹配当前租约。"""
        execution_id = self._lease_error_bundle_execution()
        response = self.client.post(
            "/agent/error-bundles/redroid-01/not-current-execution",
            headers={"x-oas-agent-secret": "agent-secret"},
            data={"metadata": self._error_bundle_metadata("not-current-execution")},
            files=[("files", ("last.png", b"not-an-error", "image/png"))],
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("当前任务租约", response.json()["detail"])

        response = self.client.post(
            f"/agent/error-bundles/redroid-01/{execution_id}",
            headers={"x-oas-agent-secret": "agent-secret"},
            data={"metadata": self._error_bundle_metadata(execution_id, revision=2)},
            files=[("files", ("last.png", b"not-an-error", "image/png"))],
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("配置修订号", response.json()["detail"])

    def _lease_error_bundle_execution(self) -> str:
        """注册健康模拟 Agent 并领取错误现场关联的有效租约。"""
        class WebSocket:
            """满足控制面下发命令所需的最小 WebSocket 接口。"""

            async def send_json(self, _message: object) -> None:
                """测试不消费下发命令，只保持会话在线。"""

        import asyncio

        async def lease() -> str:
            websocket = WebSocket()
            await agent_control_plane.register_hello(websocket, {
                "protocol_version": 1,
                "device_id": "redroid-01",
                "sent_at": "2026-11-26T00:00:00",
                "sequence": 1,
                "agent_version": "2026.11.26",
                "resource_version": "2026.11.26",
                "capabilities": ["root_shell"],
                "task_manifest": [{"name": "Restart", "minimum_resource_version": "2026.11.26"}],
            })
            await agent_control_plane.report_health("redroid-01", {
                "config_revision": 1,
                "health": {"status": "healthy", "checks": {"root": True}},
            })
            return await agent_control_plane.lease_task("redroid-01", "Restart", "test")

        return asyncio.run(lease())

    def _create_recovery_blocked_lease(self) -> str:
        """构造已启动但新 Agent 缺失执行态的恢复阻塞租约。"""
        class WebSocket:
            """满足控制面会话登记所需的最小 WebSocket 接口。"""

            async def send_json(self, _message: object) -> None:
                """测试无需消费控制面下发的命令。"""

        import asyncio

        async def create() -> str:
            first = WebSocket()
            await agent_control_plane.register_hello(first, {
                "protocol_version": 1,
                "device_id": "redroid-01",
                "sent_at": "2026-11-26T00:00:00",
                "sequence": 1,
                "agent_version": "2026.11.26",
                "resource_version": "2026.11.26",
                "capabilities": ["root_shell"],
                "task_manifest": [{"name": "Restart", "minimum_resource_version": "2026.11.26"}],
            })
            await agent_control_plane.report_health("redroid-01", {
                "config_revision": 1,
                "health": {"status": "healthy", "checks": {"root": True}},
            })
            execution_id = await agent_control_plane.lease_task("redroid-01", "Restart", "test")
            await agent_control_plane.acknowledge_task_started("redroid-01", {
                "execution_id": execution_id,
                "task_name": "Restart",
                "config_revision": 1,
            })
            await agent_control_plane.unregister(first, "redroid-01")

            # 新会话缺少 execution_id，必须进入人工恢复阻塞而非重放 start_task。
            await agent_control_plane.register_hello(WebSocket(), {
                "protocol_version": 1,
                "device_id": "redroid-01",
                "sent_at": "2026-11-26T00:00:01",
                "sequence": 1,
                "agent_version": "2026.11.26",
                "resource_version": "2026.11.26",
                "capabilities": ["root_shell"],
                "task_manifest": [{"name": "Restart", "minimum_resource_version": "2026.11.26"}],
            })
            return execution_id

        return asyncio.run(create())

    @staticmethod
    def _error_bundle_metadata(execution_id: str, revision: int = 1, status: str = "failure") -> str:
        """构造绑定当前租约版本的错误现场元数据。"""
        return (
            '{"device_id":"redroid-01","execution_id":"%s",'
            '"task_name":"Restart","config_revision":%d,"result":{"status":"%s"}}'
        ) % (execution_id, revision, status)


if __name__ == "__main__":
    unittest.main()
