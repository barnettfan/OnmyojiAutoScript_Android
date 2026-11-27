"""Android Agent WebSocket、错误包上传和管理员管理接口。"""
from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .agent_control_plane import (
    AgentBusyError,
    AgentControlError,
    AgentControlPlane,
    AgentOfflineError,
)
from .agent_device_repository import (
    DeviceConfigConflictError,
    DeviceConfigError,
    DeviceConfigRepository,
)
from .agent_security import (
    AGENT_SECRET_HEADER,
    verify_admin_password,
    verify_agent_secret,
)
from .api_logger import ApiLoggingRoute, log_ws_event
from .sensitive import handle_sensitive_logs, handle_sensitive_text
from .log_service import ERROR_LOG_ROOT, build_error_log_dir_name
from .log_service import ensure_child_path, log_browser_service, LogServiceError, normalize_script_name


MAX_ERROR_BUNDLE_BYTES = 30 * 1024 * 1024
MAX_ERROR_FILE_BYTES = 20 * 1024 * 1024
MAX_PERSISTED_ERROR_CONTEXT_BYTES = 64 * 1024
MAX_ERROR_METADATA_BYTES = 256 * 1024
_SAFE_ERROR_BUNDLE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
# WSS 只传控制面元数据；这些字段名表示媒体或其编码，不能出现在普通 Agent 消息中。
# 不能简单匹配 ``screenshot`` 子串：健康预检合法地包含 ``root_screenshot``
# 布尔字段。只拦截明确的媒体字段和带媒体名称的编码字段。
_AGENT_WSS_MEDIA_KEYS = frozenset({
    "image", "screenshot", "frame",
    "image_data", "screenshot_data", "frame_data",
    "image_base64", "screenshot_base64", "frame_base64",
    "image_bytes", "screenshot_bytes", "frame_bytes",
})
basic_auth = HTTPBasic(auto_error=False)
CONTROL_PLANE_ROOT = Path(__file__).resolve().parents[1]
agent_repository = DeviceConfigRepository(CONTROL_PLANE_ROOT / "config" / "devices")
agent_control_plane = AgentControlPlane(agent_repository)

agent_app = APIRouter(prefix="/agents", tags=["android-agents"], route_class=ApiLoggingRoute)
agent_transport_app = APIRouter(prefix="/agent", tags=["android-agent-transport"])


def _sanitize_error_value(value: Any) -> Any:
    """递归清洗错误现场的文本字段，不信任 Agent 上传前的脱敏结果。"""
    if isinstance(value, str):
        return handle_sensitive_text(value)
    if isinstance(value, list):
        return [_sanitize_error_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _sanitize_error_value(item) for key, item in value.items()}
    return value


def _sanitize_error_log_bytes(content: bytes) -> bytes:
    """二次清洗上传日志，并容错处理截断或非法 UTF-8。"""
    text = content.decode("utf-8", errors="replace")
    return "".join(handle_sensitive_logs(text.splitlines(keepends=True))).encode("utf-8")


def _sanitize_error_json_bytes(content: bytes) -> bytes:
    """二次清洗 JSON；非法 JSON 转为可展示且已脱敏的合法对象。"""
    text = content.decode("utf-8", errors="replace")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = {"invalid_json_text": handle_sensitive_text(text)}
    return json.dumps(_sanitize_error_value(value), ensure_ascii=False).encode("utf-8")


def _reject_agent_wss_media_payload(message: dict[str, Any]) -> None:
    """拒绝普通 WSS 消息携带截图或图片编码，避免形成第二条截图通道。"""
    pending: list[object] = [message]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            for key, child in value.items():
                lowered_key = str(key).lower()
                if (
                    lowered_key in _AGENT_WSS_MEDIA_KEYS
                    or lowered_key.endswith(("_base64", "_bytes"))
                    and lowered_key.startswith(("image_", "screenshot_", "frame_"))
                ):
                    raise AgentControlError(f"Agent WSS 禁止媒体字段: {key!r}")
                pending.append(child)
            continue
        if isinstance(value, list):
            pending.extend(value)


