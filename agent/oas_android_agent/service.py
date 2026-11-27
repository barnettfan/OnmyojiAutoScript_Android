"""Android/redroid 常驻 Agent：接收租约，在本机运行 OAS，并回传结果。"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
import subprocess
import sys
import tempfile
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import websockets

from .config import AgentSettings, AgentState, StateStore
from .config_sync import ConfigSyncError, RuntimeConfigStore, native_task_keys
from .error_bundle import ErrorBundleUploader
from .task_manifest import TASK_MANIFEST, resource_version_at_least, task_minimum_resource_version
from module.device.root_shell_command import root_shell_command


PROTOCOL_VERSION = 1
PREFLIGHT_RECHECK_SECONDS = 30
EXECUTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,127}$")
# RootShell 预检临时 UI XML 只在容器内短暂存在；同一状态卷由进程锁独占，
# 因此无需让目录名承载外部输入或设备标识。
ROOT_PREFLIGHT_HIERARCHY_PATH = "/data/local/tmp/oas-agent-preflight.xml"
# RootShell 不会连接 ADB，但既有 OAS 的继承结构在导入期仍会加载这些兼容模块。
# 这些依赖必须固定在版本化基础镜像中；任务派发前先给出明确健康原因。
REQUIRED_RUNTIME_MODULES = (
    "cv2", "numpy", "PIL", "lxml", "pydantic", "yaml", "zerorpc", "requests",
    "adbutils", "uiautomator2", "onnxruntime", "paddleocr",
)


@dataclass
class PendingLease:
    """已收到但尚未收到 start_task 的租约和配置快照。"""

    execution_id: str
    task_name: str
    config_revision: int
    config: dict[str, Any]
    # Server 发出的租约绝对过期时间。Agent 在收到租约和真正启动任务前都要
    # 检查它，避免网络延迟或重放的旧命令在设备上触发新的触控。
    expires_at: datetime | None = None


class AndroidAgent:
    """保持出站 WSS；断线时继续当前任务，但绝不自行启动下一任务。"""

    def __init__(self, settings: AgentSettings) -> None:
        self.settings = settings
        self.state_store = StateStore(settings.state_root)
        self.state = self.state_store.load()
        self.config_store = RuntimeConfigStore(settings.runtime_root, settings.state_root, settings.device_id)
        self.error_uploader = ErrorBundleUploader(settings)
        self.pending_lease: PendingLease | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.process_watch: asyncio.Task | None = None
        self.websocket: Any | None = None
        self.sequence = 0
        # 心跳、错误现场回执和任务 outbox 都可能并发发送；必须串行化消息编号
        # 与 WebSocket 写入，避免 Server 因 sequence 乱序拒绝整个会话。
        self._send_lock = asyncio.Lock()
        # 任务结束和心跳超时都可能尝试冲刷 outbox；串行化可避免同一调度补丁
        # 在收到 ACK 前被重复提交。
        self._outbox_lock = asyncio.Lock()
        self._outbox_inflight: tuple[str, str] | None = None
        # Server 重启会丢失已经确认的错误包运行态。失败任务若正处于后续
        # schedule/result 阶段，重连时先重申同一个 error_id，才能让新 Server
        # 重建确认关系并最终接受失败结果；该集合只属于当前 WSS 会话。
        self._error_bundle_reassertions: set[str] = set()
        # Server 正常会按顺序发送 task_lease、start_task。若 Agent 在第一帧
        # 拒绝租约，Server 收到 task_result 后会释放该租约，但第二帧可能已经
        # 在网络缓冲区中；记录这些编号以静默丢弃对应 start_task，避免第二次
        # rejected result 被 Server 当作不属于当前租约的协议错误。
        self._rejected_lease_start_ids: set[str] = set()
        self.last_server_sequence = -1
        self.health: dict[str, Any] = {"status": "initializing"}
        # 仅缓存 Server 已确认的设备快照，用于不健康时重试预检；它不是调度真相。
        self._latest_server_config: dict[str, Any] | None = None
        # 运行中租约保持开始时的快照；管理员更新配置后的预检延后到结果 ACK。
        self._pending_preflight_config: dict[str, Any] | None = None
        self._last_preflight_monotonic = 0.0
        self._resume_commands: list[dict[str, Any]] = []
        # 该文件描述符在 Agent 存活期间持有 POSIX advisory lock。redroid 的
        # init 重复拉起或人工误执行启动命令时，第二个 Agent 必须在连接 Server
        # 之前退出，不能让两个进程争用同一台设备的 Root 输入。
        self._instance_lock_fd: int | None = None
        # 必须在成功持有 ``agent.lock`` 后才恢复中断租约。否则两次几乎同时
        # 启动的 Agent 都可能在锁竞争前改写同一个 outbox，破坏单实例边界。
        self._interrupted_execution_recovered = False

    def _recover_interrupted_execution(self) -> None:
        """将 Agent 重启时未落盘的执行转换为可回传的失败结果。

        正常任务结束会先写 outbox、再等待 Server ACK，因此已有 outbox 时不能
        改写它。仅有 ``current_execution_id`` 说明常驻 Agent 在子进程写结果前
        已退出；重连后由 Server 按同一 execution_id 收尾，避免永久占住租约。
        """
        execution_id = self.state.current_execution_id
        task_name = self.state.current_task_name
        revision = self.state.current_config_revision
        if not execution_id or not task_name or not isinstance(revision, int):
            return
        # 原子 outbox 在正常重启时保持原结果；半写入或不匹配文件则用明确失败
        # 结果替换，避免 Server/Agent 都等待一个永远不能解析的文件。
        outbox_path = self._outbox_path(execution_id)
        if self._read_outbox(execution_id) is not None:
            return
        recovery_summary = (
            "Agent outbox was corrupted before task result was persisted"
            if outbox_path.exists()
            else "Agent restarted before task result was persisted"
        )
        self._write_outbox(
            PendingLease(execution_id, task_name, revision, {}),
            {
                "status": "failure",
                "summary": recovery_summary,
            },
            None,
        )
        self._log(f"Recovered interrupted execution {execution_id} as failure")

    async def run_forever(self) -> None:
        """以退避重连保持到 Server 的唯一出站连接。"""
        self._acquire_instance_lock()
        try:
            self._recover_interrupted_execution_after_lock()
            retry_seconds = 1
            while True:
                try:
                    async with websockets.connect(self._websocket_url(), ping_interval=20, ping_timeout=20) as websocket:
                        self._set_connection(websocket)
                        # Server 为每条新 WebSocket 会话重新编号。
                        self.last_server_sequence = -1
                        await self._serve_connection(websocket)
                        retry_seconds = 1
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._log(f"WSS disconnected: {type(exc).__name__}: {exc}; retry in {retry_seconds}s")
                    await asyncio.sleep(retry_seconds)
                    retry_seconds = min(retry_seconds * 2, 30)
                finally:
                    self.websocket = None
        finally:
            self._release_instance_lock()

    def _set_connection(self, websocket: Any) -> None:
        """切换到新 WSS 会话，并清除只对旧连接有效的 outbox ACK 状态。"""
        # ACK 只属于上一条 WebSocket 会话；若旧连接在 ACK 到达前断开，
        # 重连后必须允许 outbox 重新发送当前阶段。
        self._outbox_inflight = None
        self._queue_error_bundle_reassertion()
        self.websocket = websocket

    def _queue_error_bundle_reassertion(self) -> None:
        """为重连中的失败结果重新登记错误包确认，兼容 Server 内存态重启。

        失败任务的错误包可能已在旧 Server 落盘、并且 Agent 已经收到 ACK，随后
        outbox 才推进到 schedule 或 result。若这时 Server 重启，它不再知道
        ``error_id`` 已被确认；Agent 不能直接重传结果，而要先在新会话中重发
        同一个 ``error_bundle`` 元数据帧。只检查当前持久化 execution，防止
        旧的异常文件或已结束任务影响新的 Server 租约。
        """
        self._error_bundle_reassertions.clear()
        execution_id = self.state.current_execution_id
        if not execution_id:
            return
        payload = self._read_outbox(execution_id)
        if payload is None or payload.get("phase") not in {"schedule", "result"}:
            return
        result = payload.get("result")
        if not isinstance(result, dict) or result.get("status") not in {"failure", "manual_required"}:
            return
        error_id = result.get("error_id")
        if not isinstance(error_id, str) or not error_id.strip():
            return
        self._error_bundle_reassertions.add(execution_id)

    def _recover_interrupted_execution_after_lock(self) -> None:
        """在独占状态目录后，将未完成执行转换为可回传的失败结果。

        出现容器重启时，恢复操作会写 outbox；它和任务启动一样属于单 Agent
        的状态变更。持锁后才允许执行，避免第二个误启动进程覆盖第一进程的
        结果或在尚未连接 Server 前制造重复恢复记录。
        """
        if self._interrupted_execution_recovered:
            return
        if self._instance_lock_fd is None:
            raise RuntimeError("Interrupted execution recovery requires the Agent instance lock")
        self._recover_interrupted_execution()
        self._interrupted_execution_recovered = True

    def _acquire_instance_lock(self) -> None:
        """以状态目录文件锁保证一台设备只运行一个本地 Agent 进程。"""
        if self._instance_lock_fd is not None:
            return
        try:
            import fcntl
        except ImportError as exc:
            raise RuntimeError("Android Agent requires POSIX file-lock support") from exc
        self.settings.state_root.mkdir(parents=True, exist_ok=True)
        lock_path = self.settings.state_root / "agent.lock"
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(lock_fd)
            raise RuntimeError(
                f"Another Android Agent already owns device {self.settings.device_id}"
            ) from exc
        self._instance_lock_fd = lock_fd

    def _release_instance_lock(self) -> None:
        """释放本 Agent 的进程锁；异常退出时内核也会自动释放它。"""
        lock_fd = self._instance_lock_fd
        if lock_fd is None:
            return
        self._instance_lock_fd = None
        try:
            import fcntl

            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)

    async def _serve_connection(self, websocket: Any) -> None:
        """完成 hello 同步后串行处理命令与回执，避免 WebSocket 并发读取。"""
        await self._send(
            {
                "type": "hello",
                "protocol_version": PROTOCOL_VERSION,
                "agent_version": self.settings.agent_version,
                "resource_version": self.settings.resource_version,
                "capabilities": ["root_shell", "local_ocr", "local_tasks", "error_bundle_v1"],
                "task_manifest": list(TASK_MANIFEST),
                "current_execution_id": self.state.current_execution_id,
                "current_task_name": self.state.current_task_name,
                "current_config_revision": self.state.current_config_revision or self.state.confirmed_revision,
                "cached_config_revision": self.state.confirmed_revision,
                "health": self.health,
            }
        )
        hello_ack = await asyncio.wait_for(websocket.recv(), timeout=20)
        await self._handle_message(json.loads(hello_ack), is_hello=True)
        if self.websocket is None:
            return
        await self._report_preflight(json.loads(hello_ack).get("config"))
        await self._handle_resume_commands_after_preflight()

        await self._flush_outbox()
        while True:
            try:
                raw = await asyncio.wait_for(websocket.recv(), timeout=self.settings.heartbeat_seconds)
            except asyncio.TimeoutError:
                await self._send_heartbeat()
                await self._flush_outbox()
                continue
            await self._handle_message(json.loads(raw))
            await self._flush_outbox()

    async def _handle_resume_commands_after_preflight(self) -> None:
        """在本次预检完成后恢复旧租约，或显式释放不能启动的旧租约。"""
        commands = self._resume_commands
        self._resume_commands = []
        if self.health["status"] == "healthy":
            for command in commands:
                # resume_* 是已通过序列号校验的 hello_ack 的内嵌字段，而非独立
                # WSS 帧。它们不会也不应各自占用 Server sequence。
                await self._handle_message(command, embedded_in_hello_ack=True)
            return
        for command in commands:
            if command.get("type") != "start_task":
                continue
            # 重连前断在 start_task 之前的租约尚未执行。预检不健康时不能静默
            # 丢弃它，否则 Server 会永久保留租约、设备也无法在恢复健康后再调度。
            await self._send_result(
                self._required(command, "execution_id"),
                "rejected",
                "Agent device preflight is not healthy; recovered lease was not started",
                self._required_int(command, "config_revision"),
            )

    async def _handle_message(
        self,
        message: dict[str, Any],
        is_hello: bool = False,
        *,
        embedded_in_hello_ack: bool = False,
    ) -> None:
        """校验 Server 消息并执行唯一允许的本地状态变更。"""
        if not isinstance(message, dict):
            raise ValueError("Server message must be a JSON object")
        # 正常控制面帧均由 Server 发送层补齐 device_id。对于握手失败等旧式
        # 通用错误帧保留缺省兼容；但一旦显式指定了另一台设备，必须在更新序列
        # 或配置前拒绝，防止路由错误跨设备启动/停止任务。
        message_device_id = message.get("device_id")
        if message_device_id is not None and message_device_id != self.settings.device_id:
            raise ValueError("Server message device_id does not match this Agent")
        message_type = message.get("type")
        if embedded_in_hello_ack:
            # 只有 hello_ack 中 Server 生成的恢复命令可免除独立 sequence；其它
            # 命令始终要求严格递增编号，避免把此入口变成重放旁路。
            if message_type not in {"task_lease", "start_task"}:
                raise ValueError("hello_ack 内嵌消息只能是恢复 task_lease 或 start_task")
        else:
            self._observe_server_sequence(message)
        if message_type == "hello_ack":
            if not message.get("accepted"):
                raise RuntimeError(f"Server rejected Agent: {message.get('reason')}")
            self._confirm_config(message.get("config"), message.get("config_revision"))
            resume_lease = message.get("resume_lease")
            resume_start = message.get("resume_start")
            if isinstance(resume_lease, dict):
                self._resume_commands.append(resume_lease)
            if isinstance(resume_start, dict):
                self._resume_commands.append(resume_start)
            return
        if message_type == "heartbeat_ack":
            config = message.get("config")
            revision = message.get("config_revision")
            if isinstance(config, dict):
                self._confirm_config(config, revision)
                self._latest_server_config = config
                # pending lease 也属于配置固定窗口：它虽然尚未启动子进程，
                # 但 start_task 必须使用 task_lease 所附的运行时快照。
                if self.state.current_execution_id is not None or self.pending_lease is not None:
                    self._pending_preflight_config = config
                else:
                    await self._report_preflight(config)
                    return
            # redroid 启动期 Surface、Root 或游戏包可能尚未稳定。保持 WSS 在线，
            # 周期性本机复检，且绝不据此自行领取或启动任务。
            if (
                self.state.current_execution_id is None
                and self.pending_lease is None
                and self.health.get("status") != "healthy"
                and self._latest_server_config is not None
                and self._preflight_recheck_due()
            ):
                await self._report_preflight(self._latest_server_config)
            return
        if message_type == "health_report_ack":
            return
        if message_type == "task_lease":
            await self._receive_lease(message)
            return
        if message_type == "start_task":
            await self._start_task(message)
            return
        if message_type == "stop_task":
            self._request_stop(message)
            return
        if message_type == "schedule_patch_ack":
            self._confirm_schedule_patch(message)
            return
        if message_type == "task_result_ack":
            result_confirmed = self._confirm_result(message)
            # 只有 Server 对当前 outbox 的有效 ACK 才能结束旧租约窗口。畸形、
            # 过期或跨连接 ACK 不得提前激活管理员的新配置或覆盖运行中任务。
            if result_confirmed and self._pending_preflight_config is not None:
                pending_config = self._pending_preflight_config
                self._pending_preflight_config = None
                await self._report_preflight(pending_config)
            return
        if message_type == "task_started_ack":
            return
        if message_type == "error_bundle_ack":
            self._confirm_error_bundle_notice(message)
            return
        if message_type == "error":
            self._log(f"Server message: {message_type} {message.get('reason', '')}")
            return
        raise ValueError(f"Unsupported Server message type: {message_type!r}")

    async def _receive_lease(self, message: dict[str, Any]) -> None:
        """缓存任务租约前先落盘 Server 已确认配置快照。"""
        execution_id = self._required(message, "execution_id")
        task_name = self._required(message, "task_name")
        revision = self._required_int(message, "config_revision")
        if self.state.current_execution_id and self.state.current_execution_id != execution_id:
            await self._reject_task_lease(execution_id, revision, "Agent is busy with another execution")
            return
        existing_lease = self.pending_lease
        if existing_lease is not None:
            # ``task_lease`` 与 ``start_task`` 是同一授权的两阶段命令。收到首帧后
            # 不能让另一份租约覆盖本地固定的配置快照，否则随后到达的 start_task
            # 会以错误任务或错误 revision 启动。完全相同的重传保持幂等即可。
            if (
                existing_lease.execution_id == execution_id
                and existing_lease.task_name == task_name
                and existing_lease.config_revision == revision
                and existing_lease.config == message.get("config")
            ):
                return
            await self._reject_task_lease(execution_id, revision, "Agent already has a pending task lease")
            return
        config = message.get("config")
        # 租约中的任务、版本和配置快照必须三者一致。不能只相信 Server 之后会
        # 下发正确 JSON：若任务不在快照中，ConfigModel 会填充默认值，导致 Agent
        # 以未被 Server 明确授权的默认配置触控。
        rejection = self._task_lease_rejection(task_name, revision, config)
        if rejection:
            await self._reject_task_lease(execution_id, revision, rejection)
            return
        expires_at = self._parse_lease_expiry(message.get("expires_at"))
        if self._lease_is_expired(expires_at):
            # 不写入本地配置或 pending state。Server 收到 rejected 结果后会
            # 释放本次租约，并依照权威 next_run 决定是否重建一份新租约。
            await self._reject_task_lease(execution_id, revision, "Task lease has already expired")
            return
        self._confirm_config(config, revision)
        self.pending_lease = PendingLease(execution_id, task_name, revision, config, expires_at)
        self._rejected_lease_start_ids.discard(execution_id)

    def _task_lease_rejection(self, task_name: str, revision: int, config: Any) -> str | None:
        """验证租约附带的快照，避免把配置缺失任务交给 OAS 默认模型。"""
        if not isinstance(config, dict):
            return "Task lease does not include a configuration snapshot"
        if config.get("revision") != revision:
            return "Task lease config revision does not match config_revision"
        tasks = config.get("tasks")
        if not isinstance(tasks, dict) or task_name not in tasks:
            return f"Task {task_name!r} is absent from the lease configuration"
        return self._task_manifest_rejection(task_name, config)

    def _task_manifest_rejection(self, task_name: str, config: Any) -> str | None:
        """在 Agent 本地复核固定任务清单，防止错误或恶意命令执行任意目录。"""
        minimum = task_minimum_resource_version(task_name)
        if minimum is None:
            return f"Task {task_name!r} is not declared in this Agent image"
        configured_minimum = ""
        if isinstance(config, dict):
            task_config = config.get("tasks", {}).get(task_name)
            if isinstance(task_config, dict):
                agent_config = task_config.get("agent", {})
                if isinstance(agent_config, dict):
                    configured_minimum = str(agent_config.get("minimum_resource_version") or "").strip()
        required = configured_minimum or minimum
        if not resource_version_at_least(self.settings.resource_version, required):
            return f"Agent resource version {self.settings.resource_version!r} is below required {required!r}"
        return None

    async def _start_task(self, message: dict[str, Any]) -> None:
        """只启动已接收、版本一致且当前空闲的租约；重复命令幂等。"""
        execution_id = self._required(message, "execution_id")
        task_name = self._required(message, "task_name")
        revision = self._required_int(message, "config_revision")
        if execution_id in self._rejected_lease_start_ids:
            # 对已拒绝租约的延迟/重放 start_task 不再回传第二份 task_result。
            # 首份 rejected 已足够让 Server 释放租约；重复上报反而会使 Server
            # 以为 Agent 在尝试完成已失效的 execution_id。
            return
        if self.state.current_execution_id == execution_id:
            return
        lease = self.pending_lease
        if (
            lease is None
            or lease.execution_id != execution_id
            or lease.task_name != task_name
            or lease.config_revision != revision
        ):
            await self._send_result(execution_id, "rejected", "No matching task_lease", revision)
            return
        if self.health.get("status") != "healthy":
            # 拒绝结果会让 Server 释放该租约。不能继续保留 pending lease，
            # 否则之后重放的 start_task 可能在 Server 已失去该授权后启动。
            self._discard_pending_lease(execution_id, revision)
            await self._reject_task_lease(execution_id, revision, "Agent device preflight is not healthy")
            await self._activate_deferred_config_after_pending_lease()
            return
        if lease.expires_at is not None and self._lease_is_expired(lease.expires_at):
            # task_lease 与 start_task 是两帧消息；即使租约在第一帧到达时仍然
            # 有效，也不能让被阻塞或延迟的第二帧跨过 Server 授权窗口启动任务。
            self._discard_pending_lease(execution_id, revision)
            await self._reject_task_lease(execution_id, revision, "Task lease expired before start_task")
            await self._activate_deferred_config_after_pending_lease()
            return
        if self.process is not None:
            # 同机任务子进程尚未完成时，Server 会把这个新租约视为拒绝并释放；
            # 因此不能让它残留并等待一次可能已过期的重放启动命令。
            self._discard_pending_lease(execution_id, revision)
            await self._reject_task_lease(execution_id, revision, "Agent task process is still active")
            await self._activate_deferred_config_after_pending_lease()
            return
        self.pending_lease = None
        self.state.current_execution_id = execution_id
        self.state.current_task_name = lease.task_name
        self.state.current_config_revision = lease.config_revision
        self.state_store.save(self.state)
        self._stop_file(execution_id).unlink(missing_ok=True)
        # 先将任务加入事件循环，再发送启动确认。当前协程会先取得发送锁；新任务
        # 即使立即结束也只能在该确认之后发送结果，避免 Server 状态倒序。
        self.process_watch = asyncio.create_task(self._run_task_process(lease))
        await self._send({
            "type": "task_started",
            "execution_id": execution_id,
            "task_name": lease.task_name,
            "config_revision": revision,
        })

    def _discard_pending_lease(self, execution_id: str, config_revision: int) -> None:
        """仅清理与已拒绝启动命令完全一致的 pending 租约。

        网络重放或错误的 start_task 不得影响另一份刚收到的租约；反之，与当前
        pending lease 完全匹配的启动拒绝表示 Server 将释放该授权，必须立刻
        丢弃，防止健康状态恢复后启动已失效的执行编号。
        """
        lease = self.pending_lease
        if lease is None:
            return
        if lease.execution_id != execution_id or lease.config_revision != config_revision:
            return
        self.pending_lease = None

    async def _activate_deferred_config_after_pending_lease(self) -> None:
        """在未启动租约被释放后，安全激活管理员已同步的新快照。

        管理员可能恰好在 ``task_lease`` 与 ``start_task`` 两帧之间保存配置。
        该窗口内运行时 JSON 必须钉在租约快照；一旦该租约被拒绝或过期，才切换
        到已确认的新配置并重新进行预检，使 Server 不会长期停留在 initializing。
        """
        if self.state.current_execution_id is not None or self.pending_lease is not None:
            return
        self.config_store.activate_confirmed_runtime()
        config = self._pending_preflight_config
        if config is None:
            return
        self._pending_preflight_config = None
        await self._report_preflight(config)

    async def _reject_task_lease(self, execution_id: str, config_revision: int, summary: str) -> None:
        """上报一次租约拒绝，并忽略后续同编号的延迟启动命令。

        拒绝记录只用于处理同一连接已经排队的 ``start_task``，不作为跨重启的
        调度状态。Server 仍是租约和下一次任务授权的唯一来源。
        """
        self._rejected_lease_start_ids.add(execution_id)
        # 限定常驻进程中的重放编号缓存，正常 Server UUID 不会复用编号。
        if len(self._rejected_lease_start_ids) > 256:
            self._rejected_lease_start_ids = set(list(self._rejected_lease_start_ids)[-128:])
        await self._send_result(execution_id, "rejected", summary, config_revision)

    def _request_stop(self, message: dict[str, Any]) -> None:
        """只为当前 execution_id 写协作停止标记，绝不杀游戏或容器。"""
        execution_id = self._required(message, "execution_id")
        revision = self._required_int(message, "config_revision")
        # 停止命令也绑定到当前租约快照。execution_id 虽由 Server 生成且唯一，
        # 仍不能让陈旧或畸形命令停止一个版本不一致的本地执行。
        if (
            execution_id != self.state.current_execution_id
            or revision != self.state.current_config_revision
        ):
            return
        stop_file = self._stop_file(execution_id)
        stop_file.parent.mkdir(parents=True, exist_ok=True)
        stop_file.write_text("stop requested\n", encoding="utf-8")
        self._log(f"Stop requested for {execution_id}")

    async def _run_task_process(self, lease: PendingLease) -> None:
        """运行一个本地 OAS 子进程，完成后将补丁和结果放入可重放 outbox。"""
        result_path = self.settings.state_root / "results" / f"{lease.execution_id}.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        environment = dict(os.environ)
        environment["OAS_AGENT_STOP_FILE"] = str(self._stop_file(lease.execution_id))
        # 在启动 task_runner 前就锁定本地 RPC，避免任何导入期配置使用远端地址。
        environment["OAS_AGENT_LOCAL_RPC_ONLY"] = "true"
        # Restart 的 scheduler.enable 在 C/S 模板中只表示周期派发，不妨碍
        # 已获 Server 租约的任务在同一子进程内完成游戏级恢复。
        environment["OAS_AGENT_EXECUTION_LEASE"] = "true"
        # OAS 任务内部的 task_call 也必须回到 Server 的调度快照，禁止只改本地 JSON。
        environment["OAS_AGENT_SCHEDULED_TASKS"] = json.dumps(
            native_task_keys(lease.config), ensure_ascii=False
        )
        # Agent 镜像必须离线运行；OAS OCR 兜底修复路径在这里显式禁用。
        environment["OAS_AGENT_NO_RUNTIME_DOWNLOAD"] = "true"
        package_root = str(Path(__file__).resolve().parents[1])
        environment["PYTHONPATH"] = os.pathsep.join([package_root, str(self.settings.runtime_root), environment.get("PYTHONPATH", "")])
        command = [
            sys.executable, "-m", "oas_android_agent.task_runner",
            "--runtime-root", str(self.settings.runtime_root),
            "--config-name", self.config_store.config_name,
            "--task", lease.task_name,
            "--result", str(result_path),
            "--stop-file", str(self._stop_file(lease.execution_id)),
            # 子进程监视常驻 Agent；服务崩溃/重启后旧任务必须退出，不能成为
            # 与恢复租约并发运行的孤儿进程。
            "--supervisor-pid", str(os.getpid()),
        ]
        try:
            self.process = await asyncio.create_subprocess_exec(*command, cwd=self.settings.runtime_root, env=environment)
            await self.process.wait()
            result = self._load_result(result_path, lease)
        except Exception as exc:
            result = self._subprocess_failure_result(lease, f"Agent subprocess failed: {type(exc).__name__}: {exc}")
        finally:
            self.process = None
        if result.get("status") == "manual_required" and result.get("runner_failure") is True:
            result.pop("runner_failure", None)
        # 调度补丁必须以任务开始时的租约快照为基准。Server 配置若在运行中
        # 被管理员更新，不能把那份新快照混入当前任务的状态机变更。
        patch = self.config_store.schedule_patch_from_runtime(lease.config)
        self._write_outbox(lease, result, patch)
        # 任务完成无需等待下一个 heartbeat；连接可用时立即开始补丁/结果回传。
        await self._flush_outbox()

    def _write_outbox(self, lease: PendingLease, result: dict[str, Any], patch: dict[str, Any] | None) -> None:
        """先持久化再传输，失败现场、补丁和结果均可在重连后继续收尾。"""
        status = str(result.get("status") or "")
        # HTTP 错误包必须在 task_result 前入库；Server 接受结果后会释放租约，
        # 之后的补传会被拒绝。把失败现场也做成 outbox 阶段，避免短暂断网时
        # 只有结果回传而丢失截图、日志和上下文。
        if status in {"failure", "manual_required"}:
            phase = "error_bundle"
        else:
            phase = "schedule" if patch else "result"
        payload = {
            "execution_id": lease.execution_id,
            "task_name": lease.task_name,
            "config_revision": lease.config_revision,
            "result": result,
            "patch": patch,
            "phase": phase,
        }
        path = self._outbox_path(lease.execution_id)
        self._atomic_write_json(path, payload)

    async def _flush_outbox(self) -> None:
        """每次只发送一个 outbox 阶段，严格保持错误包、补丁、结果顺序。"""
        if self.websocket is None:
            return
        async with self._outbox_lock:
            if self.websocket is None:
                return
            # 一条未确认的控制面帧必须阻塞后续所有阶段。原先只比较当前
            # outbox phase，在 Server 重启后的 error_bundle 重申场景中会让
            # task_result 绕过尚未确认的重申帧。
            if self._outbox_inflight is not None:
                return
            for execution_id in sorted(self._error_bundle_reassertions):
                payload = self._read_outbox(execution_id)
                result = payload.get("result") if isinstance(payload, dict) else None
                error_id = result.get("error_id") if isinstance(result, dict) else None
                if (
                    payload is None
                    or payload.get("phase") not in {"schedule", "result"}
                    or not isinstance(error_id, str)
                    or not error_id
                ):
                    self._error_bundle_reassertions.discard(execution_id)
                    continue
                await self._send(
                    {
                        "type": "error_bundle",
                        "execution_id": execution_id,
                        "config_revision": payload["config_revision"],
                        "error_id": error_id,
                    }
                )
                self._outbox_inflight = (execution_id, "error_bundle_reassert")
                return
            for path in sorted((self.settings.state_root / "outbox").glob("*.json")):
                execution_id = path.stem
                payload = self._read_outbox(execution_id)
                if payload is None:
                    self._recover_corrupted_current_outbox(execution_id)
                    continue
                phase = str(payload.get("phase") or "")
                if not execution_id or phase not in {"error_bundle", "error_bundle_notice", "schedule", "result"}:
                    continue
                if phase == "error_bundle":
                    await self._upload_error_bundle_from_outbox(execution_id, payload)
                    # 上传成功后由该方法持久化为通知阶段；若 HTTP 失败，
                    # 保持当前阶段并等待下一次重连/heartbeat 重试。
                    continue
                if phase == "error_bundle_notice":
                    result = payload.get("result")
                    error_id = result.get("error_id") if isinstance(result, dict) else None
                    if not isinstance(error_id, str) or not error_id:
                        raise ValueError("Error bundle outbox does not contain error_id")
                    await self._send(
                        {
                            "type": "error_bundle",
                            "execution_id": execution_id,
                            "config_revision": payload["config_revision"],
                            "error_id": error_id,
                        }
                    )
                elif phase == "schedule":
                    await self._send(
                        {
                            "type": "schedule_patch",
                            "execution_id": execution_id,
                            "config_revision": payload["config_revision"],
                            "patch": payload["patch"],
                        }
                    )
                else:
                    result = payload.get("result", {})
                    await self._send_result(
                        execution_id, result.get("status", "failure"), result.get("summary", ""),
                        payload["config_revision"], result=result,
                    )
                self._outbox_inflight = (execution_id, phase)
                break

    async def _upload_error_bundle_from_outbox(self, execution_id: str, payload: dict[str, Any]) -> None:
        """上传持久化失败现场；仅在 HTTP 成功后推进 outbox 到通知阶段。"""
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ValueError("Error bundle outbox result 必须是 object")
        try:
            error_id = await asyncio.to_thread(
                self.error_uploader.upload,
                execution_id,
                str(payload["task_name"]),
                int(payload["config_revision"]),
                result,
            )
        except Exception as exc:
            self._log(f"Error bundle upload failed: {type(exc).__name__}: {exc}")
            return
        if not error_id:
            self._log("Error bundle upload returned no error_id; will retry before task result")
            return
        result["error_id"] = error_id
        payload["result"] = result
        payload["phase"] = "error_bundle_notice"
        self._atomic_write_json(self._outbox_path(execution_id), payload)

    def _confirm_error_bundle_notice(self, message: dict[str, Any]) -> None:
        """收到 Server 通知 ACK 后，才允许同一执行继续提交补丁或最终结果。"""
        execution_id = self._required(message, "execution_id")
        payload = self._read_outbox(execution_id)
        if payload is None:
            return
        # ACK 必须同时回显 HTTP 上传得到的 error_id 和任务租约版本。只按
        # execution_id 推进会让旧连接或伪造 ACK 越过错误现场确认阶段。
        expected_revision = payload.get("config_revision")
        expected_result = payload.get("result")
        expected_error_id = expected_result.get("error_id") if isinstance(expected_result, dict) else None
        if message.get("config_revision") != expected_revision or message.get("error_id") != expected_error_id:
            self._log(f"Ignored invalid error bundle ACK for {execution_id}")
            return
        if execution_id in self._error_bundle_reassertions:
            # 重连重申只用于让已重启的 Server 恢复错误包确认；原 outbox 已经
            # 位于 schedule/result 阶段，不能再次倒回错误包上传流程。
            self._error_bundle_reassertions.discard(execution_id)
            if self._outbox_inflight == (execution_id, "error_bundle_reassert"):
                self._outbox_inflight = None
            return
        if payload.get("phase") != "error_bundle_notice":
            return
        if self._outbox_inflight == (execution_id, "error_bundle_notice"):
            self._outbox_inflight = None
        payload["phase"] = "schedule" if payload.get("patch") else "result"
        self._atomic_write_json(self._outbox_path(execution_id), payload)

    def _confirm_schedule_patch(self, message: dict[str, Any]) -> None:
        """确认新配置快照后，延后切换运行配置并推进结果阶段。

        schedule_patch 的确认发生在当前 OAS 子进程已经退出、但本次租约尚未
        收到 task_result ACK 的窗口。此时只能缓存 Server 返回的完整快照，不能
        覆盖刚完成任务使用的运行时文件；待最终 ACK 释放 execution 后，再激活
        该快照并基于它执行下一轮预检。
        """
        execution_id = self._required(message, "execution_id")
        accepted = message.get("accepted", True)
        if self._outbox_inflight == (execution_id, "schedule"):
            self._outbox_inflight = None
        config = message.get("config")
        revision = self._required_int(message, "config_revision")
        self._confirm_config(config, revision)
        if isinstance(config, dict):
            # 管理员并发保存配置会使补丁被拒绝；无论是否接受，响应中的快照
            # 都是 Server 当前权威版本，必须在当前租约结束后重新预检。
            self._latest_server_config = config
            if self.state.current_execution_id is not None:
                self._pending_preflight_config = config
        if not accepted:
            self._log(f"Schedule patch rejected for {execution_id}: {message.get('reason', '')}")
        path = self._outbox_path(execution_id)
        payload = self._read_outbox(execution_id)
        if payload is None:
            return
        payload["phase"] = "result"
        self._atomic_write_json(path, payload)

    def _confirm_result(self, message: dict[str, Any]) -> bool:
        """确认当前结果并返回是否真正释放了本地执行态。"""
        execution_id = self._required(message, "execution_id")
        payload = self._read_outbox(execution_id)
        expected_revision = (
            payload.get("config_revision")
            if payload is not None
            else self.state.current_config_revision
        )
        if not isinstance(expected_revision, int) or message.get("config_revision") != expected_revision:
            # 结果 ACK 也绑定租约版本；旧连接的 ACK 不能清理新会话可能正在
            # 使用的同编号状态。Server 的正式 ACK 始终回显该字段。
            self._log(f"Ignored invalid task result ACK for {execution_id}")
            return False
        if self._outbox_inflight and self._outbox_inflight[0] == execution_id:
            self._outbox_inflight = None
        self._outbox_path(execution_id).unlink(missing_ok=True)
        self._stop_file(execution_id).unlink(missing_ok=True)
        self._error_bundle_reassertions.discard(execution_id)
        if self.state.current_execution_id == execution_id:
            self.state.current_execution_id = None
            self.state.current_task_name = None
            self.state.current_config_revision = None
            self.state_store.save(self.state)
            # 当前任务结束后才允许把任务期间缓存的管理配置写入 OAS 运行时
            # JSON。这样下一份租约和随后的预检使用同一版本，不会覆盖正在
            # 结束的子进程所依赖的旧快照。
            self.config_store.activate_confirmed_runtime()
        return True

    def _confirm_config(self, config: Any, revision: Any) -> None:
        """只接受 revision 与实际快照一致的 Server 配置。"""
        if not isinstance(config, dict) or not isinstance(revision, int) or config.get("revision") != revision:
            raise ConfigSyncError("Server config snapshot 与 config_revision 不一致")
        confirmed = self.config_store.save_confirmed(
            config,
            # 当前任务必须继续使用其 task_lease 快照，而非 hello 或补丁 ACK
            # 中可能更高版本的管理员配置。刚收到但尚未启动的 pending lease
            # 同样需要保持该快照，不能被 heartbeat 的新配置覆盖。
            activate_runtime=self.state.current_execution_id is None and self.pending_lease is None,
        )
        self.state.confirmed_revision = confirmed
        self.state_store.save(self.state)

    async def _send_heartbeat(self) -> None:
        """上报本地运行态；没有执行编号即表示空闲或断线后任务已完成待回传。"""
        foreground_package = await asyncio.to_thread(self._foreground_package)
        await self._send(
            {
                "type": "heartbeat",
                "execution_id": self.state.current_execution_id,
                "current_task": self.state.current_task_name,
                "config_revision": self.state.current_config_revision or self.state.confirmed_revision,
                # 运行中租约可使用旧 revision；这个字段只表示已确认的最新快照。
                "cached_config_revision": self.state.confirmed_revision,
                "foreground_package": foreground_package,
            }
        )

    @classmethod
    def _foreground_package(cls) -> str | None:
        """通过本机 Root shell 读取当前前台包名；查询失败不阻塞心跳。"""
        try:
            output = cls._shell("dumpsys window windows")
            matched = re.search(r"mCurrentFocus=.*?\s([A-Za-z0-9._]+)/(?:[A-Za-z0-9._$]+)", output)
            if matched:
                return matched.group(1)
            output = cls._shell("dumpsys activity activities")
            matched = re.search(r"mResumedActivity:.*?\s([A-Za-z0-9._]+)/(?:[A-Za-z0-9._$]+)", output)
            return matched.group(1) if matched else None
        except (OSError, subprocess.TimeoutExpired):
            return None

    async def _report_preflight(self, config: Any) -> None:
        """在收到 Server 配置后执行本机 Root 与游戏环境预检，再允许启动租约。"""
        self._latest_server_config = config if isinstance(config, dict) else None
        self.health = await asyncio.to_thread(self._preflight, config)
        self._last_preflight_monotonic = asyncio.get_running_loop().time()
        await self._send({
            "type": "health_report",
            "config_revision": config.get("revision") if isinstance(config, dict) else None,
            "health": self.health,
        })

    def _preflight_recheck_due(self) -> bool:
        """只在不健康状态达到间隔时重试 Root/redroid 预检。"""
        return (
            asyncio.get_running_loop().time() - self._last_preflight_monotonic
            >= PREFLIGHT_RECHECK_SECONDS
        )

    def _preflight(self, config: Any) -> dict[str, Any]:
        """检查 RootShell 实际能力、游戏环境和固定 Agent 运行时。"""
        checks: dict[str, bool] = {
            "runtime": False,
            "state_root": False,
            "root": False,
            # ``uid=0`` 本身不代表 SELinux/裁剪镜像允许截图和自动化输入；
            # 这些命令均在本机探测，预检不会发送截图或执行点击/滑动。
            "root_screenshot": False,
            "root_input": False,
            "root_hierarchy": False,
            "screen": False,
            "game_package": False,
            "python_dependencies": False,
            "ocr_models": False,
        }
        reasons: list[str] = []
        try:
            checks["runtime"] = all((self.settings.runtime_root / name).exists() for name in ("module", "tasks", "assets"))
            if not checks["runtime"]:
                reasons.append("OAS runtime 缺少 module、tasks 或 assets")
            dependency_issues = self._runtime_dependency_issues()
            checks["python_dependencies"] = not dependency_issues
            if dependency_issues:
                reasons.append(f"Python 运行时依赖不可用: {', '.join(dependency_issues[:3])}")
            ocr_issues = self._ocr_model_issues()
            checks["ocr_models"] = not ocr_issues
            if ocr_issues:
                reasons.append(f"OCR 模型资源不完整: {', '.join(ocr_issues[:3])}")
            self.settings.state_root.mkdir(parents=True, exist_ok=True)
            probe = self.settings.state_root / ".write-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            checks["state_root"] = True
        except OSError as exc:
            reasons.append(f"Agent state directory 不可写: {exc}")
        device = config.get("device") if isinstance(config, dict) else None
        if not isinstance(device, dict):
            return {"status": "unhealthy", "reason": "Server 未返回有效 device 配置", "checks": checks}
        package_name = str(device.get("game_package") or "")
        screen_size = device.get("screen_size")
        try:
            root_identity = self._shell("id")
            checks["root"] = "uid=0" in root_identity
            if not checks["root"]:
                reasons.append("Root shell id 未返回 uid=0")
            screen_output = self._shell("wm size")
            matches = re.findall(r"(?:Physical size|Override size):\s*(\d+)x(\d+)", screen_output)
            if matches and isinstance(screen_size, list) and len(screen_size) == 2:
                expected = (int(screen_size[0]), int(screen_size[1]))
                checks["screen"] = any(
                    (int(width), int(height)) in {expected, expected[::-1]}
                    for width, height in matches
                )
            if not checks["screen"]:
                reasons.append("设备分辨率与 Server 配置不一致")
            checks["game_package"] = bool(package_name and self._shell(f"cmd package path {package_name}").strip())
            if not checks["game_package"]:
                reasons.append("游戏包未安装或不可查询")
        except (OSError, subprocess.TimeoutExpired) as exc:
            reasons.append(f"Root shell 预检失败: {exc}")
        if checks["root"]:
            try:
                screenshot = self._shell_bytes("screencap -p")
                checks["root_screenshot"] = screenshot.startswith(b"\x89PNG\r\n\x1a\n")
                if not checks["root_screenshot"]:
                    reasons.append("Root shell screencap 未返回 PNG")

                input_help = self._shell("input help")
                checks["root_input"] = all(
                    re.search(rf"\b{command}\b", input_help) is not None
                    for command in ("tap", "swipe", "motionevent")
                )
                if not checks["root_input"]:
                    reasons.append("Root shell input 缺少 tap、swipe 或 motionevent")

                hierarchy = self._shell(
                    "uiautomator dump --compressed "
                    f"{ROOT_PREFLIGHT_HIERARCHY_PATH} >/dev/null; status=$?; "
                    f"if [ -f {ROOT_PREFLIGHT_HIERARCHY_PATH} ]; then "
                    f"cat {ROOT_PREFLIGHT_HIERARCHY_PATH}; rm -f {ROOT_PREFLIGHT_HIERARCHY_PATH}; fi; "
                    "exit $status"
                )
                checks["root_hierarchy"] = "<?xml" in hierarchy
                if not checks["root_hierarchy"]:
                    reasons.append("Root shell uiautomator 未返回 XML")
            except (OSError, subprocess.TimeoutExpired) as exc:
                reasons.append(f"RootShell 截图/输入/UI 预检失败: {exc}")
        healthy = all(checks.values())
        return {"status": "healthy" if healthy else "unhealthy", "reason": "; ".join(reasons) or None, "checks": checks}

    @staticmethod
    def _runtime_dependency_issues() -> list[str]:
        """导入 Agent/OAS 所需模块，提前识别 Android ABI 或镜像裁剪问题。"""
        issues: list[str] = []
        for module_name in REQUIRED_RUNTIME_MODULES:
            try:
                importlib.import_module(module_name)
            except Exception as exc:
                # 异常名称足以用于管理页定位；不上传完整本地堆栈或环境路径。
                issues.append(f"{module_name}:{type(exc).__name__}")
        return issues

    @staticmethod
    def _ocr_model_issues() -> list[str]:
        """读取镜像内 OCR 模型完整性，不触发下载或修复。"""
        try:
            from module.ocr.rpc import _bundled_ocr_model_issues

            return _bundled_ocr_model_issues()
        except (ImportError, OSError, RuntimeError) as exc:
            return [f"OCR 资源检查失败: {exc}"]

    @staticmethod
    def _shell(command: str) -> str:
        """以 Agent 本机 Root shell 执行预检；任务期操作仍由 RootShell 后端负责。"""
        return AndroidAgent._shell_bytes(command).decode("utf-8", errors="replace")

    @staticmethod
    def _shell_bytes(command: str) -> bytes:
        """执行预检命令并保留二进制输出，供本地 screencap 格式校验。"""
        completed = subprocess.run(
            root_shell_command(command),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
        )
        if completed.returncode != 0:
            raise OSError(completed.stderr.decode("utf-8", errors="replace").strip() or command)
        return completed.stdout

    async def _send_result(
        self,
        execution_id: str,
        status: str,
        summary: str,
        config_revision: int,
        *,
        result: dict[str, Any] | None = None,
    ) -> None:
        """构造任务结果；仅转发已定义的运行元数据，Server 通过执行编号去重。"""
        payload: dict[str, Any] = {
            "type": "task_result",
            "execution_id": execution_id,
            "config_revision": config_revision,
            # 任务租约可能仍使用旧 revision；该字段表示 Agent 已确认的最新
            # 快照，使 Server 无需等待下一次心跳即可刷新同步状态。
            "cached_config_revision": self.state.confirmed_revision,
            "status": status,
            "summary": summary,
        }
        if isinstance(result, dict):
            for key in ("started_at", "finished_at", "duration_seconds", "error_id"):
                if key in result:
                    payload[key] = result[key]
        await self._send(
            payload
        )

    async def _send(self, payload: dict[str, Any]) -> None:
        """为每个 Agent 上行消息补齐公共鉴权与身份字段。"""
        if self.websocket is None:
            return
        async with self._send_lock:
            websocket = self.websocket
            if websocket is None:
                return
            self.sequence += 1
            message = {
                # 调用方只提供业务字段；即使未来误传同名键，下面的 Agent
                # 信封仍以本地状态为准，不能伪造设备身份、序列或共享密钥。
                **payload,
                "device_id": self.settings.device_id,
                "sent_at": datetime.now().isoformat(timespec="milliseconds"),
                "sequence": self.sequence,
                "secret": self.settings.shared_secret,
            }
            await websocket.send(json.dumps(message, ensure_ascii=False))

    def _observe_server_sequence(self, message: dict[str, Any]) -> None:
        """校验当前 WebSocket 会话中的 Server 消息顺序，避免重放旧命令。"""
        sequence = message.get("sequence")
        sent_at = message.get("sent_at")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValueError("Server message sequence must be a non-negative integer")
        if not isinstance(sent_at, str) or not sent_at.strip():
            raise ValueError("Server message sent_at must be a non-empty string")
        if sequence <= self.last_server_sequence:
            raise ValueError("Server message sequence must increase strictly")
        self.last_server_sequence = sequence

    def _load_result(self, result_path: Path, lease: PendingLease) -> dict[str, Any]:
        """读取子进程结果；子进程崩溃时构造失败结果。"""
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
            # ``manual_required`` 表示 OAS 已调用 exit() 请求人工接管，不能
            # 降级成普通 failure，否则 Server 会把仍到期的任务再次自动派发。
            if result.get("status") in {"success", "failure", "cancelled", "manual_required"}:
                return result
        except (OSError, json.JSONDecodeError):
            pass
        return self._subprocess_failure_result(
            lease,
            f"Task subprocess did not produce a valid result: {lease.task_name}",
        )

    def _subprocess_failure_result(self, lease: PendingLease, summary: str) -> dict[str, Any]:
        """把任务运行器层失败升级为人工接管，并暂停当前任务调度。"""
        disabled = self.config_store.disable_task_in_runtime(lease.task_name)
        suffix = "; task_disabled" if disabled else ""
        return {"status": "manual_required", "summary": f"{summary}{suffix}", "runner_failure": True}

    def _outbox_path(self, execution_id: str) -> Path:
        return self.settings.state_root / "outbox" / f"{self._validated_execution_id(execution_id)}.json"

    def _stop_file(self, execution_id: str) -> Path:
        return self.settings.state_root / "stops" / f"{self._validated_execution_id(execution_id)}.stop"

    def _read_outbox(self, execution_id: str) -> dict[str, Any] | None:
        """只接受当前执行编号的完整 outbox，损坏文件交由恢复路径替换。"""
        try:
            payload = json.loads(self._outbox_path(execution_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        if not isinstance(payload, dict) or payload.get("execution_id") != execution_id:
            return None
        if payload.get("phase") not in {"error_bundle", "error_bundle_notice", "schedule", "result"}:
            return None
        return payload

    def _recover_corrupted_current_outbox(self, execution_id: str) -> None:
        """用失败结果覆盖当前任务的半写 outbox，确保租约最终可释放。"""
        if execution_id != self.state.current_execution_id:
            return
        task_name = self.state.current_task_name
        revision = self.state.current_config_revision
        if not task_name or not isinstance(revision, int):
            return
        self._write_outbox(
            PendingLease(execution_id, task_name, revision, {}),
            {"status": "failure", "summary": "Agent outbox was corrupted before task result was persisted"},
            None,
        )
        self._log(f"Recovered corrupted outbox for {execution_id} as failure")

    @staticmethod
    def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
        """先 fsync 临时文件再替换，避免 Agent 重启留下半截 outbox。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
                json.dump(payload, file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_name, path)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise

    def _websocket_url(self) -> str:
        return self.settings.server_url + "/agent/ws"

    @staticmethod
    def _required(message: dict[str, Any], key: str) -> str:
        value = str(message.get(key) or "").strip()
        if not value:
            raise ValueError(f"Server message missing {key}")
        return value

    @staticmethod
    def _required_int(message: dict[str, Any], key: str) -> int:
        value = message.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"Server message {key} must be an integer")
        return value

    @staticmethod
    def _parse_lease_expiry(value: Any) -> datetime:
        """解析 Server 发送的带 UTC 偏移绝对租约过期时间。

        redroid 可能运行 UTC，而 Server 常使用业务本地时区。租约必须携带
        时区偏移，Agent 统一换算到 UTC 后再比较，不能把无偏移字符串当作
        本地时间使用，否则跨时区部署会提前拒绝或延长任务租约。
        """
        text = str(value or "").strip()
        if not text:
            raise ValueError("Server message expires_at must be a non-empty ISO timestamp")
        try:
            expires_at = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("Server message expires_at must be an ISO timestamp") from exc
        if expires_at.tzinfo is None:
            raise ValueError("Server message expires_at must include a timezone offset")
        return expires_at.astimezone(timezone.utc)

    @staticmethod
    def _lease_is_expired(expires_at: datetime) -> bool:
        """按租约携带的时区比较过期时间，并兼容进程内旧状态恢复。

        新收租约一定是 UTC aware datetime。状态目录不会持久化 PendingLease；
        此处保留 naive 分支仅用于升级前进程内已存在的对象，避免将两类 datetime
        直接比较而抛出 TypeError。
        """
        if expires_at.tzinfo is None:
            return expires_at <= datetime.now()
        return expires_at <= datetime.now(timezone.utc)

    @staticmethod
    def _validated_execution_id(execution_id: str) -> str:
        """限制执行编号为文件名安全字符，防止状态文件逃逸出 Agent 目录。"""
        value = str(execution_id or "").strip()
        if not EXECUTION_ID_PATTERN.fullmatch(value):
            raise ValueError("execution_id must contain only letters, digits and hyphens")
        return value

    @staticmethod
    def _log(message: str) -> None:
        """保持 Agent 常驻进程日志简单，错误现场由 OAS 本地日志提供。"""
        print(f"[{datetime.now().isoformat(timespec='seconds')}] {message}", flush=True)


def main() -> int:
    """加载镜像环境变量并启动常驻 Agent。"""
    settings = AgentSettings.from_environment()
    asyncio.run(AndroidAgent(settings).run_forever())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
