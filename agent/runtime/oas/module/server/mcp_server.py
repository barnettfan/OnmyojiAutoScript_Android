# This Python file uses the following encoding: utf-8
"""Loopback-only Streamable HTTP MCP for OAS task authoring."""
from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP, Image
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from module.server.task_scaffold import TaskScaffolder
from module.server.config_manager import ConfigManager
from module.server.tool import AnnotatorError, annotator_manager


LOOPBACK_CLIENTS = frozenset({"127.0.0.1", "::1"})
MAX_UPLOAD_BYTES = 30 * 1024 * 1024


class LoopbackOnlyASGI:
    """拒绝所有非回环地址的 MCP 请求，避免暴露设备控制能力。"""

    def __init__(self, app: ASGIApp, mcp_server: FastMCP | None = None) -> None:
        self.app = app
        self.mcp_server = mcp_server

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        client = scope.get("client")
        client_host = client[0] if client else None
        if client_host not in LOOPBACK_CLIENTS:
            response = JSONResponse(
                {"error": "mcp_loopback_only", "message": "MCP 仅接受 127.0.0.1 或 ::1 客户端"},
                status_code=403,
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _error_payload(error: AnnotatorError) -> dict[str, Any]:
    """将标注服务的业务异常转换为 MCP 可读结果。"""
    return {
        "ok": False,
        "error": {"code": error.code, "message": error.message, "status_code": error.status_code},
    }


def _image_content(manager, session_id: str, image: dict[str, Any]) -> list[Any]:
    """返回会话图片元数据和原生 MCP 图片内容，而不是仅返回本地路径。"""
    try:
        image_file = manager.get_image_file(session_id, str(image["id"]))
    except AnnotatorError as error:
        return [_error_payload(error)]
    return [{"ok": True, "image": image}, Image(path=image_file)]


def _upload_local_image(manager, session_id: str, local_path: str) -> dict[str, Any]:
    """读取明确指定的本地截图，并复用标注会话的上传和校验逻辑。"""
    path = Path(str(local_path or "")).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise AnnotatorError("image_not_found", f"本地截图不存在: {path}", 404)
    if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".bmp", ".webp"}:
        raise AnnotatorError("invalid_image_ext", "本地截图格式不受支持", 400)
    if path.stat().st_size > MAX_UPLOAD_BYTES:
        raise AnnotatorError("image_too_large", "本地截图不能超过 30 MB", 400)
    payload = {
        "name": path.name,
        "content_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
    }
    return manager.save_uploaded_images_base64(session_id, [payload])[0]


