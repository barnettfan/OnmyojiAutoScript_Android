# OAS Android Agent（Root 手机命令行安装）

这里把 Android 端当作一台可以用 `su` 执行命令的 Root 手机来安装，不需要 redroid、Docker
或 APK。Agent 是一个常驻 Python 进程：它在手机本机运行截图、OCR 和点击，通过
`ws://154.206.14.32:22270` 主动连接 Server。

> 当前仓库不携带 Python、PaddleOCR/ONNX/OpenCV 等二进制依赖和 OCR 模型。推荐在
> Termux 的 Debian ARM64 PRoot 用户空间运行这些依赖；这不是 Docker、redroid 或模拟器，
> 截图和触控仍由手机的真实 Android Root shell 执行。

## 0. 先在 Server 创建设备

先按 [Server 部署说明](../server/README.md) 启动服务，访问：

```text
http://154.206.14.32:22270/agents/ui
```

登录后，使用 [template.agent-device.json](../server/config/template.agent-device.json) 创建
一台设备，例如 `device_id` 为 `phone-01`。其中的游戏包名和屏幕分辨率必须与手机实际值
一致；`Restart` 任务必须保留。

## 1. 手机需要具备的条件

在手机终端中执行以下命令。所有 Agent 操作都必须以 Root 身份运行：

```sh
su -c id
su -c 'screencap -p >/data/local/tmp/oas-check.png && test -s /data/local/tmp/oas-check.png'
su -c 'input help | grep -E "tap|swipe|motionevent"'
su -c 'uiautomator dump --compressed /data/local/tmp/oas-check.xml && test -s /data/local/tmp/oas-check.xml'
```

第一条必须显示 `uid=0`。另外要确认手机已经安装阴阳师，并记下实际包名和分辨率：

```sh
su -c 'wm size'
su -c 'cmd package path com.netease.onmyoji'
```

如果游戏包名不是 `com.netease.onmyoji`，请在 Server 的设备配置中填写实际包名。

还必须准备一套 ARM64 Linux Python 运行时，以及与该 Python/CPU 架构匹配的固定依赖：

```text
cv2, numpy, PIL, lxml, pydantic, yaml, zerorpc, requests,
adbutils, uiautomator2, onnxruntime, paddleocr, websockets
```

### Termux 推荐安装方式

以下操作在 Termux 用户下执行，**不要**在命令前加 `su -c`。它会安装 Debian ARM64 用户
空间与 OAS 的图像基础库；常驻 Agent 本身仍由 Android Root 启动，并经 `/system/xbin/su`
调用手机的真实 Root：

```sh
pkg install -y proot-distro
proot-distro install debian
proot-distro login debian --shared-tmp -- /bin/sh -lc '
  apt-get update &&
  DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3 python3-venv python3-pip python3-opencv python3-numpy \
    python3-pillow python3-lxml python3-yaml libgl1 libglib2.0-0
'
```

验证基础运行时：

```sh
proot-distro login debian --shared-tmp -- /usr/bin/python3 -c \
  'import cv2, numpy, PIL, lxml, yaml; print("Python image dependencies: OK")'
```

创建一个复用系统 OpenCV/NumPy 的虚拟环境，再安装 Agent 与 OCR 依赖：

```sh
proot-distro login debian --shared-tmp -- /bin/sh -lc '
  python3 -m venv --system-site-packages /root/oas-venv &&
  /root/oas-venv/bin/pip install --upgrade pip &&
  /root/oas-venv/bin/pip install \
    "setuptools<81" "packaging>=24" \
    websockets==13.1 requests==2.34.2 pydantic==2.11.7 PyYAML==6.0.2 \
    zerorpc==0.6.3 adbutils==0.11.0 uiautomator2==2.16.17 \
    onnxruntime paddleocr==3.7.0 rich==13.3.5 paho-mqtt==1.6.1 \
    fastapi==0.115.12 python-multipart==0.0.20 uvicorn==0.38.0 \
    cn2an==0.5.23 tqdm==4.65.0 anytree==2.8.0 cryptography \
    onepush==1.3.0 inflection==0.5.1
'
```

不要在 Python 3.13/ARM64 上直接使用仓库锁定的 `requirements.txt`：其中锁定的旧版本不是
为手机 ARM64 编译的。`setuptools<81` 是 `adbutils` 仍依赖 `pkg_resources` 的兼容要求；
以上组合须在启动 Agent 前通过导入和 OCR 模型检查。

