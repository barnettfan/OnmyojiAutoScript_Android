"""以独立进程执行一项已有 OAS 任务，隔离任务中的 ``exit()``。"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from module.exception import TaskEnd


def ensure_task_runtime_ready(script: object, task_name: str) -> bool:
    """在单任务 Agent 中复用 OAS 的游戏进程检查与恢复逻辑。

    桌面版 ``Script.loop`` 会在调用 ``Script.run`` 前执行
    ``prepare_task_execution``。Android Agent 只运行一个已租约任务，若跳过这
    一步，游戏被外部关闭时会直接进入截图错误而不会执行既有 ``Restart`` /
    登录恢复。恢复成功后桌面调度器会重选任务；这里保留同一任务租约，并再检查
    一次运行态后继续执行，最多只接受一次恢复请求，避免异常状态无限循环。
    """
    for _ in range(2):
        decision = script.runtime.prepare_task_execution(task_name)
        # ``ScriptRuntimeDecision`` 是 str Enum；读取 value 也让这个薄封装
        # 不必为了三个稳定状态导入完整的 OAS Device/日志依赖图。
        outcome = getattr(decision, "value", decision)
        if outcome == "ready":
            return True
        if outcome == "reschedule":
            continue
        return False
    return False


def _write_result(path: Path, payload: dict) -> None:
    """无论任务成功、异常或调用 SystemExit，均写出结构化结果供 Agent 上报。"""
    # 结果文件是任务子进程与常驻 Agent 间的唯一交接点。直接覆盖会使容器
    # 在写入期间中断时留下半截 JSON，令常驻 Agent 失去本次准确的结果状态。
    _atomic_write_json(path, payload)


def _supervisor_is_alive(supervisor_pid: int) -> bool:
    """确认任务运行器仍由常驻 Agent 直接监管，避免 Agent 崩溃后孤儿执行。"""
    return supervisor_pid > 1 and os.getppid() == supervisor_pid


def _start_supervisor_watchdog(supervisor_pid: int | None) -> tuple[threading.Event | None, object | None]:
    """监视 Agent 父进程；父进程消失时以 SystemExit 走正常清理与结果落盘。

    Android Agent 采用独立子进程隔离 OAS 的 ``exit()``。这也意味着 Agent 服务
    意外重启时任务进程可能成为孤儿；必须主动停止它，避免旧任务与新租约并发触控。
    """
    if supervisor_pid is None or os.name == "nt":
        return None, None
    stopped = threading.Event()
    previous_handler = signal.getsignal(signal.SIGTERM)

    def parent_gone(_signum: int, _frame: object) -> None:
        """将 watchdog 信号转换为可被 run_task 捕获的退出。"""
        raise SystemExit("Agent supervisor exited")

    signal.signal(signal.SIGTERM, parent_gone)

    def watch() -> None:
        """周期检查父 PID；终止信号在主线程触发正常 finally 清理。"""
        while not stopped.wait(1.0):
            if _supervisor_is_alive(supervisor_pid):
                continue
            os.kill(os.getpid(), signal.SIGTERM)
            return

    threading.Thread(target=watch, name="agent-supervisor-watchdog", daemon=True).start()
    return stopped, previous_handler


def _stop_supervisor_watchdog(stopped: threading.Event | None, previous_handler: object | None) -> None:
    """任务正常收尾后关闭看门线程并恢复 SIGTERM 处理方式。"""
    if stopped is not None:
        stopped.set()
    if previous_handler is not None:
        signal.signal(signal.SIGTERM, previous_handler)


def _shutdown_local_agent_rpc() -> None:
    """回收当前单任务子进程托管的本地 OCR/图像服务。

    虽然 RPC 模块注册了 ``atexit``，但任务运行器会捕获 ``SystemExit`` 并生成
    结构化结果。此处显式回收可保证结果写入前端口和子进程已经释放，避免下一份
    租约继承旧任务的图像帧或 OCR worker。模块只会终止本进程启动的服务。
    """
    try:
        from module.ocr.rpc import shutdown_ocr_server

        shutdown_ocr_server()
    except Exception:
        # 清理失败不得覆盖原任务结果；进程退出时 atexit 仍会再尝试一次。
        pass
    try:
        from module.image.rpc import shutdown_image_server

        shutdown_image_server()
    except Exception:
        pass


def _native_task_key(task_name: str) -> str:
    """将 OAS 的任务名映射为运行时 JSON 中的 snake_case 键。"""
    if task_name == "LBS":
        return "lbs"
    return re.sub(r"(?<!^)(?=[A-Z])", "_", task_name).lower()


def _read_task_scheduler(runtime_config_path: Path, task_name: str) -> dict | None:
    """读取指定任务的调度字段，配置无效时不把全量任务配置带入结果。"""
    try:
        runtime_config = json.loads(runtime_config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    task = runtime_config.get(_native_task_key(task_name))
    scheduler = task.get("scheduler") if isinstance(task, dict) else None
    if not isinstance(scheduler, dict):
        return None
    return {key: scheduler.get(key) for key in ("enable", "next_run")}


def ensure_task_rescheduled(
    config: object,
    runtime_config_path: Path,
    task_name: str,
    scheduler_before_run: dict | None,
    started_at: datetime,
    success: bool,
) -> bool:
    """为未自行更新调度状态的单任务补上一次常规成功/失败延后。

    常规 OAS 由 ``Script.loop`` 在持续调度上下文中运行。Android Agent 每次仅运行
    一个任务，所以任务若正常退出却没有调用 ``set_next_run``，其过期的 ``next_run``
    会被 Server 无限重复派发。已有任务显式修改过自身调度时不干预；否则使用既有
    ``task_delay`` 规则写入下一次运行时间，确保变化可经 ``schedule_patch`` 回传。
    """
    scheduler_after_run = _read_task_scheduler(runtime_config_path, task_name)
    if scheduler_before_run is None or scheduler_after_run is None:
        return False
    if scheduler_after_run != scheduler_before_run:
        return False
    # 使用任务开始时刻与既有任务 ``set_next_run(..., finish=False)`` 一致，避免
    # 长任务完成后把下一次执行额外向后推迟整段任务时长。
    config.task_delay(
        task_name,
        start_time=started_at.replace(microsecond=0),
        success=success,
    )
    return True


def disable_task_for_manual_handling(config: object, task_name: str) -> bool:
    """在人工接管结果下暂停当前任务，禁止 Server 自动重复租约。

    桌面版 Scheduler 在进程退出后整体停止；C/S Agent 会继续接收其他任务，因而必须
    将当前任务的 ``scheduler.enable`` 写为 false。该变化会被 schedule_patch 提取，
    管理员确认现场后可在 Server 配置页重新启用任务。
    """
    task = getattr(getattr(config, "model", None), _native_task_key(task_name), None)
    scheduler = getattr(task, "scheduler", None)
    if scheduler is None or not hasattr(scheduler, "enable"):
        return False
    scheduler.enable = False
    config.save()
    return True


def disable_runtime_task_for_manual_handling(runtime_config_path: Path, task_name: str) -> bool:
    """在 OAS 尚未成功初始化时直接暂停本次任务的本地调度配置。

    该分支只处理导入、OCR 服务启动等早期未捕获异常；此时没有 ``Config`` 实例可用，
    但当前子进程独占运行时配置文件。写入的开关仍会被 Agent 的 schedule_patch 提取。
    """
    try:
        runtime_config = json.loads(runtime_config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    task = runtime_config.get(_native_task_key(task_name))
    scheduler = task.get("scheduler") if isinstance(task, dict) else None
    if not isinstance(scheduler, dict) or not isinstance(scheduler.get("enable"), bool):
        return False
    scheduler["enable"] = False
    # 早期导入/OCR 失败时常驻 Agent 仍依赖该文件提取 schedule_patch。直接
    # 覆盖会在容器重启或 I/O 中断后留下半截 JSON，导致 Server 不知道任务已
    # 被人工暂停；使用同目录临时文件替换，读者只能看到新旧任一完整快照。
    _atomic_write_json(runtime_config_path, runtime_config)
    return True


def _atomic_write_json(path: Path, data: dict) -> None:
    """原子写入任务交接或运行时配置 JSON，避免中断留下半截文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def run_task(
    runtime_root: Path,
    config_name: str,
    task_name: str,
    result_path: Path,
    stop_file: Path,
    supervisor_pid: int | None = None,
) -> int:
    """启动 OAS 本地 OCR/图片服务并只运行指定任务一次。"""
    os.chdir(runtime_root)
    sys.path.insert(0, str(runtime_root))
    # 强制 OAS 的图像/OCR zerorpc 子服务仅在 Agent 容器 loopback 上运行，
    # 防止部署配置中的远端地址泄漏正常截图或 OCR 输入。
    os.environ["OAS_AGENT_LOCAL_RPC_ONLY"] = "true"
    started_at = datetime.now()
    payload = {"task_name": task_name, "status": "failure", "summary": "", "started_at": started_at.isoformat()}
    runtime_config_path = runtime_root / "config" / f"{config_name}.json"
    scheduler_before_run = _read_task_scheduler(runtime_config_path, task_name)
    script = None
    watchdog, previous_signal_handler = _start_supervisor_watchdog(supervisor_pid)
    try:
        # 单任务模式不经过 Script.loop；显式切换到按设备命名的日志文件，
        # 使错误目录中的 log.txt 能与具体 Agent 对应。
        from module.logger import logger
        logger.set_file_logger(config_name, do_cleanup=False)
        from module.agent_runtime import configure_local_agent_rpc
        from module.image.rpc import ensure_image_server_ready
        from module.ocr.rpc import ensure_ocr_server_ready
        from script import Script

        configure_local_agent_rpc()
        ensure_image_server_ready()
        ensure_ocr_server_ready()
        script = Script(config_name)
        script.config.model.running_task = task_name
        script.config.save()
        # 单任务 Agent 仍需先执行 OAS 原有的游戏状态检查。该步骤在 RootShell
        # 后端中只调用设备本机 Root shell，不会启动或连接 ADB。
        runtime_ready = ensure_task_runtime_ready(script, task_name)
        success = script.run(task_name) if runtime_ready else False
        payload["status"] = "cancelled" if stop_file.exists() else ("success" if success else "failure")
        # 取消也必须推进未被任务显式修改的 next_run，否则管理员停止后 Server
        # 会立刻再次租约同一到期任务。取消按失败间隔延后，管理员可随后手动重跑。
        fallback_applied = ensure_task_rescheduled(
            script.config,
            runtime_config_path,
            task_name,
            scheduler_before_run,
            started_at,
            payload["status"] == "success",
        )
        if payload["status"] == "cancelled":
            payload["summary"] = "stop_marker_observed_scheduler_fallback" if fallback_applied else "stop_marker_observed"
        elif not runtime_ready:
            payload["summary"] = "runtime_preparation_failed_scheduler_fallback" if fallback_applied else "runtime_preparation_failed"
        else:
            payload["summary"] = "scheduler_fallback" if fallback_applied else "task_finished"
    except TaskEnd as exc:
        # ``Script.run`` normally consumes TaskEnd as a regular task end.  A
        # RootShell stop marker can also raise it while the pre-run recovery
        # check is still executing, before Script.run installs that handler.
        # Preserve the cooperative-stop contract in that early path instead
        # of turning an administrator stop into manual_required.
        stopped = stop_file.exists()
        payload["status"] = "cancelled" if stopped else "manual_required"
        fallback_applied = False
        task_disabled = False
        try:
            if script is not None:
                if not stopped:
                    task_disabled = disable_task_for_manual_handling(script.config, task_name)
                fallback_applied = ensure_task_rescheduled(
                    script.config,
                    runtime_config_path,
                    task_name,
                    scheduler_before_run,
                    started_at,
                    False,
                )
            elif not stopped:
                task_disabled = disable_runtime_task_for_manual_handling(runtime_config_path, task_name)
        except Exception:
            # 结果必须优先落盘；调度补丁失败会由失败现场和管理页面暴露。
            fallback_applied = False
        if stopped:
            payload["summary"] = "stop_marker_observed_scheduler_fallback" if fallback_applied else "stop_marker_observed"
        else:
            suffix = "; task_disabled" if task_disabled else ""
            if fallback_applied:
                suffix += "; scheduler_fallback"
            payload["summary"] = f"Unexpected TaskEnd before task execution ({exc}){suffix}"
    except SystemExit as exc:
        # OAS 对 RequestHumanTakeover、配置错误等不可安全重试的路径使用 exit()。
        # 单任务 Agent 不能保留桌面版 Scheduler 的内存失败计数，因此必须显式要求
        # 管理员处理，避免 Server 立即再次派发同一到期任务。
        payload["status"] = "cancelled" if stop_file.exists() else "manual_required"
        fallback_applied = False
        task_disabled = False
        if script is not None:
            try:
                if not stop_file.exists():
                    task_disabled = disable_task_for_manual_handling(script.config, task_name)
                fallback_applied = ensure_task_rescheduled(
                    script.config,
                    runtime_config_path,
                    task_name,
                    scheduler_before_run,
                    started_at,
                    False,
                )
            except Exception:
                # 结果仍要落盘；调度补丁缺失时由错误现场暴露配置问题。
                fallback_applied = False
        if stop_file.exists():
            payload["summary"] = "stop_marker_observed_scheduler_fallback" if fallback_applied else "stop_marker_observed"
        else:
            suffix = "; task_disabled" if task_disabled else ""
            if fallback_applied:
                suffix += "; scheduler_fallback"
            payload["summary"] = f"OAS requires manual handling (exit {exc.code}){suffix}"
    except Exception as exc:
        # 只有 Script.run 返回 False 时才认为它走过了既有的可恢复失败路径。
        # 其余未捕获异常没有可靠的恢复语义，暂停当前任务交给管理员，避免 Server
        # 针对同一过期 next_run 形成无间隔重复执行。
        payload["status"] = "manual_required"
        task_disabled = False
        try:
            if script is not None:
                task_disabled = disable_task_for_manual_handling(script.config, task_name)
            else:
                task_disabled = disable_runtime_task_for_manual_handling(runtime_config_path, task_name)
        except Exception:
            task_disabled = False
        suffix = "; task_disabled" if task_disabled else ""
        payload["summary"] = f"{type(exc).__name__}: {exc}{suffix}"
        payload["traceback"] = traceback.format_exc(limit=20)
    finally:
        _stop_supervisor_watchdog(watchdog, previous_signal_handler)
        _shutdown_local_agent_rpc()
        payload["finished_at"] = datetime.now().isoformat()
        payload["duration_seconds"] = round((datetime.now() - started_at).total_seconds(), 3)
        _write_result(result_path, payload)
    return 0 if payload["status"] == "success" else 1


def main() -> int:
    """解析子进程参数，避免从环境变量猜测当前租约。"""
    parser = argparse.ArgumentParser(description="Run one OAS Android Agent task")
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--config-name", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--stop-file", required=True)
    parser.add_argument("--supervisor-pid", type=int)
    args = parser.parse_args()
    return run_task(
        Path(args.runtime_root),
        args.config_name,
        args.task,
        Path(args.result),
        Path(args.stop_file),
        args.supervisor_pid,
    )


if __name__ == "__main__":
    raise SystemExit(main())