def create_mcp_application(
    manager=annotator_manager,
    scaffolder: TaskScaffolder | None = None,
) -> ASGIApp:
    """构建嵌入 FastAPI 的 Streamable HTTP MCP 应用。"""
    scaffolder = scaffolder or TaskScaffolder()
    server = FastMCP(
        name="oas-task-authoring",
        instructions=(
            "OAS 任务制作工具。仅在用户明确要求创建、保存、加入规则、实现或执行点击/滑动/长按时调用写入或设备操作工具；"
            "执行设备操作后立刻调用 capture_screenshot 确认界面结果。"
        ),
        streamable_http_path="/",
    )

    @server.tool()
    def get_authoring_context() -> dict[str, Any]:
        """读取可用模拟器配置、现有任务、菜单分类和支持的任务骨架类型。"""
        return {
            "ok": True,
            "configs": manager.list_configs(),
            "tasks": manager.list_task_names(),
            "categories": scaffolder.categories(),
            "scaffold_types": ["basic", "battle"],
        }

    @server.tool()
    def read_config(config_name: str) -> dict[str, Any]:
        """读取指定 OAS 运行配置，并自动脱敏其中可能存在的凭据。"""
        try:
            name, config = ConfigManager.load_config_for_export(config_name)
        except FileNotFoundError as error:
            return {
                "ok": False,
                "error": {"code": "config_not_found", "message": str(error), "status_code": 404},
            }
        except ValueError as error:
            return {
                "ok": False,
                "error": {"code": "invalid_config", "message": str(error), "status_code": 400},
            }
        except OSError as error:
            return {
                "ok": False,
                "error": {"code": "config_read_failed", "message": str(error), "status_code": 500},
            }
        return {"ok": True, "config_name": name, "config": ConfigManager.redact_config(config)}

    @server.tool()
    def create_session() -> dict[str, Any]:
        """创建新的标注会话；新会话会关闭先前活跃会话。"""
        return {"ok": True, "session": manager.create_session()}

    @server.tool()
    def close_session(session_id: str) -> dict[str, Any]:
        """关闭标注会话及其临时截图。"""
        try:
            return {"ok": True, "result": manager.close_session(session_id, reason="mcp", raise_if_missing=True)}
        except AnnotatorError as error:
            return _error_payload(error)

    @server.tool()
    def start_capture(session_id: str, config_name: str, frame_rate: int = 2) -> dict[str, Any]:
        """连接指定模拟器配置并开始采集画面；不会启动或停止 OAS Web 服务。"""
        try:
            return {"ok": True, "emulator": manager.start_emulator(session_id, config_name, frame_rate)}
        except AnnotatorError as error:
            return _error_payload(error)

    @server.tool()
    def stop_capture(session_id: str) -> dict[str, Any]:
        """停止指定会话的模拟器画面采集。"""
        try:
            return {"ok": True, "emulator": manager.stop_emulator(session_id)}
        except AnnotatorError as error:
            return _error_payload(error)

    @server.tool()
    def get_capture_status(session_id: str) -> dict[str, Any]:
        """读取当前采集会话状态、错误信息和最近帧时间。"""
        try:
            return {"ok": True, "emulator": manager.emulator_status(session_id)}
        except AnnotatorError as error:
            return _error_payload(error)

    @server.tool()
    def capture_screenshot(session_id: str) -> list[Any]:
        """保存最近模拟器帧并以原生图片内容返回，供 AI 直接查看。"""
        try:
            image = manager.capture_from_emulator(session_id)
            return _image_content(manager, session_id, image)
        except AnnotatorError as error:
            return [_error_payload(error)]

    @server.tool()
    def upload_screenshot(session_id: str, local_path: str) -> list[Any]:
        """上传明确给出的本地截图到会话，并以原生图片内容返回。"""
        try:
            image = _upload_local_image(manager, session_id, local_path)
            return _image_content(manager, session_id, image)
        except AnnotatorError as error:
            return [_error_payload(error)]

    @server.tool()
    def list_session_images(session_id: str) -> dict[str, Any]:
        """列出当前标注会话保存的截图和上传图片。"""
        try:
            return {"ok": True, "images": manager.list_images(session_id)}
        except AnnotatorError as error:
            return _error_payload(error)

    @server.tool()
    def read_session_image(session_id: str, image_id: str) -> list[Any]:
        """以原生图片内容读取会话内的一张已保存图片。"""
        try:
            images = manager.list_images(session_id)
            image = next((item for item in images if item["id"] == image_id), None)
            if image is None:
                raise AnnotatorError("image_not_found", f"图片不存在: {image_id}", 404)
            return _image_content(manager, session_id, image)
        except AnnotatorError as error:
            return [_error_payload(error)]

    @server.tool()
    def get_rule_schema() -> dict[str, Any]:
        """读取截图标注器支持的规则字段、类型和默认值。"""
        return {"ok": True, **manager.rule_schema()}

    @server.tool()
    def load_rules(task_name: str, json_relpath: str) -> dict[str, Any]:
        """读取任务目录内的规则 JSON，不修改任何文件。"""
        try:
            return {"ok": True, **manager.load_rule_file(task_name, json_relpath)}
        except AnnotatorError as error:
            return _error_payload(error)

    @server.tool()
    def save_cropped_template(
        session_id: str,
        image_id: str,
        task_name: str,
        json_relpath: str,
        image_name: str,
        roi: str,
    ) -> dict[str, Any]:
        """从会话截图裁剪并保存一个规则模板图片。"""
        try:
            return {
                "ok": True,
                "result": manager.save_cropped_image(
                    session_id, image_id, task_name, json_relpath, image_name, roi
                ),
            }
        except AnnotatorError as error:
            return _error_payload(error)

    @server.tool()
    def test_rule(
        session_id: str,
        image_id: str,
        task_name: str,
        json_relpath: str,
        rule_type: str,
        rule: dict[str, Any],
        list_meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """在会话图片上测试图片、OCR 或动作规则；测试永不控制模拟器。"""
        try:
            return {
                "ok": True,
                "result": manager.test_rule(
                    session_id, image_id, task_name, json_relpath, rule_type, rule, list_meta
                ),
            }
        except AnnotatorError as error:
            return _error_payload(error)

    @server.tool()
    def save_rules(
        session_id: str,
        task_name: str,
        json_relpath: str,
        rule_type: str,
        rules: list[dict[str, Any]],
        list_meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """保存规则 JSON 并自动重新生成任务 assets.py；仅在用户明确要求保存规则时调用。"""
        try:
            return {
                "ok": True,
                "result": manager.save_rules_and_generate(
                    session_id, task_name, json_relpath, rule_type, rules, list_meta
                ),
            }
        except AnnotatorError as error:
            return _error_payload(error)

    @server.tool()
    def emulator_click(session_id: str, x: int, y: int) -> dict[str, Any]:
        """点击最近一帧范围内的明确坐标；调用后必须立即截图确认。"""
        try:
            return {"ok": True, "result": manager.perform_emulator_action(session_id, "click", x=x, y=y)}
        except AnnotatorError as error:
            return _error_payload(error)

    @server.tool()
    def emulator_swipe(session_id: str, x1: int, y1: int, x2: int, y2: int, duration: float = 0.3) -> dict[str, Any]:
        """在最近一帧范围内按明确坐标滑动；调用后必须立即截图确认。"""
        try:
            return {
                "ok": True,
                "result": manager.perform_emulator_action(
                    session_id, "swipe", x1=x1, y1=y1, x2=x2, y2=y2, duration=duration
                ),
            }
        except AnnotatorError as error:
            return _error_payload(error)

    @server.tool()
    def emulator_long_click(session_id: str, x: int, y: int, duration: float = 0.8) -> dict[str, Any]:
        """在最近一帧范围内按明确坐标长按；调用后必须立即截图确认。"""
        try:
            return {
                "ok": True,
                "result": manager.perform_emulator_action(
                    session_id, "long_click", x=x, y=y, duration=duration
                ),
            }
        except AnnotatorError as error:
            return _error_payload(error)

    @server.tool()
    def create_task_scaffold(
        task_name: str,
        chinese_name: str,
        category: str,
        scaffold_type: str,
        english_name: str | None = None,
        scheduler: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """创建 basic/battle 任务骨架并注册配置、菜单、模板、i18n 和优先级；拒绝覆盖同名任务。"""
        try:
            return {"ok": True, "result": scaffolder.create(
                task_name, chinese_name, category, scaffold_type, english_name, scheduler
            )}
        except AnnotatorError as error:
            return _error_payload(error)

    return LoopbackOnlyASGI(server.streamable_http_app(), server)


mcp_application = create_mcp_application()


@asynccontextmanager
async def mcp_lifespan():
    """让挂载到 FastAPI 的 MCP 会话管理器共享父服务生命周期。"""
    server = mcp_application.mcp_server
    if server is None:
        raise RuntimeError("MCP server is not initialized")
    async with server.session_manager.run():
        yield