def _validate_error_bundle_filename(filename: str | None) -> str:
    """只接受简单 ASCII 文件名，同时拒绝 POSIX 和 Windows 路径逃逸写法。"""
    name = str(filename or "")
    if (
        not _SAFE_ERROR_BUNDLE_NAME_RE.fullmatch(name)
        or name in {".", ".."}
        or PurePosixPath(name).name != name
        or PureWindowsPath(name).name != name
    ):
        raise HTTPException(status_code=422, detail="错误包文件名不合法")
    return name


def _authoritative_error_context(
    metadata: dict[str, Any],
    device_id: str,
    execution_id: str,
    task_name: str,
    config_revision: int,
) -> dict[str, Any]:
    """构建仅含已验证字段的权威上下文，丢弃 Agent 自定义顶层元数据。"""
    result = metadata["result"]
    context_result: dict[str, Any] = {"status": result["status"]}
    # 管理页只需要这些执行结果字段；其余 OAS 细节由 task-context.json 独立保存。
    for field, limit in (("summary", 1000), ("started_at", 255), ("finished_at", 255)):
        value = result.get(field)
        if isinstance(value, str) and value.strip():
            context_result[field] = handle_sensitive_text(value.strip())[:limit]
    duration = result.get("duration_seconds")
    if (
        isinstance(duration, (int, float))
        and not isinstance(duration, bool)
        and math.isfinite(float(duration))
        and 0 <= float(duration) <= 7 * 24 * 60 * 60
    ):
        context_result["duration_seconds"] = duration
    return {
        "device_id": device_id,
        "execution_id": execution_id,
        "task_name": task_name,
        "config_revision": config_revision,
        "result": context_result,
    }


def get_agent_control_plane() -> AgentControlPlane:
    """供应用生命周期和测试取得同一个控制面实例。"""
    return agent_control_plane


def require_admin(credentials: HTTPBasicCredentials | None = Depends(basic_auth)) -> None:
    """保护 Web UI 和管理 API，且不接受 Agent 共享密钥。"""
    if credentials is None or credentials.username != "admin":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="需要管理员 Basic Auth",
            headers={"WWW-Authenticate": "Basic"},
        )
    verify_admin_password(credentials.password)


def _agent_http_auth(secret: str | None = Header(default=None, alias=AGENT_SECRET_HEADER)) -> None:
    """校验 Agent 上传错误现场使用的 HTTP 请求头。"""
    verify_agent_secret(secret)


def _raise_control_error(exc: Exception) -> None:
    """把领域错误映射为稳定的 HTTP 状态，避免落入全局 500。"""
    if isinstance(exc, DeviceConfigNotFoundForHttp):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, (DeviceConfigConflictError, AgentBusyError)):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, (AgentOfflineError, AgentControlError, DeviceConfigError)):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise exc


class DeviceConfigNotFoundForHttp(DeviceConfigError):
    """HTTP 层使用的 404 标记类型。"""


def _translate_repository_error(exc: Exception) -> Exception:
    """避免让不存在设备在管理 API 中表现为配置格式错误。"""
    from .agent_device_repository import DeviceConfigNotFoundError

    if isinstance(exc, DeviceConfigNotFoundError):
        return DeviceConfigNotFoundForHttp(str(exc))
    return exc


@agent_app.get("/devices", dependencies=[Depends(require_admin)])
async def list_devices() -> list[dict[str, Any]]:
    """列出配置设备及其 Agent 在线、兼容和租约状态。"""
    return await agent_control_plane.snapshots()


