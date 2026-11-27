"""Android/redroid 容器内 Root shell 设备后端。

该后端只在设备本机执行 ``su -c``，不会启动、连接或暴露 ADB 服务。
"""
from __future__ import annotations

import re
import os
import shlex
import subprocess
import time
from typing import Sequence

import cv2
import numpy as np
from lxml import etree

from module.device.root_shell_command import root_shell_command
from module.device.method.utils import HierarchyButton
from module.exception import RequestHumanTakeover
from module.exception import TaskEnd
from module.logger import logger


class RootShell:
    """将 OAS 的统一设备操作映射为容器内的 Root shell 命令。"""

    ROOT_COMMAND_TIMEOUT = 15
    # Android init 服务没有控制终端，不能依赖 ``uiautomator dump /dev/tty``。
    # 每台设备同时只有一个 Agent 任务，固定的容器内临时文件不会与其他任务冲突。
    ROOT_HIERARCHY_PATH = "/data/local/tmp/oas-agent-hierarchy.xml"

    def __getattribute__(self, name: str):
        """在 Root Agent 中封死 ``Connection`` 遗留的所有 ADB 方法入口。

        ``Connection`` 自己定义了 ``adb_command``、``adb_shell`` 等方法，因此
        Python 的 MRO 会让它们遮蔽本 mixin 中同名的兼容拒绝方法。只在各处
        增加局部判断容易遗漏新加的 ADB helper；这里在属性解析处统一阻止所有
        ``adb_`` 前缀的调用，确保 RootShell 模式绝不会意外拉起或连接 ADB。
        非 RootShell 的桌面/模拟器后端保持原来的属性解析路径。
        """
        if name.startswith("adb_"):
            try:
                is_root_shell = object.__getattribute__(self, "is_root_shell")
            except (AttributeError, TypeError):
                is_root_shell = False
            if is_root_shell:
                raise RequestHumanTakeover("ADB is disabled in RootShell Agent mode")
        return super().__getattribute__(name)

    @property
    def adb(self):
        """禁止 Root Agent 通过遗留 ``adbutils`` 属性绕过本机后端。

        现有 OAS 的设备继承图为兼容桌面和模拟器仍保留 ``adb`` 属性。RootShell
        模式下即使只是首次读取该属性，也可能构造 ADB client；因此必须在属性
        入口直接拒绝，而不能只依赖调用方使用 ``adb_shell``。
        """
        if not self.is_root_shell:
            # RootShell 位于 Device MRO 的兼容层；桌面/ADB 模式仍须委托给
            # ConnectionAttr 的原始属性，不能让 RootShell 防护影响旧后端。
            return super().adb
        raise RequestHumanTakeover("ADB is disabled in RootShell Agent mode")

    @property
    def adb_client(self):
        """禁止 Root Agent 初始化 ADB server 客户端。"""
        if not self.is_root_shell:
            return super().adb_client
        raise RequestHumanTakeover("ADB is disabled in RootShell Agent mode")

    @property
    def u2(self):
        """禁止 Root Agent 初始化依赖 ADB 的 uiautomator2 连接。"""
        if not self.is_root_shell:
            return super().u2
        raise RequestHumanTakeover("uiautomator2 is disabled in RootShell Agent mode")

    def adb_command(self, *args, **kwargs):
        """Root Agent 禁止所有外部 ADB 命令，防止任务误启动 ADB 服务。"""
        raise RequestHumanTakeover("ADB is disabled in RootShell Agent mode")

    def adb_shell(self, *args, **kwargs):
        """Root Agent 的任务必须使用 root_shell，不允许 ADB shell 旁路。"""
        raise RequestHumanTakeover("ADB shell is disabled in RootShell Agent mode")

    def _raise_if_agent_stop_requested(self) -> None:
        """在截图或输入前检查 Agent 协作停止标记，形成任务安全退出点。"""
        stop_file = os.environ.get("OAS_AGENT_STOP_FILE", "")
        if stop_file and os.path.exists(stop_file):
            raise TaskEnd("Agent stop requested")

    def root_shell(self, command: str, *, binary: bool = False, timeout: int | None = None) -> str | bytes:
        """执行不经过 ADB 的本机 Root shell 命令，并在失败时给出明确错误。"""
        if not self.is_root_shell:
            raise RequestHumanTakeover("RootShell backend is not enabled")
        try:
            completed = subprocess.run(
                root_shell_command(command),
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout or self.ROOT_COMMAND_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RequestHumanTakeover(f"Root shell command failed: {exc}") from exc
        if completed.returncode != 0:
            error = completed.stderr.decode("utf-8", errors="replace").strip()
            raise RequestHumanTakeover(f"Root shell command failed ({completed.returncode}): {error or command}")
        if binary:
            return completed.stdout
        return completed.stdout.decode("utf-8", errors="replace").strip()

    def root_shell_healthcheck(self) -> None:
        """确认当前 Agent 确实获得 Root，再允许任务启动。"""
        identity = str(self.root_shell("id"))
        if "uid=0" not in identity:
            raise RequestHumanTakeover(f"Root shell is not uid=0: {identity}")
        logger.attr("RootShell", identity)

    def screenshot_root_shell(self) -> np.ndarray:
        """使用 ``screencap -p`` 获取本地 PNG，并转换为 OAS 的 RGB ndarray。"""
        self._raise_if_agent_stop_requested()
        data = self.root_shell("screencap -p", binary=True)
        image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise RequestHumanTakeover("Root shell screencap returned an invalid PNG")
        return cv2.cvtColor(image, cv2.COLOR_BGR2RGB)

    def click_root_shell(self, x: int, y: int) -> None:
        """通过 Android input 注入一次触控点击。"""
        self._raise_if_agent_stop_requested()
        self.root_shell(f"input tap {int(x)} {int(y)}")

    def long_click_root_shell(self, x: int, y: int, duration: float) -> None:
        """Root shell 没有独立长按指令，使用原地 swipe 表示按住。"""
        self._raise_if_agent_stop_requested()
        duration_ms = max(1, round(float(duration) * 1000))
        self.root_shell(f"input swipe {int(x)} {int(y)} {int(x)} {int(y)} {duration_ms}")

    def swipe_root_shell(self, p1: Sequence[int], p2: Sequence[int], duration: float) -> None:
        """通过 Android input 注入滑动，时长按毫秒传递。"""
        self._raise_if_agent_stop_requested()
        duration_ms = max(1, round(float(duration) * 1000))
        self.root_shell(
            f"input swipe {int(p1[0])} {int(p1[1])} {int(p2[0])} {int(p2[1])} {duration_ms}"
        )

    def draw_root_shell(self, points: list, interval: float = 0.01) -> None:
        """用本机 ``input motionevent`` 绘制多点连续轨迹，不经过 ADB。

        ``DailyTrifles`` 的神秘图案需要连续的 DOWN/MOVE/UP 事件，单次
        ``input swipe`` 不能表达折线路径。复用 OAS 现有路径平滑规则，逐点
        通过 Root shell 注入；停止标记在每个事件前检查，确保管理员停止命令
        能在绘制过程中安全生效。
        """
        if len(points) < 2:
            logger.warning("至少需要2个点来画线")
            return
        # 不能为了复用 ``minitouch.smooth_path`` 而导入整个 Minitouch 模块：
        # 它的模块级依赖包含 ADB/uiautomator2。RootShell 仅需要下方的纯 NumPy
        # 路径平滑，不应在绘制神秘图案时加载任何 ADB 兼容栈。
        smooth_points = self._smooth_root_shell_path(points)
        start = smooth_points[0]
        self._raise_if_agent_stop_requested()
        self.root_shell(f"input motionevent DOWN {int(start[0])} {int(start[1])}")
        for point in smooth_points[1:]:
            if interval > 0:
                time.sleep(interval)
            self._raise_if_agent_stop_requested()
            self.root_shell(f"input motionevent MOVE {int(point[0])} {int(point[1])}")
        final = smooth_points[-1]
        self._raise_if_agent_stop_requested()
        self.root_shell(f"input motionevent UP {int(final[0])} {int(final[1])}")

    @staticmethod
    def _smooth_root_shell_path(
        points: list,
        min_distance: float = 30.0,
        offset_range: float = 3.0,
    ) -> list:
        """为 Root ``motionevent`` 构造不依赖 Minitouch/ADB 的平滑轨迹。

        保持原有 Minitouch 路径算法的分段和微小垂直扰动语义，以免每日召唤
        图案在切换到 RootShell 后退化为生硬直线；该方法仅依赖已用于截图的
        NumPy，因此不会触发 Minitouch 的 ADB 或 uiautomator2 导入。
        """
        if len(points) < 2:
            return points
        smooth_points = [points[0]]
        for index in range(len(points) - 1):
            start = np.array(points[index])
            end = np.array(points[index + 1])
            direction = end - start
            distance = np.linalg.norm(direction)
            if distance <= min_distance:
                continue
            unit_direction = direction / distance
            perpendicular = np.array([-unit_direction[1], unit_direction[0]])
            segments = int(distance / min_distance)
            for segment in range(1, segments + 1):
                ratio = segment / segments
                interpolated = start + ratio * direction
                offset = np.random.uniform(-offset_range, offset_range)
                point = interpolated + offset * perpendicular
                smooth_points.append((int(point[0]), int(point[1])))
        if smooth_points[-1] != points[-1]:
            smooth_points.append(points[-1])
        return smooth_points

    def input_text_root_shell(self, value: str) -> None:
        """经本机剪贴板粘贴文本，避免 ``input text`` 不能可靠输入中文名称。

        每日捐赠等流程需要输入游戏内昵称。直接的 Android ``input text`` 依赖
        KeyCharacterMap，通常只能输入 ASCII；使用 ``cmd clipboard`` 加粘贴键可
        保留 UTF-8 文本，并且仍只在容器内的 Root shell 执行。
        """
        self._raise_if_agent_stop_requested()
        text = str(value)
        if not text:
            return
        self.root_shell(f"cmd clipboard set {shlex.quote(text)} && input keyevent KEYCODE_PASTE")

    def app_start_root_shell(self) -> None:
        """以前台 launcher intent 启动配置的游戏包。"""
        self._raise_if_agent_stop_requested()
        self.root_shell(f"monkey -p {self.package} -c android.intent.category.LAUNCHER 1")

    def app_stop_root_shell(self) -> None:
        """仅停止游戏进程，不处理 redroid 容器生命周期。"""
        self._raise_if_agent_stop_requested()
        self.root_shell(f"am force-stop {self.package}")

    def app_current_root_shell(self) -> str:
        """读取当前前台 Activity 所属包名。"""
        output = str(self.root_shell("dumpsys window windows"))
        matched = re.search(r"mCurrentFocus=.*?\s([A-Za-z0-9._]+)/(?:[A-Za-z0-9._$]+)", output)
        if matched:
            return matched.group(1)
        output = str(self.root_shell("dumpsys activity activities"))
        matched = re.search(r"mResumedActivity:.*?\s([A-Za-z0-9._]+)/(?:[A-Za-z0-9._$]+)", output)
        return matched.group(1) if matched else ""

    def app_is_alive_root_shell(self, package_name: str) -> bool:
        """通过 pidof 判断游戏是否仍有进程，不要求它处于前台。"""
        return bool(str(self.root_shell(f"pidof {package_name}")).strip())

    def dump_hierarchy_root_shell(self) -> etree._Element:
        """在本机导出 UIAutomator XML，兼容依赖 hierarchy 的既有任务。"""
        # ``/dev/tty`` 在 redroid 的 init 服务中可能不可用，而且 uiautomator
        # 把 XML 写到该文件时不保证 stdout 能被 subprocess 捕获。先落在设备
        # 本地临时文件、读取后删除，既适配 Android shell 也不需要 ADB pull。
        path = self.ROOT_HIERARCHY_PATH
        output = str(self.root_shell(
            f"uiautomator dump --compressed {path} >/dev/null; status=$?; "
            f"if [ -f {path} ]; then cat {path}; rm -f {path}; fi; exit $status"
        ))
        xml_start = output.find("<?xml")
        if xml_start < 0:
            raise RequestHumanTakeover("Root shell uiautomator did not return XML")
        try:
            return etree.fromstring(output[xml_start:].encode("utf-8"))
        except etree.XMLSyntaxError as exc:
            raise RequestHumanTakeover(f"Root shell uiautomator XML is invalid: {exc}") from exc

    def xpath_to_button_root_shell(self, xpath: str) -> HierarchyButton:
        """保持 AppControl 对 HierarchyButton 的既有调用方式。"""
        return HierarchyButton(self.hierarchy, xpath)
