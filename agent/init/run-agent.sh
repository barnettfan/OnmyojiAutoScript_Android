#!/system/bin/sh
# Agent state contains only confirmed config snapshots, stop markers and outbox;
# screenshots, OAS logs and OCR all remain inside this redroid container.
# Android init implementations often clear the Docker process environment. A
# deployment may mount this root-readable file from its secret store so Agent
# identity, WSS endpoint and shared secret reach the init service without being
# baked into the image. Treat that file as data, never source it as shell code.
agent_env_file="${OAS_AGENT_ENV_FILE:-/data/oas-agent/agent.env}"
# Android init 在部分 redroid 基础镜像中不会继承 Docker 的 ENV。以下运行时
# 路径和离线约束必须由启动脚本本身提供，不能依赖镜像环境仍然存在。部署文件
# 只允许覆盖设备身份、Server 地址、密钥和显式状态目录，不能关闭离线模型策略。
# 任务代码和资源必须来自镜像固定版本，不能由可写的部署环境把 Agent
# 指向另一份运行时；只有状态目录允许按设备挂载持久卷。
export OAS_AGENT_RUNTIME_ROOT="/opt/oas"
export OAS_AGENT_STATE_ROOT="${OAS_AGENT_STATE_ROOT:-/var/lib/oas-agent}"
# 默认只允许 WSS。先清掉继承环境中的开关，只有挂载的 agent.env 明确写入
# ``OAS_AGENT_ALLOW_INSECURE_WS=true`` 时才允许直连明文 ws:// Server。
unset OAS_AGENT_ALLOW_INSECURE_WS
if [ -r "$agent_env_file" ]; then
    while IFS='=' read -r agent_env_name agent_env_value || [ -n "$agent_env_name" ]; do
        case "$agent_env_name" in
            ''|'#'*)
                continue
                ;;
            OAS_AGENT_DEVICE_ID|OAS_AGENT_SERVER_URL|OAS_AGENT_SHARED_SECRET|OAS_AGENT_STATE_ROOT|OAS_AGENT_VERSION|OAS_AGENT_RESOURCE_VERSION|OAS_AGENT_HEARTBEAT_SECONDS|OAS_AGENT_ALLOW_INSECURE_WS)
                export "$agent_env_name=$agent_env_value"
                ;;
            *)
                echo "Invalid key in OAS agent environment file: $agent_env_name" >&2
                exit 64
                ;;
        esac
    done < "$agent_env_file"
fi
# 固定镜像绝不在运行期下载 OCR/资源；即使外部环境误设 false 也必须保持拒绝。
export OAS_AGENT_NO_RUNTIME_DOWNLOAD=true
# Agent 源码没有作为系统 site-package 安装。显式设置模块搜索路径，保证 init
# 清空 Docker 环境后仍能执行 ``python3 -m oas_android_agent``，并让子进程同时
# 找到内置 OAS runtime。
export PYTHONPATH="/opt/oas-agent:${OAS_AGENT_RUNTIME_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
# Android init 的默认工作目录不是镜像的 WORKDIR。OAS 在导入 logger 时就会按
# 当前目录创建 ``log/``，必须在启动 Python 前固定到内置运行时，避免误写 /log
# 或其它 init 工作目录；任务运行器也因此始终读取 /opt/oas/config。
cd "$OAS_AGENT_RUNTIME_ROOT" || exit 70
exec python3 -m oas_android_agent