下载并放置两份离线 OCR 模型（约 140MB）：

```sh
su -c 'export PATH=/data/data/com.termux/files/usr/bin:$PATH; \
  export LD_LIBRARY_PATH=/data/data/com.termux/files/usr/lib; \
  proot-distro login debian --shared-tmp -- /root/oas-venv/bin/python -c \
  "from paddleocr import PaddleOCR; PaddleOCR(\
    text_detection_model_name=\\\"PP-OCRv6_medium_det\\\",\
    text_recognition_model_name=\\\"PP-OCRv6_medium_rec\\\",\
    engine=\\\"onnxruntime\\\",\
    use_doc_orientation_classify=False,\
    use_doc_unwarping=False,\
    use_textline_orientation=False)"'
su -c 'export PATH=/data/data/com.termux/files/usr/bin:$PATH; \
  export LD_LIBRARY_PATH=/data/data/com.termux/files/usr/lib; \
  proot-distro login debian --shared-tmp -- /bin/sh -lc \
  "mkdir -p /data/local/oas-agent/runtime/oas/toolkit/.paddlex; \
   cp -a /root/.paddlex/official_models /data/local/oas-agent/runtime/oas/toolkit/.paddlex/"'
```

验证模型文件：

```sh
su -c 'find /data/local/oas-agent/runtime/oas/toolkit/.paddlex/official_models \
  -name inference.onnx -size +1M -print'
```

还需要一份离线 OCR 模型，解压后必须位于：

```text
/data/local/oas-agent/runtime/oas/toolkit/.paddlex
```

Agent 不会在手机上下载缺失的 Python 依赖或 OCR 模型。没有这些条件时，Agent 虽然能启动，
但管理页会显示 `unhealthy`，不会接收任务。

## 2. 上传 Agent 文件到手机

在开发电脑的仓库根目录打包 `agent` 目录：

```bash
tar -C agent -czf oas-agent.tar.gz \
  oas_android_agent runtime init requirements.txt
```

通过你已有的文件传输方式把 `oas-agent.tar.gz` 和 OCR 模型文件传到手机，例如放到
`/sdcard/Download/`。随后在手机终端以 Root 解压：

```sh
su -c 'rm -rf /data/local/oas-agent.new'
su -c 'mkdir -p /data/local/oas-agent.new'
su -c 'tar -xzf /sdcard/Download/oas-agent.tar.gz -C /data/local/oas-agent.new'
su -c 'chmod 700 /data/local/oas-agent.new/init/run-root-phone-agent.sh'
su -c 'mv /data/local/oas-agent /data/local/oas-agent.previous 2>/dev/null || true'
su -c 'mv /data/local/oas-agent.new /data/local/oas-agent'
```

然后把 OCR 模型放进 `/data/local/oas-agent/runtime/oas/toolkit/.paddlex`。模型文件来源取决
于你准备的 Android PaddleOCR 运行环境，不能用桌面 x86 Python 的模型/二进制替代 Android
版本。确认最终目录至少包含：

```text
/data/local/oas-agent/oas_android_agent/
/data/local/oas-agent/runtime/oas/
/data/local/oas-agent/init/run-root-phone-agent.sh
/data/local/oas-agent/runtime/oas/toolkit/.paddlex/
```

上面的移动方式会保留上一次代码在 `/data/local/oas-agent.previous`，方便回滚。确认新版本
正常后可手动删除该目录。

## 3. 创建手机配置文件

在手机创建 `/data/local/oas-agent/agent.env`：

```sh
su -c 'cat > /data/local/oas-agent/agent.env <<"EOF"
OAS_AGENT_DEVICE_ID=phone-01
OAS_AGENT_SERVER_URL=ws://154.206.14.32:22270
OAS_AGENT_ALLOW_INSECURE_WS=true
OAS_AGENT_SHARED_SECRET=<替换为 server/.env 中完全相同的 OAS_AGENT_SHARED_SECRET>
OAS_AGENT_STATE_ROOT=/data/local/oas-agent/state
OAS_AGENT_PROOT_DISTRO=debian
OAS_AGENT_TERMUX_PREFIX=/data/data/com.termux/files/usr
OAS_AGENT_PYTHON_BIN=/root/oas-venv/bin/python
OAS_AGENT_ROOT_SU_BIN=/system/xbin/su
OAS_AGENT_VERSION=2026.11.26
OAS_AGENT_RESOURCE_VERSION=2026.11.26
EOF
chmod 600 /data/local/oas-agent/agent.env'
```

