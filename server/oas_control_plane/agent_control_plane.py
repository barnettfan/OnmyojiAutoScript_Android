"""Android Agent 的内存运行态、租约和命令派发。

持久化配置由 :mod:`agent_device_repository` 管理；本模块只保存可由
Agent 重连重新建立的 WebSocket、心跳和任务租约状态。
"""
from __future__ import annotations

import asyncio
import copy
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .agent_device_repository import (
    DeviceConfigConflictError,
    DeviceConfigError,
    DeviceConfigNotFoundError,
    DeviceConfigRepository,
    TASK_NAME_PATTERN,
    version_at_least,
)
from .sensitive import handle_sensitive_text


PROTOCOL_VERSION = 1
# 管理页只保留有限的 Server 控制面事件；任务过程日志始终只保留在设备上。
CONTROL_EVENT_LIMIT = 100


class AgentControlError(ValueError):
    """Agent 协议字段或当前运行态不满足要求。"""


class AgentBusyError(AgentControlError):
    """设备已有未完成的任务租约。"""


class AgentOfflineError(AgentControlError):
    """设备目前没有 Agent WebSocket。"""


@dataclass
class TaskLease:
    """一个不可重用的、绑定单设备的任务执行租约。"""

    execution_id: str
    task_name: str
    config_revision: int
    expires_at: datetime
    requested_by: str
    # Agent 已持久化执行态并启动本地任务子进程后的控制面确认时间。
    started_at: datetime | None = None
    stop_requested: bool = False
    expired: bool = False

    def payload(self, device_id: str, config: dict[str, Any]) -> dict[str, Any]:
        """构造 Agent 先校验再缓存的 task_lease 消息。"""
        # 租约跨 Server 与 redroid 传输，不能使用无时区的本地墙上时间。Server
        # 内部仍按既有本地时间管理到期调度；这里只把同一绝对时刻编码为 UTC，
        # 防止容器时区与 Server 不同时把刚签发的租约误判为已过期。
        expires_at_utc = self.expires_at.astimezone(timezone.utc)
        return {
            "type": "task_lease",
            "device_id": device_id,
            "execution_id": self.execution_id,
            "task_name": self.task_name,
            "config_revision": self.config_revision,
            "expires_at": expires_at_utc.isoformat(timespec="seconds"),
            "config": copy.deepcopy(config),
        }


@dataclass
class DeviceRuntime:
    """不可持久化的在线设备状态。"""

    device_id: str
    websocket: Any | None = None
    agent_version: str = ""
    resource_version: str = ""
    # Agent 最近确认并缓存的设备 JSON 修订号，仅用于管理页观察同步状态。
    confirmed_config_revision: int | None = None
    capabilities: list[str] = field(default_factory=list)
    task_manifest: dict[str, str] = field(default_factory=dict)
    health: dict[str, Any] = field(default_factory=lambda: {"status": "initializing"})
    compatible: bool = False
    incompatibility_reason: str | None = None
    # 已启动租约但新连接没有报告同一 execution_id 时，禁止 Server 把任务重放
    # 给另一份 Agent；管理员需要先确认旧执行面的真实状态，避免重复触控。
    recovery_reason: str | None = None
    last_heartbeat: datetime | None = None
    foreground_package: str | None = None
    current_task: str | None = None
    lease: TaskLease | None = None
    last_result: dict[str, Any] | None = None
    # 仅记录 Server 自己观察到的连接、租约和结果元数据，不写入 Agent 的
    # 正常任务日志、截图或识别结果。
    control_events: list[dict[str, Any]] = field(default_factory=list)
    completed_execution_ids: set[str] = field(default_factory=set)
    # HTTP 错误包可能在 Server 已落盘、但响应丢失后被 Agent 重传。按当前
    # execution_id 保存已完成目录并临时标记上传中，避免为同一失败创建多份
    # 截图目录；它们和租约一样都是单实例内存运行态。
    error_bundle_ids: dict[str, str] = field(default_factory=dict)
    error_bundle_uploading: set[str] = field(default_factory=set)
    # 只有 Agent 用 WSS 确认看见 HTTP 已落盘的错误包后，才允许它携带该
    # error_id 提交最终结果。该状态防止跳过错误包通知直接释放任务租约。
    acknowledged_error_bundle_ids: dict[str, str] = field(default_factory=dict)
    last_agent_sequence: int = -1
    server_sequence: int = 0
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

