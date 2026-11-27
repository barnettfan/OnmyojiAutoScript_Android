#!/bin/sh
# Runs inside Debian PRoot. PRoot discards parent environment variables, so this
# script reloads only approved Agent settings from the Root-owned configuration.
agent_root="${OAS_AGENT_PROOT_HOME:-/opt/oas-agent}"
agent_env_file="${agent_root}/agent.env"
runtime_root="${agent_root}/runtime/oas"

# Plain ws:// may only be enabled by the Root-owned configuration file.
unset OAS_AGENT_ALLOW_INSECURE_WS

if [ -r "$agent_env_file" ]; then
    while IFS='=' read -r agent_env_name agent_env_value || [ -n "$agent_env_name" ]; do
        case "$agent_env_value" in
            \"*\") agent_env_value="${agent_env_value#\"}"; agent_env_value="${agent_env_value%\"}" ;;
            \'*\') agent_env_value="${agent_env_value#\'}"; agent_env_value="${agent_env_value%\'}" ;;
        esac
        case "$agent_env_name" in
            ''|'#'*)
                continue
                ;;
            OAS_AGENT_DEVICE_ID|OAS_AGENT_SERVER_URL|OAS_AGENT_SHARED_SECRET|OAS_AGENT_STATE_ROOT|OAS_AGENT_VERSION|OAS_AGENT_RESOURCE_VERSION|OAS_AGENT_HEARTBEAT_SECONDS|OAS_AGENT_ALLOW_INSECURE_WS|OAS_AGENT_PYTHON_BIN)
                export "$agent_env_name=$agent_env_value"
                ;;
            OAS_AGENT_ROOT_SU_BIN|OAS_AGENT_PROOT_DISTRO|OAS_AGENT_PROOT_BIN|OAS_AGENT_TERMUX_PREFIX)
                export "$agent_env_name=$agent_env_value"
                ;;
            *)
                # The outer Android-root launcher validates the complete file.
                # Ignore keys owned only by that launcher instead of coupling
                # the PRoot child to its host-runtime settings.
                continue
                ;;
        esac
    done < "$agent_env_file"
fi

if [ ! -d "$runtime_root" ] || [ ! -d "${agent_root}/oas_android_agent" ]; then
    echo "OAS Agent payload is incomplete under ${agent_root}" >&2
    exit 66
fi

# Convert the Android-side state path to the explicit PRoot bind mount.
case "${OAS_AGENT_STATE_ROOT:-}" in
    /data/local/oas-agent)
        export OAS_AGENT_STATE_ROOT="$agent_root"
        ;;
    /data/local/oas-agent/*)
        export OAS_AGENT_STATE_ROOT="${agent_root}/${OAS_AGENT_STATE_ROOT#/data/local/oas-agent/}"
        ;;
    '')
        export OAS_AGENT_STATE_ROOT="${agent_root}/state"
        ;;
esac

python_bin="${OAS_AGENT_PYTHON_BIN:-python3}"
if [ ! -x "$python_bin" ]; then
    python_bin="$(command -v "$python_bin" 2>/dev/null || true)"
fi
if [ -z "$python_bin" ] || [ ! -x "$python_bin" ]; then
    echo "OAS_AGENT_PYTHON_BIN is not executable: ${OAS_AGENT_PYTHON_BIN:-python3}" >&2
    exit 69
fi

# All task OCR/image services run locally in the PRoot process. Android device
# access still uses OAS_AGENT_ROOT_SU_BIN, which points at the true system su.
export OAS_AGENT_RUNTIME_ROOT="$runtime_root"
export OAS_AGENT_NO_RUNTIME_DOWNLOAD=true
export PYTHONPATH="${agent_root}:${runtime_root}${PYTHONPATH:+:${PYTHONPATH}}"
cd "$runtime_root" || exit 70
exec "$python_bin" -m oas_android_agent
