"""失败时从设备本地 OAS 日志收集并上传错误现场。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from .config import AgentSettings
from module.handler.sensitive_info import handle_sensitive_logs, handle_sensitive_text


MAX_UPLOAD_TOTAL_BYTES = 28 * 1024 * 1024
MAX_UPLOAD_FILE_BYTES = 19 * 1024 * 1024
MAX_FALLBACK_LOG_BYTES = 512 * 1024


class ErrorBundleUploader:
    """只在失败时上传截图、日志和脱敏上下文；正常截图不走网络。"""

    def __init__(self, settings: AgentSettings) -> None:
        self.settings = settings

    def upload(
        self,
        execution_id: str,
        task_name: str,
        config_revision: int,
        result: dict[str, Any],
    ) -> str | None:
        """找到最新本地错误目录后 HTTPS 上传；上传失败留在本地等待人工取证。"""
        error_root = self.settings.runtime_root / "log" / "error"
        # 单任务运行器把 logger 固定为 ``agent-<device_id>``。除任务开始时间
        # 外，再把错误目录限定为这台 Agent 的日志前缀，避免镜像里遗留的其它
        # 配置/人工调试错误目录被当作本次任务截图上传到 Server。
        source = self._latest_error_dir(
            error_root,
            result.get("started_at"),
            f"agent-{self.settings.device_id}",
        )
        metadata = self._sanitize_metadata({
            "device_id": self.settings.device_id,
            "execution_id": execution_id,
            "task_name": task_name,
            # Server 用任务租约版本把 HTTP 错误包绑定到当前执行，避免仅凭
            # 共享密钥向任意 execution_id 上传截图或设备日志。
            "config_revision": config_revision,
            "agent_version": self.settings.agent_version,
            "result": {key: value for key, value in result.items() if key != "traceback"},
        })
        files = self._bundle_files(source)
        if not any(item[1][0] == "log.txt" for item in files):
            # OAS 有些子进程失败发生在保存 error 目录之前；此时仍要保留本设备
            # 当前任务的日志尾部，且不能拿旧错误目录的截图补数。
            files.append((
                "files",
                ("log.txt", self._task_log_tail(self._started_timestamp(result.get("started_at"))), "text/plain"),
            ))
        url = self._http_base_url() + f"/agent/error-bundles/{self.settings.device_id}/{execution_id}"
        response = requests.post(
            url,
            data={"metadata": json.dumps(metadata, ensure_ascii=False)},
            files=files,
            headers={"x-oas-agent-secret": self.settings.shared_secret},
            timeout=45,
        )
        response.raise_for_status()
        return str(response.json().get("error_id") or "") or None

    def _bundle_files(self, source: Path | None) -> list[tuple[str, tuple[str, bytes, str]]]:
        """读取本次错误目录内受大小限制的截图、日志和上下文文件。"""
        if source is None:
            return []
        files: list[tuple[str, tuple[str, bytes, str]]] = []
        total_size = 0
        try:
            paths = sorted(source.iterdir())
        except OSError:
            return files
        for path in paths:
            # 错误现场只能来自 Agent 自己生成的普通文件；不跟随符号链接，
            # 防止任务代码把容器外的敏感文件伪装成截图或日志上传。
            if path.is_symlink() or not path.is_file() or path.suffix.lower() not in {".png", ".txt", ".json"}:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > MAX_UPLOAD_FILE_BYTES or total_size + size > MAX_UPLOAD_TOTAL_BYTES:
                continue
            content_type = {
                ".png": "image/png",
                ".json": "application/json",
                ".txt": "text/plain",
            }[path.suffix.lower()]
            try:
                content = path.read_bytes()
            except OSError:
                continue
            if path.suffix.lower() == ".txt":
                # 即使任务异常早于 save_error_log，上传的文本也须走统一脱敏。
                content = self._sanitize_log_bytes(content)
            elif path.suffix.lower() == ".json":
                content = self._sanitize_json_bytes(content)
            # 非法 UTF-8 经 errors=replace 后可能膨胀为多个字节；必须按清洗
            # 后的实际 payload 重新检查边界，否则 Agent 会先认为上传成功，
            # Server 再因大小限制拒绝，导致错误 outbox 永久重试。
            sanitized_size = len(content)
            if (
                sanitized_size > MAX_UPLOAD_FILE_BYTES
                or total_size + sanitized_size > MAX_UPLOAD_TOTAL_BYTES
            ):
                continue
            files.append(("files", (path.name, content, content_type)))
            total_size += sanitized_size
        return files

    @staticmethod
    def _latest_error_dir(error_root: Path, started_at: Any, config_name: str) -> Path | None:
        """只采用本 Agent 当前任务写入的错误目录，杜绝误传其它截图。"""
        if not error_root.exists():
            return None
        started_timestamp = ErrorBundleUploader._started_timestamp(started_at)
        if started_timestamp is None:
            return None
        expected_prefix = f"{config_name}_"
        now_timestamp = datetime.now().timestamp()
        try:
            candidates: list[tuple[Path, float]] = []
            for path in error_root.iterdir():
                # 目录本身也不能是链接；否则后续 iterdir 会越出 Agent 的
                # runtime/log/error 范围，读取到不属于当前任务的现场。
                if path.is_symlink() or not path.is_dir() or not path.name.startswith(expected_prefix):
                    continue
                directory_timestamp_ms = ErrorBundleUploader._error_dir_timestamp(
                    path.name,
                    expected_prefix,
                )
                if directory_timestamp_ms is None:
                    continue
                # OAS 以 ``<config_name>_<timestamp_ms>`` 创建错误目录。以目录名
                # 中的生成时间绑定本次任务，不能仅依赖目录 mtime；旧目录可能被
                # 人工浏览或日志写入触碰，mtime 因而不具备执行归属语义。
                # 错误目录名只有毫秒精度，而 task_runner 的 started_at 含微秒。
                # 统一转换成整数毫秒后，同一毫秒内启动并失败的截图会被保留，
                # 但前一毫秒的历史目录仍严格拒绝；不能用浮点容差放宽边界。
                started_timestamp_ms = int(started_timestamp * 1000)
                now_timestamp_ms = int((now_timestamp + 5) * 1000)
                if directory_timestamp_ms < started_timestamp_ms or directory_timestamp_ms > now_timestamp_ms:
                    continue
                try:
                    modified_at = path.stat().st_mtime
                except OSError:
                    continue
                # 同时拒绝未来时间目录，避免异常时钟或人为触碰令尚未发生的
                # 错误现场抢占本次上传；较旧 mtime 则允许，任务可持续较久。
                if modified_at > now_timestamp + 5:
                    continue
                candidates.append((path, modified_at))
        except OSError:
            # 错误现场采集不能因临时目录读取失败影响本地结果 outbox；之后会
            # 回退到当前 Agent 的脱敏任务日志尾部。
            return None
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[1])[0]

    @staticmethod
    def _error_dir_timestamp(name: str, expected_prefix: str) -> int | None:
        """解析 OAS 错误目录的毫秒时间戳，格式不符时拒绝采用该目录。"""
        suffix = name.removeprefix(expected_prefix)
        if not suffix.isdigit():
            return None
        try:
            return int(suffix)
        except ValueError:
            return None

    @staticmethod
    def _started_timestamp(value: Any) -> float | None:
        """解析任务启动时间；无时间戳时宁可不采用任何旧截图。"""
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return None

    def _task_log_tail(self, started_timestamp: float | None = None) -> bytes:
        """读取本次任务的 OAS 日志尾部；缺失或过期时上传明确占位文本。"""
        log_root = self.settings.runtime_root / "log"
        config_name = f"agent-{self.settings.device_id}"
        candidates: list[tuple[Path, float]] = []
        try:
            for path in log_root.glob(f"*_{config_name}.txt"):
                if path.is_symlink() or not path.is_file():
                    continue
                try:
                    modified_at = path.stat().st_mtime
                except OSError:
                    continue
                # 如果当前任务尚未写入日志，不能把上一次任务的尾部当作本次现场。
                if started_timestamp is not None and modified_at < started_timestamp:
                    continue
                candidates.append((path, modified_at))
        except OSError:
            candidates = []
        if not candidates:
            return b"No local OAS task log was available when this failure was reported.\n"
        log_path = max(candidates, key=lambda item: item[1])[0]
        try:
            with log_path.open("rb") as file:
                file.seek(max(0, log_path.stat().st_size - MAX_FALLBACK_LOG_BYTES))
                return self._sanitize_log_bytes(file.read())
        except OSError:
            return b"No local OAS task log was available when this failure was reported.\n"

    @staticmethod
    def _sanitize_log_bytes(content: bytes) -> bytes:
        """复用 OAS 日志脱敏规则，处理截断 UTF-8 时不抛出上传异常。"""
        text = content.decode("utf-8", errors="replace")
        return "".join(handle_sensitive_logs(text.splitlines(keepends=True))).encode("utf-8")

    @classmethod
    def _sanitize_json_bytes(cls, content: bytes) -> bytes:
        """清理错误上下文 JSON 内的路径文本；无效 JSON 也不能绕过脱敏。"""
        text = content.decode("utf-8", errors="replace")
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            # 保留可读的损坏内容便于排错，但转为一个合法 JSON object，确保
            # Server 的任务上下文读取接口不会把未清洗原文再次展示出来。
            value = {"invalid_json_text": handle_sensitive_text(text)}
        return json.dumps(cls._sanitize_metadata(value), ensure_ascii=False).encode("utf-8")

    @classmethod
    def _sanitize_metadata(cls, value: Any) -> Any:
        """递归清理待上传元数据中的文本，不改变结构化排错字段。"""
        if isinstance(value, str):
            return handle_sensitive_text(value)
        if isinstance(value, list):
            return [cls._sanitize_metadata(item) for item in value]
        if isinstance(value, dict):
            return {str(key): cls._sanitize_metadata(item) for key, item in value.items()}
        return value

    def _http_base_url(self) -> str:
        """把 WSS 服务地址转换为同一 Server 的 HTTPS 上传地址。"""
        base = self.settings.server_url
        if base.startswith("wss://"):
            return "https://" + base.removeprefix("wss://")
        return "http://" + base.removeprefix("ws://")