class AgentControlPlane:
    """单 Server 实例中的 Agent 注册表和单设备单租约调度器。"""

    def __init__(
        self,
        repository: DeviceConfigRepository,
        lease_seconds: int = 180,
        heartbeat_timeout_seconds: int = 45,
    ) -> None:
        """初始化单实例控制面，并定义连接失效的心跳容忍时间。"""
        if heartbeat_timeout_seconds < 1:
            raise ValueError("heartbeat_timeout_seconds 必须大于 0")
        self.repository = repository
        self.lease_seconds = lease_seconds
        # WebSocket 未关闭不代表 Agent 仍在工作；调度前必须同时检查心跳新鲜度。
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self._devices: dict[str, DeviceRuntime] = {}
        self._lock = asyncio.Lock()

    async def replace_config(
        self,
        device_id: str,
        data: dict[str, Any],
        expected_revision: int | None,
    ) -> dict[str, Any]:
        """串行化管理员配置写入与租约快照，避免发送过期配置。"""
        async with self._lock:
            config = self.repository.replace(device_id, data, expected_revision)
            runtime = self._devices.get(device_id)
            if runtime is not None:
                # 管理员可能修改分辨率、游戏包或运行时参数。即使 Agent 仍在线，
                # 也必须等待它确认新快照并重新预检，不能沿用旧配置的 healthy。
                runtime.health = {
                    "status": "initializing",
                    "reason": "等待 Agent 确认新设备配置并重新预检",
                    "checks": {},
                }
                self._record_event_locked(
                    runtime,
                    "config_updated",
                    config_revision=config["revision"],
                )
            return config

    async def register_hello(self, websocket: Any, message: dict[str, Any]) -> dict[str, Any]:
        """注册 hello，并返回配置快照或明确的不兼容原因。"""
        self._require_protocol(message)
        self._validate_message_envelope(message, label="hello")
        device_id = self._message_device_id(message)
        async with self._lock:
            try:
                config = self.repository.read(device_id)
            except DeviceConfigError as exc:
                return {
                    "type": "hello_ack",
                    "device_id": device_id,
                    "accepted": False,
                    "reason": str(exc),
                    "protocol_version": PROTOCOL_VERSION,
                }

            agent_version = str(message.get("agent_version") or "").strip()
            required_version = config["required_agent_version"]
            raw_capabilities = message.get("capabilities") or []
            capabilities = [str(item) for item in raw_capabilities if isinstance(item, str)]
            compatibility_reasons: list[str] = []
            if agent_version != required_version:
                compatibility_reasons.append(
                    f"agent_version={agent_version!r}, required={required_version!r}"
                )
            # 首版设备 JSON 仅允许 root_shell。不能仅相信 Agent 随后的 health
            # 文本字段；错误镜像或旧版客户端缺少该后端时必须在 hello 阶段拒绝。
            if config["device"]["transport"] == "root_shell" and "root_shell" not in capabilities:
                compatibility_reasons.append("Agent 未声明 root_shell capability")
            compatible = not compatibility_reasons
            reason = "; ".join(compatibility_reasons) or None
            runtime = self._devices.get(device_id)
            # 一个 device_id 只能由一个活跃 Agent 持有。若新连接直接覆盖仍有
            # 新鲜心跳的旧会话，旧 Agent 的任务子进程仍会继续操作设备，而新会话
            # 可能重放同一租约，最终造成两套触控并发。连接陈旧后才允许接管，
            # 此时旧会话的后续上报会被 assert_session 拒绝。
            if runtime is not None and runtime.websocket is not websocket and self._is_online(runtime):
                self._record_event_locked(runtime, "connection_rejected", reason="已有健康 Agent 会话")
                return {
                    "type": "hello_ack",
                    "device_id": device_id,
                    "accepted": False,
                    "reason": "该 device_id 已有健康的 Agent WSS 会话，拒绝并发连接",
                    "protocol_version": PROTOCOL_VERSION,
                }
            if runtime is None:
                runtime = DeviceRuntime(device_id=device_id)
                self._devices[device_id] = runtime
            runtime.websocket = websocket
            runtime.agent_version = agent_version
            runtime.resource_version = str(message.get("resource_version") or "")
            runtime.confirmed_config_revision = self._cached_config_revision(message)
            runtime.capabilities = capabilities
            runtime.task_manifest = self._parse_task_manifest(message.get("task_manifest"))
            # hello 携带的健康状态可能是上一次连接残留，不能绕过本次
            # Agent 本地 Root/OCR/分辨率预检；必须等待本会话的 health_report。
            runtime.health = {"status": "initializing", "reason": "等待本次连接的 health_report", "checks": {}}
            runtime.compatible = compatible
            runtime.incompatibility_reason = reason
            runtime.recovery_reason = None
            runtime.last_heartbeat = datetime.now()
            runtime.foreground_package = self._optional_string(message.get("foreground_package"))
            runtime.last_agent_sequence = message["sequence"]
            runtime.server_sequence = 0
            self._record_event_locked(
                runtime,
                "connected",
                agent_version=agent_version,
                resource_version=runtime.resource_version,
            )
            reported_execution_id = str(message.get("current_execution_id") or "")
            reported_task_name = str(message.get("current_task_name") or "")
            reported_revision = message.get("current_config_revision")
            if (
                compatible
                and runtime.lease is None
                and reported_execution_id
                # 任务可能在 Server 重启期间被管理员从新配置中删除；
                # 仍需恢复旧 execution_id 以接收最终结果并释放租约，不能
                # 因为当前配置已无该任务而让 Agent outbox 永久重试。
                and TASK_NAME_PATTERN.fullmatch(reported_task_name)
                and isinstance(reported_revision, int)
                and not isinstance(reported_revision, bool)
                and reported_revision > 0
            ):
                # Server 进程重启会丢失内存租约。可信 Agent 在 hello 中报告
                # 仍在执行（或本地 outbox 待回传）的编号时，仅恢复该编号，
                # 让它上报结果/补丁，绝不据此重复启动任务。
                runtime.lease = TaskLease(
                    execution_id=reported_execution_id,
                    task_name=reported_task_name,
                    config_revision=reported_revision,
                    expires_at=self._lease_expiry(),
                    requested_by="agent-recovery",
                    # current_execution_id 只会在 Agent 持久化 start_task 后写入；
                    # Server 重启恢复该执行态时可安全标为已启动。
                    started_at=datetime.now(),
                )
            if runtime.lease and reported_execution_id == runtime.lease.execution_id:
                runtime.current_task = runtime.lease.task_name
                runtime.lease.expires_at = self._lease_expiry()
                runtime.lease.expired = False
                runtime.recovery_reason = None
            elif runtime.lease and reported_execution_id:
                # 新连接报告了另一个执行编号。无论 Server 租约是否已经收到
                # task_started，都不能猜测哪一边仍在触控，更不能重放旧命令；
                # 显式进入人工恢复状态，让管理员核验设备后释放权威租约。
                runtime.recovery_reason = (
                    "当前租约与 Agent execution_id 不一致，已阻止自动重放；"
                    "请确认旧 Agent 后再处理该租约"
                )
                self._record_event_locked(
                    runtime,
                    "recovery_blocked",
                    execution_id=runtime.lease.execution_id,
                    task_name=runtime.lease.task_name,
                    reported_execution_id=reported_execution_id,
                )
            elif not reported_execution_id and runtime.lease is None:
                runtime.current_task = None

            # 旧连接断在 start_task 之前时，Agent 的持久化状态仍为空；在
            # hello_ack 内重放同一租约是安全的，并避免这类租约永久悬挂。
            resume_lease = None
            resume_start = None
            if (
                compatible
                and runtime.lease
                and not reported_execution_id
                and runtime.lease.started_at is None
                and runtime.recovery_reason is None
            ):
                resume_lease = runtime.lease.payload(device_id, config)
                resume_start = {
                    "type": "start_task",
                    "device_id": device_id,
                    "execution_id": runtime.lease.execution_id,
                    "task_name": runtime.lease.task_name,
                    "config_revision": runtime.lease.config_revision,
                }
            elif compatible and runtime.lease and not reported_execution_id and runtime.lease.started_at is not None:
                # task_started 已确认任务进入设备执行面。若新 hello 没有同一
                # execution_id，重放 start_task 会让两个执行面同时触控；保留
                # 原租约等待人工确认，而不是把同一任务交给新连接。
                runtime.recovery_reason = (
                    "已启动租约缺少 Agent execution 状态，已阻止自动重放；"
                    "请确认旧 Agent 后再处理该租约"
                )
                self._record_event_locked(
                    runtime,
                    "recovery_blocked",
                    execution_id=runtime.lease.execution_id,
                    task_name=runtime.lease.task_name,
                )

        response = {
            "type": "hello_ack",
            "device_id": device_id,
            "accepted": compatible,
            "reason": reason,
            "protocol_version": PROTOCOL_VERSION,
            "config_revision": config["revision"],
            "config": config,
        }
        if resume_lease is not None:
            response["resume_lease"] = resume_lease
            response["resume_start"] = resume_start
        return response

    async def unregister(self, websocket: Any, device_id: str | None) -> None:
        """断线仅标离线；已有租约保留，避免任务被重复派发。"""
        if not device_id:
            return
        async with self._lock:
            runtime = self._devices.get(device_id)
            if runtime and runtime.websocket is websocket:
                runtime.websocket = None
                self._record_event_locked(runtime, "disconnected")

    async def assert_session(self, device_id: str, websocket: Any) -> None:
        """拒绝已被新连接替换的旧 WebSocket 继续上报状态。"""
        async with self._lock:
            runtime = self._devices.get(device_id)
            if runtime is None or runtime.websocket is not websocket:
                raise AgentOfflineError("Agent 连接已被替换或尚未完成 hello")

    async def observe_agent_sequence(self, device_id: str, message: dict[str, Any]) -> None:
        """拒绝同一连接中乱序或重放的 Agent 消息。"""
        sequence = message.get("sequence")
        sent_at = message.get("sent_at")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise AgentControlError("Agent 消息 sequence 必须是非负整数")
        if not isinstance(sent_at, str) or not sent_at.strip():
            raise AgentControlError("Agent 消息 sent_at 必须是非空字符串")
        async with self._lock:
            runtime = self._devices.get(device_id)
            if runtime is None or runtime.websocket is None:
                raise AgentOfflineError(f"设备 {device_id} 未完成 hello")
            if sequence <= runtime.last_agent_sequence:
                raise AgentControlError("Agent 消息 sequence 必须严格递增")
            runtime.last_agent_sequence = sequence

    async def send_to_device(self, device_id: str, payload: dict[str, Any]) -> None:
        """通过当前会话发送含公共协议字段的 Server 消息。"""
        runtime = await self._runtime_for_session(device_id)
        await self._send(runtime, payload)

    async def heartbeat(self, device_id: str, message: dict[str, Any]) -> dict[str, Any]:
        """记录在线/前台状态，并为同一 execution_id 的运行任务续租。"""
        runtime = await self._runtime_for_session(device_id)
        execution_id = str(message.get("execution_id") or "")
        cached_revision = self._cached_config_revision(message)
        # 借用 heartbeat ACK 下发新快照，令在线 Agent 无需重连即可同步配置。
        config = self.repository.read(device_id)
        async with self._lock:
            runtime.last_heartbeat = datetime.now()
            runtime.foreground_package = self._optional_string(message.get("foreground_package"))
            runtime.confirmed_config_revision = cached_revision
            # 只有 ``health_report`` 带有经过版本校验的配置 revision，才可改变
            # 可派发状态。管理员保存新配置后，旧 Agent 的周期 heartbeat 仍可能
            # 携带上一次的 healthy；若在这里覆盖 initializing，会绕过重新预检。
            if runtime.lease and execution_id == runtime.lease.execution_id:
                runtime.lease.expires_at = self._lease_expiry()
                runtime.lease.expired = False
                runtime.current_task = runtime.lease.task_name
            elif not execution_id and runtime.lease is None:
                runtime.current_task = None
        response = {
            "type": "heartbeat_ack",
            "device_id": device_id,
            "server_time": datetime.now().isoformat(),
            "config_revision": config["revision"],
        }
        if cached_revision != config["revision"]:
            response["config"] = config
        return response

    async def report_health(self, device_id: str, message: dict[str, Any]) -> dict[str, Any]:
        """记录 Agent Root/分辨率/游戏包预检结果，作为派发任务前置条件。"""
        runtime = await self._runtime_for_session(device_id)
        health = self._normalize_health(message.get("health"))
        reported_revision = message.get("config_revision")
        # 预检结论必须绑定 Agent 实际加载的 Server 快照。缺少 revision 时，
        # 管理员更新设备配置后的旧预检可能被误认为仍然有效。
        if (
            isinstance(reported_revision, bool)
            or not isinstance(reported_revision, int)
            or reported_revision < 1
        ):
            raise AgentControlError("health_report.config_revision 必须是正整数")
        config = self.repository.read(device_id)
        if reported_revision != config["revision"]:
            health = {
                "status": "initializing",
                "reason": "Agent 预检使用的配置版本不是当前 Server revision",
                "checks": {},
            }
        async with self._lock:
            runtime.last_heartbeat = datetime.now()
            runtime.health = health
            # Agent 只会在成功保存 hello/heartbeat 返回的快照后，才对该快照
            # 执行本机预检并发送 health_report。当前 Server revision 匹配时，
            # 这份报告同时证明该 revision 已被 Agent 确认；不能再等待下一次
            # heartbeat，否则管理页会在最多一个心跳周期内错误显示“未确认”。
            if reported_revision == config["revision"]:
                runtime.confirmed_config_revision = reported_revision
            self._record_event_locked(runtime, "health_report", status=health["status"])
        return {"type": "health_report_ack", "device_id": device_id, "health": health}

    async def report_result(self, device_id: str, message: dict[str, Any]) -> dict[str, Any]:
        """接受一次任务结果；相同 execution_id 的重传不会二次改写状态。"""
        runtime = await self._runtime_for_session(device_id)
        execution_id = self._execution_id(message)
        config_revision = message.get("config_revision")
        if isinstance(config_revision, bool) or not isinstance(config_revision, int):
            raise AgentControlError("task_result.config_revision 必须是整数")
        status = str(message.get("status") or "").strip()
        if status not in {"success", "failure", "cancelled", "manual_required", "rejected"}:
            raise AgentControlError("task_result.status 不合法")
        duration_seconds = self._result_duration(message.get("duration_seconds"))
        error_id = self._optional_string(message.get("error_id"))
        cached_revision = self._cached_config_revision(message)
        async with self._lock:
            if execution_id in runtime.completed_execution_ids:
                return {
                    "type": "task_result_ack",
                    "device_id": device_id,
                    "execution_id": execution_id,
                    "config_revision": config_revision,
                    "duplicate": True,
                }
            if runtime.lease is None or runtime.lease.execution_id != execution_id:
                raise AgentControlError("task_result 不属于当前租约")
            if runtime.lease.config_revision != config_revision:
                raise AgentControlError("task_result 使用的配置修订号不是当前租约版本")
            # 必须在控制面锁内读取权威 revision；否则管理员并发保存配置时，
            # 锁外的旧读取可能让结果回报把管理页的已确认版本写回旧值。
            current_config = self.repository.read(device_id)
            uploaded_error_id = runtime.error_bundle_ids.get(execution_id)
            acknowledged_error_id = runtime.acknowledged_error_bundle_ids.get(execution_id)
            # 错误包是失败现场的唯一外传通道。若 Agent 已成功落盘错误包，就
            # 必须先通过绑定的 WSS 通知确认该目录，再把相同编号写进结果，不能
            # 让丢失 ACK 的请求静默释放租约并留下未关联的错误现场。
            if status in {"failure", "manual_required"}:
                # Agent 对所有失败路径都会生成 error_bundle outbox；即使 OAS
                # 尚未写出截图目录，也会上传当前任务的脱敏日志尾部。因此缺少
                # error_id 不应被当作可完成结果，否则 Server 会先释放租约而
                # 排错证据永远无法再绑定回该执行。
                if error_id is None:
                    raise AgentControlError("failure 或 manual_required 任务结果必须关联已确认错误包")
                if uploaded_error_id is None:
                    raise AgentControlError("task_result.error_id 未关联当前任务的错误包")
                if error_id != uploaded_error_id:
                    raise AgentControlError("task_result.error_id 与已上传错误包不一致")
                if acknowledged_error_id != uploaded_error_id:
                    raise AgentControlError("错误包尚未收到 Server 确认，不能提交 task_result")
            elif error_id is not None:
                raise AgentControlError("task_result.error_id 未关联当前任务的错误包")
            runtime.last_result = {
                "execution_id": execution_id,
                "task_name": runtime.lease.task_name,
                "status": status,
                # 管理页会直接展示该字段；即使 Agent 任务异常没有成功生成错误包，
                # 也不能把容器内项目路径或无限长度异常文本写入 Server 运行态。
                "summary": self._result_summary(message.get("summary")),
                "started_at": self._optional_string(message.get("started_at")),
                "finished_at": datetime.now().isoformat(timespec="seconds"),
                "agent_finished_at": self._optional_string(message.get("finished_at")),
                "duration_seconds": duration_seconds,
                "error_id": error_id,
            }
            # schedule_patch ACK 后 Agent 会先保存新快照再提交结果。仅在与
            # Server 当前 revision 相等时，立即确认管理页中的缓存版本，避免
            # 等待下一次 heartbeat；旧 Agent 不能借此把版本状态回退。
            if cached_revision == current_config["revision"]:
                runtime.confirmed_config_revision = cached_revision
            self._record_event_locked(
                runtime,
                "task_result",
                execution_id=execution_id,
                task_name=runtime.lease.task_name,
                status=status,
            )
            runtime.completed_execution_ids.add(execution_id)
            # 有界去重集合，防止常驻服务无限增长。
            if len(runtime.completed_execution_ids) > 256:
                runtime.completed_execution_ids = set(list(runtime.completed_execution_ids)[-128:])
            # 最终结果已确认后，错误包只需要保留在错误目录和 last_result 中；
            # 清理会话态关联避免长期运行的 Server 逐次累积执行编号。
            runtime.error_bundle_ids.pop(execution_id, None)
            runtime.acknowledged_error_bundle_ids.pop(execution_id, None)
            runtime.error_bundle_uploading.discard(execution_id)
            runtime.lease = None
            runtime.current_task = None
        return {
            "type": "task_result_ack",
            "device_id": device_id,
            "execution_id": execution_id,
            "config_revision": config_revision,
            "duplicate": False,
        }

    async def acknowledge_task_started(self, device_id: str, message: dict[str, Any]) -> dict[str, Any]:
        """确认 Agent 已持久化并启动指定租约，重复确认保持幂等。"""
        runtime = await self._runtime_for_session(device_id)
        execution_id = self._execution_id(message)
        config_revision = message.get("config_revision")
        if isinstance(config_revision, bool) or not isinstance(config_revision, int):
            raise AgentControlError("task_started.config_revision 必须是整数")
        async with self._lock:
            lease = runtime.lease
            if lease is None or lease.execution_id != execution_id:
                raise AgentControlError("task_started 不属于当前租约")
            if lease.config_revision != config_revision:
                raise AgentControlError("task_started 使用的配置修订号不是当前租约版本")
            reported_task_name = self._optional_string(message.get("task_name"))
            if reported_task_name is not None and reported_task_name != lease.task_name:
                raise AgentControlError("task_started.task_name 与当前租约不一致")
            duplicate = lease.started_at is not None
            if not duplicate:
                lease.started_at = datetime.now()
                lease.expires_at = self._lease_expiry()
                lease.expired = False
                self._record_event_locked(
                    runtime,
                    "task_started",
                    execution_id=execution_id,
                    task_name=lease.task_name,
                )
        return {
            "type": "task_started_ack",
            "device_id": device_id,
            "execution_id": execution_id,
            "config_revision": config_revision,
            "duplicate": duplicate,
        }

    async def apply_schedule_patch(self, device_id: str, message: dict[str, Any]) -> dict[str, Any]:
        """只允许当前租约把本地任务状态机产生的调度变化提交回 Server。"""
        runtime = await self._runtime_for_session(device_id)
        execution_id = self._execution_id(message)
        config_revision = message.get("config_revision")
        if isinstance(config_revision, bool) or not isinstance(config_revision, int):
            raise AgentControlError("schedule_patch.config_revision 必须是整数")
        async with self._lock:
            if runtime.lease is None or runtime.lease.execution_id != execution_id:
                raise AgentControlError("schedule_patch 不属于当前租约")
            if runtime.lease.config_revision != config_revision:
                raise AgentControlError("schedule_patch 使用的配置修订号不是当前租约版本")
            try:
                # 租约校验与配置落盘必须处于同一把控制面锁中。否则 task_result
                # 可在两者之间释放租约，过期 Agent 仍可能覆盖管理员的新调度。
                config = self.repository.apply_schedule_patch(device_id, message.get("patch"), config_revision)
            except (DeviceConfigError, DeviceConfigConflictError) as exc:
                raise AgentControlError(str(exc)) from exc
            self._record_event_locked(
                runtime,
                "schedule_patch_applied",
                execution_id=execution_id,
                config_revision=config["revision"],
            )
        return {
            "type": "schedule_patch_ack",
            "device_id": device_id,
            "execution_id": execution_id,
            "config_revision": config["revision"],
            "config": config,
        }

    async def reject_schedule_patch(self, device_id: str, message: dict[str, Any], reason: str) -> dict[str, Any]:
        """回传补丁拒绝及最新快照，让 Agent 继续提交原租约的任务结果。"""
        execution_id = self._execution_id(message)
        config = self.repository.read(device_id)
        return {
            "type": "schedule_patch_ack",
            "device_id": device_id,
            "execution_id": execution_id,
            "accepted": False,
            "reason": reason,
            "config_revision": config["revision"],
            "config": config,
        }

    async def dispatch_due_tasks(self) -> None:
        """为每台空闲、兼容且在线的设备派发一个可执行的到期任务。

        配置可早于镜像发布，因此第一个到期任务可能暂未在当前 Agent 清单中。
        这种情形不能阻止同一设备的其它已验证任务；仅跳过不能租约的候选项。
        """
        for device_id in self.repository.list_ids():
            try:
                task_names = self.repository.due_tasks(device_id)
            except DeviceConfigError:
                continue
            for task_name in task_names:
                try:
                    await self.lease_task(device_id, task_name, requested_by="scheduler")
                    break
                except (AgentBusyError, AgentOfflineError):
                    # 同机只能有一个租约；离线也不能再尝试后续任务。
                    break
                except AgentControlError:
                    # 单项任务不受当前固定镜像支持时，继续检查同一候选队列。
                    continue

    async def lease_task(self, device_id: str, task_name: str, requested_by: str) -> str:
        """创建租约并依次下发 ``task_lease`` 与 ``start_task``。"""
        async with self._lock:
            config = self.repository.read(device_id)
            if task_name not in config["tasks"]:
                raise AgentControlError(f"设备 {device_id} 未配置任务 {task_name}")
            runtime = self._devices.get(device_id)
            if not self._is_online(runtime):
                raise AgentOfflineError(f"设备 {device_id} 不在线")
            unavailable_reason = self._task_unavailability(config, runtime, task_name)
            if unavailable_reason:
                raise AgentControlError(unavailable_reason)
            if runtime.lease is not None:
                raise AgentBusyError(f"设备 {device_id} 已有任务租约 {runtime.lease.execution_id}")
            lease = TaskLease(
                execution_id=str(uuid.uuid4()),
                task_name=task_name,
                config_revision=config["revision"],
                expires_at=self._lease_expiry(),
                requested_by=requested_by,
            )
            runtime.lease = lease
            runtime.current_task = task_name
            self._record_event_locked(
                runtime,
                "task_leased",
                execution_id=lease.execution_id,
                task_name=task_name,
                requested_by=requested_by,
            )

        try:
            # 租约和启动命令必须是一个不可插队的下行序列。否则管理员并发
            # 点击停止时，stop_task 可能插入两帧之间，被 Agent 视为尚未启动
            # 而忽略，最终出现“页面显示已停止但任务仍运行”的竞态。
            await self._send_many(
                runtime,
                [
                    lease.payload(device_id, config),
                    {
                        "type": "start_task",
                        "device_id": device_id,
                        "execution_id": lease.execution_id,
                        "task_name": task_name,
                        "config_revision": config["revision"],
                    },
                ],
            )
        except Exception as exc:
            await self.unregister(runtime.websocket, device_id)
            raise AgentOfflineError(f"设备 {device_id} 命令发送失败: {exc}") from exc
        return lease.execution_id

    async def request_stop(self, device_id: str) -> str:
        """向当前任务发送协作取消请求，任务仍须由 Agent 回报结果后结束。"""
        async with self._lock:
            runtime = self._devices.get(device_id)
            if runtime is None or runtime.lease is None:
                raise AgentControlError("设备没有可停止的任务")
            if not self._is_online(runtime):
                raise AgentOfflineError("设备离线，无法发送停止请求")
            runtime.lease.stop_requested = True
            lease = runtime.lease
            self._record_event_locked(
                runtime,
                "stop_requested",
                execution_id=lease.execution_id,
                task_name=lease.task_name,
            )
        await self._send(
            runtime,
            {
                "type": "stop_task",
                "device_id": device_id,
                "execution_id": lease.execution_id,
                "config_revision": lease.config_revision,
            },
        )
        return lease.execution_id

    async def resolve_recovery(self, device_id: str, execution_id: str) -> str:
        """管理员确认旧执行面已停止后，释放恢复阻塞的租约。

        该操作不会向设备发送触控或停止命令，只清除 Server 对丢失执行态的
        保守锁定。调用方必须显式回传页面展示的 execution_id，且当前状态必须
        是恢复阻塞态；普通运行中的租约均拒绝处理，避免误操作释放一个仍在
        正常执行的任务。
        """
        normalized_execution_id = str(execution_id or "").strip()
        if not normalized_execution_id:
            raise AgentControlError("execution_id 不能为空")
        async with self._lock:
            runtime = self._devices.get(device_id)
            if runtime is None or runtime.lease is None:
                raise AgentControlError("设备没有待人工确认的恢复租约")
            lease = runtime.lease
            if lease.execution_id != normalized_execution_id:
                raise AgentControlError("execution_id 与当前恢复租约不一致")
            if not runtime.recovery_reason:
                raise AgentControlError("当前租约不是待人工确认的恢复阻塞状态")
            runtime.lease = None
            runtime.current_task = None
            runtime.recovery_reason = None
            self._record_event_locked(
                runtime,
                "recovery_resolved",
                execution_id=normalized_execution_id,
                task_name=lease.task_name,
            )
        return normalized_execution_id

    async def expire_leases(self) -> None:
        """标记超时租约和失活会话，保留租约以禁止自动重复执行。"""
        now = datetime.now()
        async with self._lock:
            for runtime in self._devices.values():
                if runtime.lease and runtime.lease.expires_at < now:
                    if not runtime.lease.expired:
                        runtime.lease.expired = True
                        self._record_event_locked(
                            runtime,
                            "lease_expired",
                            execution_id=runtime.lease.execution_id,
                            task_name=runtime.lease.task_name,
                        )
                # Agent 卡死时 TCP/WebSocket 不一定立刻关闭。移除陈旧会话后，
                # Server 不会再向它派发任务；已有租约仍等原 Agent 重连并收尾。
                if runtime.websocket is not None and not self._is_online(runtime, now):
                    runtime.websocket = None

    async def snapshots(self) -> list[dict[str, Any]]:
        """生成管理 UI 使用的设备配置与运行态合并视图。"""
        result: list[dict[str, Any]] = []
        async with self._lock:
            runtimes = dict(self._devices)
        for device_id in self.repository.list_ids():
            try:
                config = self.repository.read(device_id)
            except DeviceConfigError as exc:
                result.append({"device_id": device_id, "config_error": str(exc)})
                continue
            runtime = runtimes.get(device_id)
            result.append(self._snapshot(config, runtime))
        return result

    async def snapshot(self, device_id: str) -> dict[str, Any]:
        """返回一台设备的管理视图。"""
        config = self.repository.read(device_id)
        async with self._lock:
            runtime = self._devices.get(device_id)
        return self._snapshot(config, runtime)

    async def control_events(self, device_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """返回设备的 Server 控制面事件，不返回 Agent 正常任务日志。"""
        self.repository.read(device_id)
        # 固定上限避免管理员页面请求造成大量内存复制或响应体膨胀。
        bounded_limit = max(1, min(int(limit), CONTROL_EVENT_LIMIT))
        async with self._lock:
            runtime = self._devices.get(device_id)
            if runtime is None:
                return []
            return copy.deepcopy(list(reversed(runtime.control_events[-bounded_limit:])))

    async def reserve_error_bundle_upload(
        self,
        device_id: str,
        execution_id: str,
        config_revision: int,
    ) -> tuple[str, str | None]:
        """确认租约并预留错误包上传，返回任务名和已完成的幂等目录。

        Agent 以 HTTP 上传错误包，因而该请求不能只凭全局共享密钥接受任意
        ``device_id/execution_id``。上传发生在 ``task_result`` 之前，故必须与
        当前租约及其配置版本匹配；结果 ACK 以后不再接收补传，避免正常任务
        通过旧执行编号向 Server 写入截图或日志。HTTP 响应丢失时 Agent 会按
        同一 execution_id 重试；已完成上传直接返回原目录，上传中的并发重试
        明确拒绝，防止同一失败现场写入多个目录。
        """
        if isinstance(config_revision, bool) or not isinstance(config_revision, int):
            raise AgentControlError("错误包 config_revision 必须是整数")
        async with self._lock:
            runtime = self._devices.get(device_id)
            lease = runtime.lease if runtime is not None else None
            if lease is None or lease.execution_id != execution_id:
                raise AgentControlError("错误包不属于当前任务租约")
            if lease.config_revision != config_revision:
                raise AgentControlError("错误包使用的配置修订号不是当前租约版本")
            existing_error_id = runtime.error_bundle_ids.get(execution_id)
            if existing_error_id is not None:
                return lease.task_name, existing_error_id
            if execution_id in runtime.error_bundle_uploading:
                raise AgentControlError("当前任务的错误包正在上传，请稍后重试")
            runtime.error_bundle_uploading.add(execution_id)
            # 返回服务端租约中的任务名，供 HTTP 层校验 Agent 元数据，避免
            # Agent 伪造另一个任务名并把错误现场归档到错误任务下。
            return lease.task_name, None

    async def complete_error_bundle_upload(self, device_id: str, execution_id: str, error_id: str) -> None:
        """提交已写入错误目录的预留上传，并使之后的重传幂等返回原目录。"""
        async with self._lock:
            runtime = self._devices.get(device_id)
            if runtime is None or execution_id not in runtime.error_bundle_uploading:
                raise AgentControlError("错误包上传预留已失效")
            runtime.error_bundle_uploading.remove(execution_id)
            runtime.error_bundle_ids[execution_id] = error_id
            # 为长期运行的单实例控制面设置有限上限；活跃租约的记录绝不裁剪。
            if len(runtime.error_bundle_ids) > 256:
                completed = [item for item in runtime.error_bundle_ids if item != execution_id]
                for stale_execution_id in completed[:128]:
                    runtime.error_bundle_ids.pop(stale_execution_id, None)
            self._record_event_locked(
                runtime,
                "error_bundle_uploaded",
                execution_id=execution_id,
                error_id=error_id,
            )

    async def restore_error_bundle_upload(
        self,
        device_id: str,
        execution_id: str,
        config_revision: int,
        task_name: str,
        error_id: str,
    ) -> bool:
        """从 Server 重启前已落盘的错误目录恢复当前执行的幂等关联。

        错误目录属于持久化文件，而本控制面的 ``error_bundle_ids`` 只在内存中。
        路由层会先限制目录位置并核验其权威 context.json；本方法仍重复核对
        当前租约，确保一个历史目录不能被恢复到其它任务或其它配置版本。

        返回值表示是否新恢复了一条运行态关联；同编号的重复恢复保持幂等。
        """
        if isinstance(config_revision, bool) or not isinstance(config_revision, int):
            raise AgentControlError("错误包 config_revision 必须是整数")
        normalized_task_name = str(task_name or "").strip()
        normalized_error_id = str(error_id or "").strip()
        if not normalized_task_name or not normalized_error_id:
            raise AgentControlError("错误包恢复缺少 task_name 或 error_id")
        async with self._lock:
            runtime = self._devices.get(device_id)
            lease = runtime.lease if runtime is not None else None
            if lease is None or lease.execution_id != execution_id:
                raise AgentControlError("错误包不属于当前任务租约")
            if lease.config_revision != config_revision:
                raise AgentControlError("错误包使用的配置修订号不是当前租约版本")
            if lease.task_name != normalized_task_name:
                raise AgentControlError("错误包 task_name 与当前任务租约不一致")
            existing_error_id = runtime.error_bundle_ids.get(execution_id)
            if existing_error_id is not None:
                if existing_error_id != normalized_error_id:
                    raise AgentControlError("当前任务已关联不同的错误包目录")
                return False
            if execution_id in runtime.error_bundle_uploading:
                raise AgentControlError("当前任务的错误包正在上传，不能恢复历史目录")
            runtime.error_bundle_ids[execution_id] = normalized_error_id
            self._record_event_locked(
                runtime,
                "error_bundle_recovered",
                execution_id=execution_id,
                error_id=normalized_error_id,
            )
            return True

    async def acknowledge_error_bundle(self, device_id: str, message: dict[str, Any]) -> dict[str, Any]:
        """确认 Agent 已看到落盘错误包，严格绑定当前租约和错误目录。

        HTTP 上传和 WSS 通知使用不同通道：前者只证明目录已写入，后者才让
        Agent 推进 outbox。两者都必须匹配同一个 execution_id、配置修订号和
        error_id，避免过期连接或伪造编号提前触发最终结果。
        """
        runtime = await self._runtime_for_session(device_id)
        execution_id = self._execution_id(message)
        config_revision = message.get("config_revision")
        if isinstance(config_revision, bool) or not isinstance(config_revision, int):
            raise AgentControlError("error_bundle.config_revision 必须是整数")
        error_id = self._optional_string(message.get("error_id"))
        if error_id is None:
            raise AgentControlError("error_bundle.error_id 不能为空")
        async with self._lock:
            lease = runtime.lease
            if lease is None or lease.execution_id != execution_id:
                raise AgentControlError("error_bundle 不属于当前任务租约")
            if lease.config_revision != config_revision:
                raise AgentControlError("error_bundle 使用的配置修订号不是当前租约版本")
            uploaded_error_id = runtime.error_bundle_ids.get(execution_id)
            if uploaded_error_id is None:
                raise AgentControlError("error_bundle 尚未完成 HTTP 上传")
            if uploaded_error_id != error_id:
                raise AgentControlError("error_bundle.error_id 与已上传错误包不一致")
            duplicate = runtime.acknowledged_error_bundle_ids.get(execution_id) == error_id
            runtime.acknowledged_error_bundle_ids[execution_id] = error_id
            # HTTP/WSS 响应丢失时 Agent 会重发通知；重复 ACK 不应挤掉真正的
            # 生命周期事件，因此只记录首次确认。
            if not duplicate:
                self._record_event_locked(
                    runtime,
                    "error_bundle_acknowledged",
                    execution_id=execution_id,
                    error_id=error_id,
                )
        return {
            "type": "error_bundle_ack",
            "device_id": device_id,
            "execution_id": execution_id,
            "config_revision": config_revision,
            "error_id": error_id,
            "duplicate": duplicate,
        }

    async def abandon_error_bundle_upload(self, device_id: str, execution_id: str) -> None:
        """清除失败 HTTP 写入的预留，让 Agent 之后可用同一租约安全重试。"""
        async with self._lock:
            runtime = self._devices.get(device_id)
            if runtime is not None:
                runtime.error_bundle_uploading.discard(execution_id)

    def _snapshot(self, config: dict[str, Any], runtime: DeviceRuntime | None) -> dict[str, Any]:
        """合并权威配置和会话态；在线状态必须包含心跳新鲜度。"""
        lease = runtime.lease if runtime else None
        return {
            "device_id": config["device_id"],
            "display_name": config.get("display_name", config["device_id"]),
            "config_revision": config["revision"],
            "required_agent_version": config["required_agent_version"],
            "online": self._is_online(runtime),
            "last_heartbeat": runtime.last_heartbeat.isoformat(timespec="seconds") if runtime and runtime.last_heartbeat else None,
            "foreground_package": runtime.foreground_package if runtime else None,
            "agent_version": runtime.agent_version if runtime else None,
            "resource_version": runtime.resource_version if runtime else None,
            "confirmed_config_revision": runtime.confirmed_config_revision if runtime else None,
            "compatible": runtime.compatible if runtime else False,
            "incompatibility_reason": runtime.incompatibility_reason if runtime else None,
            "recovery_reason": runtime.recovery_reason if runtime else None,
            "health": copy.deepcopy(runtime.health) if runtime else {"status": "offline"},
            "current_task": runtime.current_task if runtime else None,
            "lease": {
                "execution_id": lease.execution_id,
                "task_name": lease.task_name,
                "expires_at": lease.expires_at.isoformat(timespec="seconds"),
                "started_at": lease.started_at.isoformat(timespec="seconds") if lease.started_at else None,
                "stop_requested": lease.stop_requested,
                "expired": lease.expired,
                "requested_by": lease.requested_by,
            } if lease else None,
            "last_result": copy.deepcopy(runtime.last_result) if runtime else None,
            "tasks": copy.deepcopy(config["tasks"]),
            "task_support": {
                task_name: self._task_unavailability(config, runtime, task_name) is None
                for task_name in config["tasks"]
            },
            "task_support_reasons": {
                task_name: self._task_unavailability(config, runtime, task_name)
                for task_name in config["tasks"]
                if self._task_unavailability(config, runtime, task_name) is not None
            },
        }

    def _is_online(self, runtime: DeviceRuntime | None, now: datetime | None = None) -> bool:
        """仅把 WebSocket 存在且最近心跳未超时的 Agent 视为可派发状态。"""
        if runtime is None or runtime.websocket is None or runtime.last_heartbeat is None:
            return False
        current = now or datetime.now()
        return current - runtime.last_heartbeat <= timedelta(seconds=self.heartbeat_timeout_seconds)

    async def _runtime_for_session(self, device_id: str) -> DeviceRuntime:
        async with self._lock:
            runtime = self._devices.get(device_id)
            if not self._is_online(runtime):
                raise AgentOfflineError(f"设备 {device_id} 未完成 hello")
            return runtime

    async def _send(self, runtime: DeviceRuntime, payload: dict[str, Any]) -> None:
        async with runtime.send_lock:
            await self._send_locked(runtime, payload)

    async def _send_many(self, runtime: DeviceRuntime, payloads: list[dict[str, Any]]) -> None:
        """在同一设备发送锁内连续发送多帧，保持协议状态机的原子顺序。"""
        async with runtime.send_lock:
            for payload in payloads:
                await self._send_locked(runtime, payload)

    async def _send_locked(self, runtime: DeviceRuntime, payload: dict[str, Any]) -> None:
        """在已持有 ``send_lock`` 时发送单帧并补齐权威公共字段。"""
        if runtime.websocket is None:
            raise AgentOfflineError(f"设备 {runtime.device_id} 已离线")
        runtime.server_sequence += 1
        message = {
            **payload,
            # ``device_id``、时间与序列号是发送层的权威公共字段。必须在
            # payload 展开后最终写入，避免未来某个调用方误传这些键时把命令
            # 路由到其它 Agent 或破坏同一会话的单调 sequence。
            "device_id": runtime.device_id,
            "sent_at": datetime.now().isoformat(timespec="milliseconds"),
            "sequence": runtime.server_sequence,
        }
        await runtime.websocket.send_json(message)

    @staticmethod
    def _record_event_locked(runtime: DeviceRuntime, event: str, **details: Any) -> None:
        """在持有控制面锁时记录可展示的、非内容型事件元数据。"""
        record = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "event": event,
            **details,
        }
        runtime.control_events.append(record)
        if len(runtime.control_events) > CONTROL_EVENT_LIMIT:
            del runtime.control_events[:-CONTROL_EVENT_LIMIT]

    def _lease_expiry(self) -> datetime:
        return datetime.now() + timedelta(seconds=self.lease_seconds)

    @staticmethod
    def _parse_task_manifest(raw_manifest: Any) -> dict[str, str]:
        """读取 Agent 镜像内声明的任务清单和每项最低资源版本。"""
        if not isinstance(raw_manifest, list):
            return {}
        manifest: dict[str, str] = {}
        for item in raw_manifest:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            minimum_resource_version = str(item.get("minimum_resource_version") or "").strip()
            if name and minimum_resource_version:
                manifest[name] = minimum_resource_version
        return manifest

    @staticmethod
    def _normalize_health(raw_health: Any) -> dict[str, Any]:
        """限制 Agent 健康报告为可安全展示的基础结构。"""
        if not isinstance(raw_health, dict):
            return {"status": "initializing"}
        status = str(raw_health.get("status") or "initializing")
        if status not in {"initializing", "healthy", "unhealthy"}:
            status = "unhealthy"
        checks = raw_health.get("checks")
        return {
            "status": status,
            "reason": str(raw_health.get("reason") or "")[:500] or None,
            "checks": {str(key): bool(value) for key, value in checks.items()} if isinstance(checks, dict) else {},
        }

    def _task_unavailability(self, config: dict[str, Any], runtime: DeviceRuntime | None, task_name: str) -> str | None:
        """返回任务不能派发的原因；None 表示当前 Agent 允许执行。"""
        if not self._is_online(runtime):
            return "Agent 不在线"
        if not runtime.compatible:
            return runtime.incompatibility_reason or "Agent 版本不兼容"
        if runtime.health.get("status") != "healthy":
            return runtime.health.get("reason") or "Agent 设备预检尚未通过"
        if task_name not in runtime.task_manifest:
            return f"Agent 镜像未声明支持任务 {task_name}"
        configured_minimum = str(
            config["tasks"][task_name].get("agent", {}).get("minimum_resource_version", "")
        ).strip()
        required_resource_version = configured_minimum or runtime.task_manifest[task_name]
        if not version_at_least(runtime.resource_version, required_resource_version):
            return (
                f"Agent 资源版本 {runtime.resource_version!r} 低于任务 {task_name} "
                f"要求的 {required_resource_version!r}"
            )
        return None

    @staticmethod
    def _require_protocol(message: dict[str, Any]) -> None:
        if message.get("protocol_version") != PROTOCOL_VERSION:
            raise AgentControlError(f"只支持 protocol_version={PROTOCOL_VERSION}")

    @staticmethod
    def _message_device_id(message: dict[str, Any]) -> str:
        value = str(message.get("device_id") or "").strip()
        if not value:
            raise AgentControlError("消息缺少 device_id")
        return value

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        """规范化可选状态字符串，避免把任意对象直接放入管理快照。"""
        text = str(value or "").strip()
        return text[:255] or None

    @staticmethod
    def _result_summary(value: Any) -> str:
        """清理并限制 Agent 结果摘要，供管理 UI 安全展示。"""
        return handle_sensitive_text(str(value or ""))[:1000]

    @staticmethod
    def _cached_config_revision(message: dict[str, Any]) -> int | None:
        """读取 Agent 已确认快照的修订号，拒绝损坏的运行态字段。"""
        revision = message.get("cached_config_revision")
        if revision is None:
            return None
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise AgentControlError("cached_config_revision 必须是正整数或 null")
        return revision

    @staticmethod
    def _result_duration(value: Any) -> float | None:
        """接受有限、非负的任务耗时，避免异常状态字段污染管理快照。"""
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise AgentControlError("task_result.duration_seconds 必须是非负数")
        duration = float(value)
        if duration < 0 or duration > 7 * 24 * 60 * 60:
            raise AgentControlError("task_result.duration_seconds 超出允许范围")
        return duration

    @staticmethod
    def _execution_id(message: dict[str, Any]) -> str:
        value = str(message.get("execution_id") or "").strip()
        if not value:
            raise AgentControlError("消息缺少 execution_id")
        return value

    @staticmethod
    def _validate_message_envelope(message: dict[str, Any], label: str) -> None:
        """校验所有 Agent 上行消息共有的时间与单调序列字段。"""
        sequence = message.get("sequence")
        sent_at = message.get("sent_at")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise AgentControlError(f"{label}.sequence 必须是非负整数")
        if not isinstance(sent_at, str) or not sent_at.strip():
            raise AgentControlError(f"{label}.sent_at 必须是非空字符串")
