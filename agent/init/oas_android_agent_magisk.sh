#!/system/bin/sh
# Copy this file to /data/adb/service.d/oas_android_agent.sh on a Magisk-rooted
# phone. Magisk runs service.d scripts as root after Android userspace starts.
agent_root="/data/local/oas-agent"
agent_runner="${agent_root}/init/run-root-phone-agent.sh"
agent_log="${agent_root}/agent.log"

# Wait until Surface, PackageManager and the RootShell commands used by the
# Agent preflight are available. Restart only after the launcher exits.
while [ "$(getprop sys.boot_completed)" != "1" ]; do
    sleep 2
done

while true; do
    "$agent_runner" >> "$agent_log" 2>&1
    sleep 10
done