@agent_app.post("/devices", dependencies=[Depends(require_admin)])
async def create_device(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """注册一台新设备；新设备必须从 revision=0 的 JSON 原子创建。"""
    try:
        config = payload.get("config", payload)
        if not isinstance(config, dict):
            raise DeviceConfigError("config 必须是 JSON object")
        device_id = str(config.get("device_id") or "")
        return await agent_control_plane.replace_config(device_id, config, expected_revision=None)
    except Exception as exc:
        _raise_control_error(_translate_repository_error(exc))


@agent_app.get("/devices/{device_id}", dependencies=[Depends(require_admin)])
async def get_device(device_id: str) -> dict[str, Any]:
    """读取一台设备的权威配置和运行态。"""
    try:
        return await agent_control_plane.snapshot(device_id)
    except Exception as exc:
        _raise_control_error(_translate_repository_error(exc))


@agent_app.get("/devices/{device_id}/config", dependencies=[Depends(require_admin)])
async def get_device_config(device_id: str) -> dict[str, Any]:
    """读取用于编辑的原始设备 JSON。"""
    try:
        return agent_repository.read(device_id)
    except Exception as exc:
        _raise_control_error(_translate_repository_error(exc))


@agent_app.put("/devices/{device_id}/config", dependencies=[Depends(require_admin)])
async def put_device_config(device_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """以 CAS 修订号原子保存设备 JSON。"""
    expected_revision = payload.get("expected_revision")
    config = payload.get("config")
    if not isinstance(config, dict):
        raise HTTPException(status_code=422, detail="config 必须是 JSON object")
    if expected_revision is not None and (isinstance(expected_revision, bool) or not isinstance(expected_revision, int)):
        raise HTTPException(status_code=422, detail="expected_revision 必须是整数或 null")
    try:
        return await agent_control_plane.replace_config(device_id, config, expected_revision)
    except Exception as exc:
        _raise_control_error(_translate_repository_error(exc))


@agent_app.post("/devices/{device_id}/execute", dependencies=[Depends(require_admin)])
async def execute_task(device_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """管理员手动为指定在线设备创建任务租约。"""
    task_name = str(payload.get("task_name") or "").strip()
    if not task_name:
        raise HTTPException(status_code=422, detail="task_name 不能为空")
    try:
        execution_id = await agent_control_plane.lease_task(device_id, task_name, requested_by="admin")
        return {"device_id": device_id, "task_name": task_name, "execution_id": execution_id}
    except Exception as exc:
        _raise_control_error(_translate_repository_error(exc))


@agent_app.post("/devices/{device_id}/stop", dependencies=[Depends(require_admin)])
async def stop_task(device_id: str) -> dict[str, Any]:
    """请求 Agent 在安全步骤后停止当前任务。"""
    try:
        execution_id = await agent_control_plane.request_stop(device_id)
        return {"device_id": device_id, "execution_id": execution_id, "requested": True}
    except Exception as exc:
        _raise_control_error(_translate_repository_error(exc))


@agent_app.post("/devices/{device_id}/recovery/resolve", dependencies=[Depends(require_admin)])
async def resolve_device_recovery(device_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """管理员确认旧 Agent 已停止后释放恢复阻塞租约。"""
    execution_id = str(payload.get("execution_id") or "").strip()
    if not execution_id:
        raise HTTPException(status_code=422, detail="execution_id 不能为空")
    try:
        resolved = await agent_control_plane.resolve_recovery(device_id, execution_id)
        return {"device_id": device_id, "execution_id": resolved, "resolved": True}
    except Exception as exc:
        _raise_control_error(_translate_repository_error(exc))


@agent_app.get("/devices/{device_id}/events", dependencies=[Depends(require_admin)])
async def get_device_control_events(device_id: str) -> dict[str, Any]:
    """查看 Server 控制面事件，不传输设备的正常任务日志或截图。"""
    try:
        return {"items": await agent_control_plane.control_events(device_id)}
    except Exception as exc:
        _raise_control_error(_translate_repository_error(exc))


@agent_app.get("/devices/{device_id}/errors", dependencies=[Depends(require_admin)])
async def get_device_error_bundles(device_id: str) -> dict[str, Any]:
    """列出指定设备上传到既有 log/error 目录的错误截图和日志入口。"""
    try:
        agent_repository.read(device_id)
        return log_browser_service.list_error_logs(script_name=device_id)
    except LogServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except Exception as exc:
        _raise_control_error(_translate_repository_error(exc))


def _get_device_error_detail(device_id: str, error_id: str) -> dict[str, Any]:
    """读取并校验错误目录确实属于所请求的 Agent 设备。"""
    agent_repository.read(device_id)
    detail = log_browser_service.get_error_detail(error_id)
    if detail["script_name"] != normalize_script_name(device_id):
        raise HTTPException(status_code=404, detail="错误现场不属于该设备")
    for image in detail["images"]:
        image["url"] = (
            f"/agents/devices/{device_id}/errors/{detail['id']}/images/{image['name']}"
        )
    return detail


@agent_app.get("/devices/{device_id}/errors/{error_id}", dependencies=[Depends(require_admin)])
async def get_device_error_detail(device_id: str, error_id: str) -> dict[str, Any]:
    """查看一台设备单个错误现场的日志与截图元数据。"""
    try:
        return _get_device_error_detail(device_id, error_id)
    except LogServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except Exception as exc:
        _raise_control_error(_translate_repository_error(exc))


@agent_app.get("/devices/{device_id}/errors/{error_id}/images/{image_name}", dependencies=[Depends(require_admin)])
async def get_device_error_image(device_id: str, error_id: str, image_name: str) -> FileResponse:
    """以管理员鉴权读取失败截图，不复用公开的普通日志图片路由。"""
    try:
        _get_device_error_detail(device_id, error_id)
        return FileResponse(str(log_browser_service.get_error_image_path(error_id, image_name)), media_type="image/png")
    except LogServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except Exception as exc:
        _raise_control_error(_translate_repository_error(exc))


@agent_app.get("/ui", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def agent_management_ui() -> HTMLResponse:
    """返回最小管理页；它复用同源 Basic Auth 调用管理 API。"""
    page = Path(__file__).resolve().parent / "web" / "agents" / "index.html"
    return HTMLResponse(page.read_text(encoding="utf-8"))


@agent_transport_app.websocket("/ws")
async def agent_websocket(websocket: WebSocket) -> None:
    """Agent 主动建立的出站 WSS/WebSocket 协议端点。"""
    await websocket.accept()
    device_id: str | None = None
    try:
        first = await websocket.receive_json()
        if not isinstance(first, dict) or first.get("type") != "hello":
            await websocket.send_json({"type": "error", "reason": "首条消息必须是 hello"})
            await websocket.close(code=1008)
            return
        verify_agent_secret(first.get("secret"))
        _reject_agent_wss_media_payload(first)
        ack = await agent_control_plane.register_hello(websocket, first)
        device_id = str(first.get("device_id") or "")
        if ack["accepted"]:
            await agent_control_plane.send_to_device(device_id, ack)
        else:
            await websocket.send_json({
                "sent_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "sequence": 0,
                **ack,
            })
        if not ack["accepted"]:
            await websocket.close(code=1008)
            return
        log_ws_event(f"agent[{device_id}] connected")

        while True:
            message = await websocket.receive_json()
            if not isinstance(message, dict):
                raise AgentControlError("Agent 消息必须是 JSON object")
            verify_agent_secret(message.get("secret"))
            _reject_agent_wss_media_payload(message)
            if message.get("device_id") != device_id:
                raise AgentControlError("Agent 消息 device_id 不匹配")
            await agent_control_plane.assert_session(device_id, websocket)
            await agent_control_plane.observe_agent_sequence(device_id, message)
            message_type = message.get("type")
            if message_type == "heartbeat":
                response = await agent_control_plane.heartbeat(device_id, message)
            elif message_type == "health_report":
                response = await agent_control_plane.report_health(device_id, message)
            elif message_type == "task_result":
                response = await agent_control_plane.report_result(device_id, message)
            elif message_type == "task_started":
                response = await agent_control_plane.acknowledge_task_started(device_id, message)
            elif message_type == "schedule_patch":
                try:
                    response = await agent_control_plane.apply_schedule_patch(device_id, message)
                except AgentControlError as exc:
                    # 管理员在任务执行期间保存新配置时，旧租约的补丁可能冲突。
                    # 不关闭连接，Agent 仍可用旧租约修订号提交最终 task_result。
                    response = await agent_control_plane.reject_schedule_patch(device_id, message, str(exc))
            elif message_type == "error_bundle":
                # HTTP 错误包落盘成功只是第一阶段；只有控制面确认当前租约、
                # 配置版本和 error_id 全部匹配时，Agent 才能推进到补丁/结果。
                await _restore_persisted_error_bundle_from_message(device_id, message)
                response = await agent_control_plane.acknowledge_error_bundle(device_id, message)
            else:
                raise AgentControlError(f"不支持的 Agent 消息类型: {message_type!r}")
            await agent_control_plane.send_to_device(device_id, response)
    except WebSocketDisconnect:
        log_ws_event(f"agent[{device_id or '?'}] disconnected", level="warning")
    except HTTPException as exc:
        log_ws_event(f"agent[{device_id or '?'}] authentication failed: {exc.detail}", level="warning")
        await websocket.close(code=1008)
    except (AgentControlError, DeviceConfigError, ValueError) as exc:
        log_ws_event(f"agent[{device_id or '?'}] protocol error: {exc}", level="warning")
        try:
            if device_id:
                await agent_control_plane.send_to_device(device_id, {"type": "error", "reason": str(exc)})
            else:
                await websocket.send_json({
                    "type": "error",
                    "reason": str(exc),
                    "sent_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "sequence": 0,
                })
        except Exception:
            pass
        await websocket.close(code=1008)
    finally:
        await agent_control_plane.unregister(websocket, device_id)


@agent_transport_app.post("/error-bundles/{device_id}/{execution_id}")
async def upload_error_bundle(
    device_id: str,
    execution_id: str,
    metadata: str = Form("{}"),
    files: list[UploadFile] = File(...),
    _: None = Depends(_agent_http_auth),
) -> dict[str, Any]:
    """保存失败截图、任务日志和脱敏上下文，不接收正常运行截图。"""
    if not files:
        raise HTTPException(status_code=422, detail="错误包至少需要一个文件")
    if len(metadata.encode("utf-8", errors="replace")) > MAX_ERROR_METADATA_BYTES:
        raise HTTPException(status_code=413, detail="错误包 metadata 过大")
    try:
        metadata_value = json.loads(metadata)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"metadata 不是合法 JSON: {exc}") from exc
    if not isinstance(metadata_value, dict):
        raise HTTPException(status_code=422, detail="metadata 必须是 JSON object")
    # 仅允许失败结果上传现场，避免正常截图误入 Server。
    result = metadata_value.get("result")
    if not isinstance(result, dict) or result.get("status") not in {"failure", "manual_required"}:
        raise HTTPException(status_code=422, detail="错误包必须关联 failure 或 manual_required 任务结果")
    if metadata_value.get("device_id") != device_id:
        raise HTTPException(status_code=422, detail="错误包 metadata.device_id 与路径不一致")
    if metadata_value.get("execution_id") != execution_id:
        raise HTTPException(status_code=422, detail="错误包 metadata.execution_id 与路径不一致")
    task_name = metadata_value.get("task_name")
    if not isinstance(task_name, str) or not task_name.strip():
        raise HTTPException(status_code=422, detail="错误包 metadata.task_name 必须是非空字符串")
    config_revision = metadata_value.get("config_revision")
    if isinstance(config_revision, bool) or not isinstance(config_revision, int):
        raise HTTPException(status_code=422, detail="错误包 metadata.config_revision 必须是整数")

    try:
        config = agent_repository.read(device_id)
        # Server 重启后，内存中的错误包去重表会丢失。先从同设备历史目录中
        # 找到与当前租约完全匹配的权威 context.json，随后 reserve 会幂等返回
        # 原目录而不会再次写入截图。
        await _restore_persisted_error_bundle(
            device_id,
            execution_id,
            config_revision,
            task_name,
        )
        # 错误包在 Agent 回传 task_result 前上传。除共享密钥外，还必须绑定
        # 当前设备的有效租约，防止其它执行编号写入截图或本地任务日志。
        authorized_task_name, existing_error_id = await agent_control_plane.reserve_error_bundle_upload(
            device_id,
            execution_id,
            config_revision,
        )
    except Exception as exc:
        _raise_control_error(_translate_repository_error(exc))
    if task_name != authorized_task_name:
        await agent_control_plane.abandon_error_bundle_upload(device_id, execution_id)
        raise HTTPException(status_code=422, detail="错误包 metadata.task_name 与当前任务租约不一致")
    if not execution_id.strip():
        await agent_control_plane.abandon_error_bundle_upload(device_id, execution_id)
        raise HTTPException(status_code=422, detail="execution_id 不能为空")
    if existing_error_id is not None:
        # 上一次请求已把现场完整落盘但 HTTP 响应可能在网络层丢失。按同一租约
        # 重试时返回原错误目录，避免创建重复截图和日志目录。
        return {
            "device_id": device_id,
            "execution_id": execution_id,
            "error_id": existing_error_id,
            "files": [],
            "duplicate": True,
        }

    folder = ERROR_LOG_ROOT / build_error_log_dir_name(config["device_id"], int(time.time() * 1000))
    total_size = 0
    saved: list[str] = []
    has_task_context = False
    has_task_log = False
    try:
        folder.mkdir(parents=True, exist_ok=False)
        # context.json 是 Server 根据已验证租约写入的权威元数据，Agent 同名
        # 文件另存为 task-context.json，保留 OAS 细节但绝不允许覆盖租约上下文。
        context = _authoritative_error_context(
            metadata_value,
            device_id,
            execution_id,
            authorized_task_name,
            config_revision,
        )
        (folder / "context.json").write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")
        for index, upload in enumerate(files):
            original_name = _validate_error_bundle_filename(upload.filename)
            suffix = Path(original_name).suffix.lower()
            if suffix not in {".png", ".txt", ".json"}:
                raise HTTPException(status_code=422, detail=f"不允许错误包文件类型: {original_name}")
            content = await upload.read(MAX_ERROR_FILE_BYTES + 1)
            if len(content) > MAX_ERROR_FILE_BYTES:
                raise HTTPException(status_code=413, detail=f"错误包文件过大: {original_name}")
            # 即使 Agent 已执行本地脱敏，Server 仍必须在落盘前清洗所有非图片
            # 文件，不能让自定义 Agent 或损坏 JSON 绕过错误现场的隐私边界。
            if suffix == ".txt":
                content = _sanitize_error_log_bytes(content)
            elif suffix == ".json":
                content = _sanitize_error_json_bytes(content)
            if len(content) > MAX_ERROR_FILE_BYTES:
                raise HTTPException(status_code=413, detail=f"错误包清洗后过大: {original_name}")
            total_size += len(content)
            if total_size > MAX_ERROR_BUNDLE_BYTES:
                raise HTTPException(status_code=413, detail="错误包总大小超过限制")
            # Agent 的 context.json 只作为 OAS 细节上下文展示；重复文件直接拒绝，
            # 防止一个错误包内出现不确定的任务上下文。
            if original_name == "context.json":
                if has_task_context:
                    raise HTTPException(status_code=422, detail="错误包只能包含一个 context.json")
                target_name = "task-context.json"
                has_task_context = True
            else:
                if original_name == "log.txt":
                    if has_task_log:
                        raise HTTPException(status_code=422, detail="错误包只能包含一个 log.txt")
                    target_name = "log.txt"
                    has_task_log = True
                else:
                    target_name = f"{index:02d}_{original_name}"
            (folder / target_name).write_bytes(content)
            saved.append(target_name)
    except Exception:
        if folder.exists():
            for child in folder.glob("*"):
                child.unlink(missing_ok=True)
            folder.rmdir()
        await agent_control_plane.abandon_error_bundle_upload(device_id, execution_id)
        raise
    # 控制面只记录错误包编号，错误图片和任务日志仍由错误现场接口按需读取。
    await agent_control_plane.complete_error_bundle_upload(device_id, execution_id, folder.name)
    return {
        "device_id": device_id,
        "execution_id": execution_id,
        "error_id": folder.name,
        "files": saved,
        "duplicate": False,
    }


def _read_persisted_error_context(error_id: str) -> dict[str, Any] | None:
    """读取一个受路径和大小限制的 Server 权威错误上下文。"""
    if Path(error_id).name != error_id:
        return None
    try:
        path = ensure_child_path(ERROR_LOG_ROOT, ERROR_LOG_ROOT / error_id)
        context_path = ensure_child_path(path, path / "context.json")
        if not context_path.is_file() or context_path.stat().st_size > MAX_PERSISTED_ERROR_CONTEXT_BYTES:
            return None
        value = json.loads(context_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError, LogServiceError):
        return None
    return value if isinstance(value, dict) else None


def _find_persisted_error_bundle(
    device_id: str,
    execution_id: str,
    config_revision: int,
    task_name: str | None = None,
    error_id: str | None = None,
) -> tuple[str, str] | None:
    """按设备和执行上下文查找已落盘错误目录，最多读取有限候选。"""
    if error_id:
        candidates = [ERROR_LOG_ROOT / error_id]
    else:
        try:
            # device_id 已由设备配置校验为字母、数字和连字符；glob 只定位
            # 当前设备前缀，不遍历不相关设备的全部错误现场。
            candidates = sorted(ERROR_LOG_ROOT.glob(f"{device_id}_*"), reverse=True)[:128]
        except OSError:
            return None
    for candidate in candidates:
        candidate_id = candidate.name
        context = _read_persisted_error_context(candidate_id)
        if context is None:
            continue
        if (
            context.get("device_id") != device_id
            or context.get("execution_id") != execution_id
            or context.get("config_revision") != config_revision
        ):
            continue
        context_task_name = context.get("task_name")
        if not isinstance(context_task_name, str) or not context_task_name.strip():
            continue
        if task_name is not None and context_task_name != task_name:
            continue
        return candidate_id, context_task_name
    return None


async def _restore_persisted_error_bundle(
    device_id: str,
    execution_id: str,
    config_revision: int,
    task_name: str | None,
    error_id: str | None = None,
) -> None:
    """将磁盘中已验证的错误目录登记回当前控制面运行态。"""
    found = _find_persisted_error_bundle(device_id, execution_id, config_revision, task_name, error_id)
    if found is None:
        return
    await agent_control_plane.restore_error_bundle_upload(
        device_id,
        execution_id,
        config_revision,
        found[1],
        found[0],
    )


async def _restore_persisted_error_bundle_from_message(device_id: str, message: dict[str, Any]) -> None:
    """WSS 错误包通知在 Server 重启后按 error_id 恢复其磁盘关联。"""
    execution_id = str(message.get("execution_id") or "").strip()
    error_id = str(message.get("error_id") or "").strip()
    revision = message.get("config_revision")
    if not execution_id or not error_id or isinstance(revision, bool) or not isinstance(revision, int):
        return
    await _restore_persisted_error_bundle(device_id, execution_id, revision, None, error_id)
