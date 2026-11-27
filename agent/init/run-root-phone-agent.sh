#!/system/bin/sh
# Root phone launcher. The complete ``agent/`` directory is deployed to
# /data/local/oas-agent; code and assets stay immutable there while state and
# secrets are kept in separate runtime paths below the same private directory.
agent_root="${OAS_AGENT_HOME:-/data/local/oas-agent}"
agent_env_file="${OAS_AGENT_ENV_FILE:-${agent_root}/agent.env}"

# Do not let a mounted environment redirect task code away from the installed
# payload. Only the state directory may be overridden per phone deployment.
export OAS_AGENT_RUNTIME_ROOT="${agent_root}/runtime/oas"
export OAS_AGENT_STATE_ROOT="${OAS_AGENT_STATE_ROOT:-${agent_root}/state}"
# Plain ws:// must be an explicit value in the root-owned agent.env file, not a
# process environment value inherited from an Android service manager.
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
            OAS_AGENT_PROOT_DISTRO|OAS_AGENT_PROOT_BIN|OAS_AGENT_ROOT_SU_BIN|OAS_AGENT_TERMUX_PREFIX)
                export "$agent_env_name=$agent_env_value"
                ;;
            *)
                echo "Invalid key in OAS agent environment file: $agent_env_name" >&2
                exit 64
                ;;
        esac
    done < "$agent_env_file"
fi

if [ ! -d "$OAS_AGENT_RUNTIME_ROOT" ] || [ ! -d "${agent_root}/oas_android_agent" ]; then
    echo "OAS Agent payload is incomplete under ${agent_root}" >&2
    exit 66
fi

# The deployment directory contains the shared secret and must be started by
# Android Root. The PRoot runtime below still uses OAS_AGENT_ROOT_SU_BIN for
# screenshot/input because its apparent uid 0 is a userspace mapping only.
if [ "$(id -u)" != "0" ]; then
    echo "Start OAS Agent through Android Root: su -c $0" >&2
    exit 67
fi

# The phone must provide an Android-compatible Python interpreter and its fixed
# OAS/OCR dependencies. This launcher intentionally never downloads them.
python_bin="${OAS_AGENT_PYTHON_BIN:-python3}"

export OAS_AGENT_NO_RUNTIME_DOWNLOAD=true
export PYTHONPATH="${agent_root}:${OAS_AGENT_RUNTIME_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
cd "$OAS_AGENT_RUNTIME_ROOT" || exit 70

# Debian in Termux PRoot supplies ARM64 Linux wheels for the image/OCR stack.
# It remains a normal process on this phone; OAS_AGENT_ROOT_SU_BIN makes device
# operations cross back to Android's real Root shell when required.
if [ -n "${OAS_AGENT_PROOT_DISTRO:-}" ]; then
    # proot-distro is installed by Termux and needs its binary and shared
    # library directories even though this launcher itself runs as Android Root.
    if [ -n "${OAS_AGENT_TERMUX_PREFIX:-}" ]; then
        if [ ! -d "${OAS_AGENT_TERMUX_PREFIX}/bin" ] || [ ! -d "${OAS_AGENT_TERMUX_PREFIX}/lib" ]; then
            echo "OAS_AGENT_TERMUX_PREFIX is incomplete: ${OAS_AGENT_TERMUX_PREFIX}" >&2
            exit 71
        fi
        export PATH="${OAS_AGENT_TERMUX_PREFIX}/bin:${PATH}"
        export LD_LIBRARY_PATH="${OAS_AGENT_TERMUX_PREFIX}/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
    fi
    proot_bin="${OAS_AGENT_PROOT_BIN:-proot-distro}"
    if [ ! -x "$proot_bin" ]; then
        proot_bin="$(command -v "$proot_bin" 2>/dev/null || true)"
    fi
    if [ -z "$proot_bin" ] || [ ! -x "$proot_bin" ]; then
        echo "OAS_AGENT_PROOT_BIN is not executable: ${OAS_AGENT_PROOT_BIN:-proot-distro}" >&2
        exit 72
    fi
    # A PRoot filesystem does not inherit Android Root's access to /data/local.
    # Bind the protected payload explicitly. The child launcher reloads the
    # whitelisted agent.env inside PRoot because proot-distro clears inherited
    # OAS_AGENT_* values, including the shared secret.
    proot_agent_root="/opt/oas-agent"
    exec "$proot_bin" login "$OAS_AGENT_PROOT_DISTRO" --shared-tmp \
        --bind "${agent_root}:${proot_agent_root}" -- \
        "${proot_agent_root}/init/run-proot-phone-agent.sh"
fi

if [ ! -x "$python_bin" ]; then
    python_bin="$(command -v "$python_bin" 2>/dev/null || true)"
fi
if [ -z "$python_bin" ] || [ ! -x "$python_bin" ]; then
    echo "OAS_AGENT_PYTHON_BIN is not executable: ${OAS_AGENT_PYTHON_BIN:-python3}" >&2
    exit 69
fi

exec "$python_bin" -m oas_android_agent