需要替换三处：

- `phone-01`：必须与 Server 管理页创建的 `device_id` 完全一致。
- `OAS_AGENT_SHARED_SECRET`：必须与服务端 `server/.env` 相同，不能填管理密码。
- `OAS_AGENT_PROOT_DISTRO=debian`：使用上节安装的 Debian ARM64 Python；没有 PRoot 时删除此行，
  再把 `OAS_AGENT_PYTHON_BIN` 改为手机本机 Python 的真实绝对路径。
- `OAS_AGENT_TERMUX_PREFIX`：Termux 的固定运行时路径，使 Android Root 启动的脚本能加载
  `proot-distro`，并将 Root 私有的 Agent 目录显式绑定到 Debian。若 Termux 安装在其他
  用户/路径，必须调整为实际路径。
- `OAS_AGENT_ROOT_SU_BIN=/system/xbin/su`：本手机已验证的真实 Android Root 程序路径。不同
  Root 方案应改为实际路径；没有它时，非 PRoot 运行方式会自动根据进程身份决定是否使用 `su`。

`OAS_AGENT_ALLOW_INSECURE_WS=true` 允许本项目当前使用 `ws://` 直连 IP；没有它 Agent 会
拒绝连接。明文 `ws://` 会暴露共享密钥和后台密码，请将服务器 `22270` 端口在安全组/防火墙
中限制为手机和管理电脑的来源 IP。

## 4. 第一次手动启动

先前台启动，便于直接看到错误信息：

```sh
su -c /data/local/oas-agent/init/run-root-phone-agent.sh
```

正常情况下，该命令会持续运行。回到 Server 管理页，设备应先连接，然后变为 `healthy`。
按 `Ctrl+C` 停止测试进程。

如需后台运行：

```sh
su -c 'nohup /data/local/oas-agent/init/run-root-phone-agent.sh >> /data/local/oas-agent/agent.log 2>&1 &'
```

查看日志和进程：

```sh
su -c 'tail -n 100 /data/local/oas-agent/agent.log'
su -c 'ps -A | grep oas_android_agent'
```

常见 `unhealthy` 原因是 Python 依赖或 OCR 模型缺失、游戏包名或分辨率与 Server 配置不一致、
没有 RootShell 截图/输入权限。以管理页返回的具体预检原因优先处理。

## 5. Magisk 开机自动启动（可选）

如果手机使用 Magisk Root，可把仓库中的启动服务安装到 `service.d`：

```sh
su -c 'cp /data/local/oas-agent/init/oas_android_agent_magisk.sh /data/adb/service.d/oas_android_agent.sh'
su -c 'chmod 700 /data/adb/service.d/oas_android_agent.sh'
```

它会等待 `sys.boot_completed=1` 后启动 Agent；Agent 异常退出时每 10 秒重试。重启手机后验证：

```sh
su -c 'ps -A | grep oas_android_agent'
su -c 'tail -n 100 /data/local/oas-agent/agent.log'
```

使用 KernelSU、APatch 或其他 Root 方案时，不要假定存在 Magisk 的 `/data/adb/service.d`；请将
`run-root-phone-agent.sh` 以该 Root 方案提供的“开机后 Root 脚本”机制启动。

## 6. 升级、停止和回滚

升级时重新打包并替换代码目录，但保留 `agent.env` 与 `state/`，这样已确认的配置和待回传任务
结果不会丢失。替换前先停止当前 Agent：

```sh
su -c 'pkill -f "python.*oas_android_agent"'
```

若升级后异常，先停止 Agent，再恢复前一份代码：

```sh
su -c 'mv /data/local/oas-agent /data/local/oas-agent.failed'
su -c 'mv /data/local/oas-agent.previous /data/local/oas-agent'
```

随后按第 4 节手动启动或重启手机。不要删除 `/data/local/oas-agent/state/`，除非确认没有正在
执行或待回传的任务。
