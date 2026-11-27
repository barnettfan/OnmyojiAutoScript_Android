"""Android Agent 启动配置与本地已确认状态。"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

from . import AGENT_VERSION


DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
EXECUTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,127}$")
TASK_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,63}$")


class AgentConfigurationError(ValueError):
    """Agent 环境变量或本地状态不可用。"""


@dataclass(frozen=True)
class AgentSettings:
    """只包含 Agent 自己所需的连接与运行时路径，不复制 Server 设备 JSON。"""

    device_id: str
    server_url: str
    shared_secret: str
    runtime_root: Path
    state_root: Path
    agent_version: str
    resource_version: str
    heartbeat_seconds: int

    @classmethod
    def from_environment(cls) -> "AgentSettings":
        """从镜像注入的环境变量加载配置，默认拒绝明文 WebSocket。"""
        device_id = os.environ.get("OAS_AGENT_DEVICE_ID", "").strip()
        server_url = os.environ.get("OAS_AGENT_SERVER_URL", "").strip().rstrip("/")
        shared_secret = os.environ.get("OAS_AGENT_SHARED_SECRET", "")
        runtime_root_text = os.environ.get("OAS_AGENT_RUNTIME_ROOT", "/opt/oas").strip()
        state_root_text = os.environ.get("OAS_AGENT_STATE_ROOT", "/var/lib/oas-agent").strip()
        runtime_root = Path(runtime_root_text)
        state_root = Path(state_root_text)
        agent_version = os.environ.get("OAS_AGENT_VERSION", AGENT_VERSION).strip()
        resource_version = os.environ.get("OAS_AGENT_RESOURCE_VERSION", agent_version).strip()
        raw_heartbeat_seconds = os.environ.get("OAS_AGENT_HEARTBEAT_SECONDS", "15").strip()
        parsed = urlparse(server_url)
        allow_insecure = os.environ.get("OAS_AGENT_ALLOW_INSECURE_WS", "").lower() == "true"
        if not device_id or not server_url or not shared_secret:
            raise AgentConfigurationError("必须设置 OAS_AGENT_DEVICE_ID、OAS_AGENT_SERVER_URL 和 OAS_AGENT_SHARED_SECRET")
        # device_id 会参与 Agent 本地配置、outbox 与停止标记的文件名；与 Server
        # 保持相同的受限格式，不能允许部署环境的误配写出状态根目录。
        if not DEVICE_ID_PATTERN.fullmatch(device_id):
            raise AgentConfigurationError("OAS_AGENT_DEVICE_ID 只能包含字母、数字和连字符，且长度为 1-64")
        if parsed.scheme not in ({"wss", "ws"} if allow_insecure else {"wss"}) or not parsed.netloc or not parsed.hostname:
            raise AgentConfigurationError("OAS_AGENT_SERVER_URL 必须是 wss:// URL；仅显式设置 OAS_AGENT_ALLOW_INSECURE_WS=true 时允许 ws://")
        if parsed.username or parsed.password:
            raise AgentConfigurationError("OAS_AGENT_SERVER_URL 不能包含用户名或密码")
        # Agent 会固定追加 ``/agent/ws`` 以及错误包上传路径。允许部署地址
        # 自带 path 会造成 ``/agent/ws/agent/ws`` 这类静默错误请求，因而首版
        # 协议只接受 Server 根地址；反向代理请在域名/端口层完成路由。
        if parsed.path not in ("", "/"):
            raise AgentConfigurationError("OAS_AGENT_SERVER_URL 只能是 Server 根地址，不能包含 path")
        if parsed.query or parsed.fragment:
            raise AgentConfigurationError("OAS_AGENT_SERVER_URL 不能包含 query 或 fragment")
        try:
            parsed.port
        except ValueError as exc:
            raise AgentConfigurationError("OAS_AGENT_SERVER_URL 的端口无效") from exc
        if not runtime_root.exists():
            raise AgentConfigurationError(f"OAS 运行时目录不存在: {runtime_root}")
        # init 启动器会把运行时固定在镜像内；状态必须落到部署显式提供的绝对
        # 持久目录。空字符串会被 ``Path`` 解释成当前目录，若不拦截会使 outbox
        # 与密钥挂载目录或 OAS 代码目录混在一起。
        # ``Path('/var/...').is_absolute()`` is false when the test suite is
        # executed on Windows, even though it is an absolute Android path.
        # Accept the POSIX-root form explicitly; the production launcher runs
        # on Android/Linux and still rejects every relative value.
        runtime_root_absolute = runtime_root.is_absolute() or runtime_root_text.startswith("/")
        state_root_absolute = state_root.is_absolute() or state_root_text.startswith("/")
        if not runtime_root_text or not runtime_root_absolute:
            raise AgentConfigurationError("OAS_AGENT_RUNTIME_ROOT 必须是非空绝对路径")
        if not state_root_text or not state_root_absolute:
            raise AgentConfigurationError("OAS_AGENT_STATE_ROOT 必须是非空绝对路径")
        try:
            heartbeat_seconds = int(raw_heartbeat_seconds)
        except ValueError as exc:
            raise AgentConfigurationError("OAS_AGENT_HEARTBEAT_SECONDS 必须是整数") from exc
        if heartbeat_seconds < 5:
            raise AgentConfigurationError("OAS_AGENT_HEARTBEAT_SECONDS 不能小于 5")
        return cls(device_id, server_url, shared_secret, runtime_root, state_root, agent_version, resource_version, heartbeat_seconds)


@dataclass
class AgentState:
    """只缓存 Server 已确认的配置版本和当前已领取租约。"""

    confirmed_revision: int | None = None
    current_execution_id: str | None = None
    current_task_name: str | None = None
    # 当前租约固定使用的 Server 配置版本，不能被运行中收到的新快照覆盖。
    current_config_revision: int | None = None


class StateStore:
    """原子持久化 Agent 的小型状态文件，容器短暂重启后可对齐 Server。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "state.json"

    def load(self) -> AgentState:
        """读取历史状态；损坏的缓存不作为调度依据，直接从空状态恢复。"""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            confirmed_revision = data.get("confirmed_revision")
            current_execution_id = data.get("current_execution_id")
            current_task_name = data.get("current_task_name")
            current_config_revision = data.get("current_config_revision")
            if confirmed_revision is not None and (
                isinstance(confirmed_revision, bool) or not isinstance(confirmed_revision, int) or confirmed_revision < 1
            ):
                raise ValueError("confirmed_revision is invalid")
            if current_execution_id is not None and (
                not isinstance(current_execution_id, str) or not EXECUTION_ID_PATTERN.fullmatch(current_execution_id)
            ):
                raise ValueError("current_execution_id is invalid")
            if current_task_name is not None and (
                not isinstance(current_task_name, str) or not TASK_NAME_PATTERN.fullmatch(current_task_name)
            ):
                raise ValueError("current_task_name is invalid")
            if current_config_revision is not None and (
                isinstance(current_config_revision, bool)
                or not isinstance(current_config_revision, int)
                or current_config_revision < 1
            ):
                raise ValueError("current_config_revision is invalid")
            if (current_execution_id is None) != (current_task_name is None):
                raise ValueError("current execution state is incomplete")
            return AgentState(
                confirmed_revision=confirmed_revision,
                current_execution_id=current_execution_id,
                current_task_name=current_task_name,
                current_config_revision=current_config_revision,
            )
        except (OSError, json.JSONDecodeError, ValueError):
            return AgentState()

    def save(self, state: AgentState) -> None:
        """以临时文件替换，避免断电留下半截 JSON。"""
        self.root.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".state.", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(asdict(state), file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_name, self.path)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
