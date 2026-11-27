"""Agent 错误现场上传边界的回归测试。"""
from __future__ import annotations

import tempfile
import unittest
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from oas_android_agent.config import AgentSettings
from oas_android_agent.error_bundle import ErrorBundleUploader


class _Response:
    """模拟 requests 的成功响应，避免测试访问网络。"""

    def raise_for_status(self) -> None:
        """模拟成功的 HTTP 状态。"""

    def json(self) -> dict[str, str]:
        """返回 Server 分配的错误目录标识。"""
        return {"error_id": "redroid-01_1"}


class ErrorBundleUploaderTest(unittest.TestCase):
    """验证正常截图不上传，失败日志始终能够归档。"""

    def setUp(self) -> None:
        """创建包含设备本地 runtime 的临时 Agent 配置。"""
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.runtime_root = root / "runtime"
        self.settings = AgentSettings(
            device_id="redroid-01",
            server_url="wss://oas.example.test",
            shared_secret="test-secret",
            runtime_root=self.runtime_root,
            state_root=root / "state",
            agent_version="2026.11.26",
            resource_version="2026.11.26",
            heartbeat_seconds=15,
        )
        self.uploader = ErrorBundleUploader(self.settings)

    def tearDown(self) -> None:
        """清理临时 runtime 目录。"""
        self.tempdir.cleanup()

    @patch("oas_android_agent.error_bundle.requests.post", return_value=_Response())
    def test_stale_error_screenshot_is_not_uploaded_and_current_log_is_used(self, post) -> None:
        """错误目录早于任务启动时，不能泄露历史截图。"""
        stale_stamp = int((datetime.now() - timedelta(minutes=10)).timestamp() * 1000)
        stale = self.runtime_root / "log" / "error" / f"agent-redroid-01_{stale_stamp}"
        stale.mkdir(parents=True)
        (stale / "old.png").write_bytes(b"old-screenshot")
        old_time = (datetime.now() - timedelta(minutes=10)).timestamp()
        __import__("os").utime(stale, (old_time, old_time))
        log = self.runtime_root / "log" / "2026-11-26_agent-redroid-01.txt"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text('File "/data/oas/OnmyojiAutoScript-Self/script.py" current task log', encoding="utf-8")

        error_id = self.uploader.upload(
            "execution-1", "Restart", 7,
            {"started_at": (datetime.now() - timedelta(seconds=1)).isoformat()}
        )

        self.assertEqual(error_id, "redroid-01_1")
        self.assertEqual(
            post.call_args.args[0],
            "https://oas.example.test/agent/error-bundles/redroid-01/execution-1",
        )
        files = post.call_args.kwargs["files"]
        self.assertEqual([item[1][0] for item in files], ["log.txt"])
        self.assertEqual(files[0][1][1], b'File "C:\\fakepath\\OnmyojiAutoScript-Self/script.py" current task log')

    @patch("oas_android_agent.error_bundle.requests.post", return_value=_Response())
    def test_touched_stale_error_directory_is_not_uploaded(self, post) -> None:
        """旧目录即使刚被触碰，也不能靠 mtime 冒充当前任务的错误现场。"""
        old_stamp = int((datetime.now() - timedelta(minutes=10)).timestamp() * 1000)
        stale = self.runtime_root / "log" / "error" / f"agent-redroid-01_{old_stamp}"
        stale.mkdir(parents=True)
        (stale / "old.png").write_bytes(b"old-screenshot")
        local_log = self.runtime_root / "log" / "2026-11-26_agent-redroid-01.txt"
        local_log.parent.mkdir(parents=True, exist_ok=True)
        local_log.write_text("current task log", encoding="utf-8")

        self.uploader.upload(
            "execution-touched-stale", "Restart", 7,
            {"started_at": (datetime.now() - timedelta(seconds=1)).isoformat()},
        )

        files = post.call_args.kwargs["files"]
        self.assertEqual([item[1][0] for item in files], ["log.txt"])
        self.assertEqual(files[0][1][1], b"current task log")

    @patch("oas_android_agent.error_bundle.requests.post", return_value=_Response())
    def test_other_runtime_error_directory_is_not_uploaded(self, post) -> None:
        """即使其它配置的错误目录更新，也不能带出其截图或日志。"""
        foreign = self.runtime_root / "log" / "error" / "manual-debug_1"
        foreign.mkdir(parents=True)
        (foreign / "foreign.png").write_bytes(b"unrelated-screenshot")
        (foreign / "log.txt").write_text("unrelated task log", encoding="utf-8")
        local_log = self.runtime_root / "log" / "2026-11-26_agent-redroid-01.txt"
        local_log.parent.mkdir(parents=True, exist_ok=True)
        local_log.write_text("current agent failure", encoding="utf-8")

        self.uploader.upload(
            "execution-foreign", "Restart", 7,
            {"started_at": (datetime.now() - timedelta(seconds=1)).isoformat()}
        )

        files = post.call_args.kwargs["files"]
        self.assertEqual([item[1][0] for item in files], ["log.txt"])
        self.assertEqual(files[0][1][1], b"current agent failure")

    @patch("oas_android_agent.error_bundle.requests.post", return_value=_Response())
    def test_future_touched_error_directory_is_not_uploaded(self, post) -> None:
        """被重新触碰到未来时间的旧目录不能冒充本次任务截图。"""
        current_stamp = int(datetime.now().timestamp() * 1000)
        future = self.runtime_root / "log" / "error" / f"agent-redroid-01_{current_stamp}"
        future.mkdir(parents=True)
        (future / "old.png").write_bytes(b"old-screenshot")
        future_timestamp = (datetime.now() + timedelta(hours=1)).timestamp()
        os.utime(future, (future_timestamp, future_timestamp))
        log = self.runtime_root / "log" / "2026-11-26_agent-redroid-01.txt"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("current agent failure", encoding="utf-8")

        self.uploader.upload(
            "execution-future", "Restart", 7,
            {"started_at": (datetime.now() - timedelta(seconds=1)).isoformat()}
        )

        files = post.call_args.kwargs["files"]
        self.assertEqual([item[1][0] for item in files], ["log.txt"])
        self.assertEqual(files[0][1][1], b"current agent failure")

    @patch("oas_android_agent.error_bundle.requests.post", return_value=_Response())
    def test_stale_task_log_is_not_uploaded_as_current_log(self, post) -> None:
        """当前任务没有写日志时，不应上传上一任务的日志尾部。"""
        log = self.runtime_root / "log" / "2026-11-26_agent-redroid-01.txt"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("previous task context", encoding="utf-8")
        old_time = (datetime.now() - timedelta(minutes=10)).timestamp()
        os.utime(log, (old_time, old_time))

        self.uploader.upload(
            "execution-stale-log", "Restart", 7, {"started_at": datetime.now().isoformat()}
        )

        files = post.call_args.kwargs["files"]
        self.assertEqual([item[1][0] for item in files], ["log.txt"])
        self.assertIn(b"No local OAS task log was available", files[0][1][1])

    @patch("oas_android_agent.error_bundle.requests.post", return_value=_Response())
    def test_symlink_files_are_not_uploaded(self, post) -> None:
        """错误目录中的符号链接不能把目录外文件带入错误包。"""
        current_stamp = int(datetime.now().timestamp() * 1000)
        current = self.runtime_root / "log" / "error" / f"agent-redroid-01_{current_stamp}"
        current.mkdir(parents=True)
        outside = self.runtime_root / "private.txt"
        outside.write_text("private data", encoding="utf-8")
        link = current / "private.txt"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"filesystem does not support symlink creation: {exc}")

        self.uploader.upload(
            "execution-link", "Restart", 7, {"started_at": datetime.now().isoformat()}
        )

        files = post.call_args.kwargs["files"]
        self.assertEqual([item[1][0] for item in files], ["log.txt"])

    @patch("oas_android_agent.error_bundle.requests.post", return_value=_Response())
    def test_current_error_directory_uploads_only_current_task_artifacts(self, post) -> None:
        """本任务的错误截图、日志和上下文会随失败结果一同上传。"""
        current_stamp = int(datetime.now().timestamp() * 1000)
        current = self.runtime_root / "log" / "error" / f"agent-redroid-01_{current_stamp}"
        current.mkdir(parents=True)
        (current / "last.png").write_bytes(b"current-screenshot")
        (current / "log.txt").write_text("failure detail", encoding="utf-8")
        (current / "context.json").write_text('{"page":"battle"}', encoding="utf-8")

        self.uploader.upload(
            "execution-2", "Exploration", 8,
            {"started_at": (datetime.now() - timedelta(seconds=1)).isoformat()}
        )

        files = post.call_args.kwargs["files"]
        self.assertEqual([item[1][0] for item in files], ["context.json", "last.png", "log.txt"])
        self.assertEqual(post.call_args.kwargs["headers"]["x-oas-agent-secret"], "test-secret")

    @patch("oas_android_agent.error_bundle.requests.post", return_value=_Response())
    def test_error_directory_millisecond_precision_matches_task_start(self, post) -> None:
        """同一毫秒内创建的错误目录不能因启动时间含微秒而被丢弃。"""
        started = datetime.now()
        current = self.runtime_root / "log" / "error" / (
            f"agent-redroid-01_{int(started.timestamp() * 1000)}"
        )
        current.mkdir(parents=True)
        (current / "last.png").write_bytes(b"same-millisecond-screenshot")

        self.uploader.upload(
            "execution-same-millisecond",
            "Restart",
            7,
            {"started_at": started.isoformat()},
        )

        files = post.call_args.kwargs["files"]
        self.assertEqual([item[1][0] for item in files], ["last.png", "log.txt"])

    @patch("oas_android_agent.error_bundle.requests.post", return_value=_Response())
    def test_error_metadata_and_context_paths_are_sanitized(self, post) -> None:
        """上传的失败元数据和 JSON 上下文不得暴露容器内项目路径。"""
        current_stamp = int(datetime.now().timestamp() * 1000)
        current = self.runtime_root / "log" / "error" / f"agent-redroid-01_{current_stamp}"
        current.mkdir(parents=True)
        (current / "context.json").write_text(
            '{"trace":"File \\\"/data/oas/OnmyojiAutoScript-Self/a.py\\\""}', encoding="utf-8"
        )

        self.uploader.upload(
            "execution-3", "Restart", 9,
            {
                "started_at": (datetime.now() - timedelta(seconds=1)).isoformat(),
                "summary": 'File "/data/oas/OnmyojiAutoScript-Self/b.py"',
            },
        )

        metadata = __import__("json").loads(post.call_args.kwargs["data"]["metadata"])
        self.assertEqual(metadata["config_revision"], 9)
        self.assertIn("C:\\fakepath\\OnmyojiAutoScript", metadata["result"]["summary"])
        context = next(item for item in post.call_args.kwargs["files"] if item[1][0] == "context.json")
        self.assertIn(
            "C:\\fakepath\\OnmyojiAutoScript",
            __import__("json").loads(context[1][1].decode("utf-8"))["trace"],
        )

    @patch("oas_android_agent.error_bundle.requests.post", return_value=_Response())
    def test_invalid_json_context_is_sanitized_before_upload(self, post) -> None:
        """损坏 JSON 也必须先按文本脱敏，不能以原始字节绕过保护。"""
        current_stamp = int(datetime.now().timestamp() * 1000)
        current = self.runtime_root / "log" / "error" / f"agent-redroid-01_{current_stamp}"
        current.mkdir(parents=True)
        current.joinpath("context.json").write_bytes(
            b'{"trace":"File \\"/data/oas/OnmyojiAutoScript-Self/private.py"'
        )

        self.uploader.upload(
            "execution-4", "Restart", 10,
            {"started_at": (datetime.now() - timedelta(seconds=1)).isoformat()},
        )

        context = next(item for item in post.call_args.kwargs["files"] if item[1][0] == "context.json")
        payload = context[1][1].decode("utf-8")
        self.assertNotIn("/data/oas/OnmyojiAutoScript-Self", payload)
        self.assertIn(
            "C:\\fakepath\\OnmyojiAutoScript-Self",
            __import__("json").loads(payload)["invalid_json_text"],
        )

    @patch("oas_android_agent.error_bundle.MAX_UPLOAD_FILE_BYTES", 8)
    @patch("oas_android_agent.error_bundle.MAX_UPLOAD_TOTAL_BYTES", 32)
    @patch("oas_android_agent.error_bundle.requests.post", return_value=_Response())
    def test_sanitized_payload_size_is_rechecked(self, post) -> None:
        """清洗后的 UTF-8 膨胀不能突破 Agent 自身错误包大小上限。"""
        current_stamp = int(datetime.now().timestamp() * 1000)
        current = self.runtime_root / "log" / "error" / f"agent-redroid-01_{current_stamp}"
        current.mkdir(parents=True)
        # 原始文件恰好达到 8 字节，但非法 UTF-8 替换和 JSON 包装后会超过上限。
        (current / "context.json").write_bytes(b"\xff" * 8)

        self.uploader.upload(
            "execution-expanded", "Restart", 11,
            {"started_at": (datetime.now() - timedelta(seconds=1)).isoformat()},
        )

        files = post.call_args.kwargs["files"]
        self.assertEqual([item[1][0] for item in files], ["log.txt"])


if __name__ == "__main__":
    unittest.main()
