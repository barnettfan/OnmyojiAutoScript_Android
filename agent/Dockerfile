# Build from this project root:
# docker build --build-arg BASE_IMAGE=<redroid-python-base> -f agent/Dockerfile -t oas-redroid-agent:2026.11.26 .
#
# BASE_IMAGE must provide redroid, a CPython runtime compiled for its Android
# userspace, and fixed Android-compatible OAS dependencies: numpy, OpenCV,
# Pillow, lxml, Pydantic, PyYAML, ZeroRPC, requests, adbutils, uiautomator2,
# ONNX Runtime and PaddleOCR. RootShell does not connect ADB, but the existing
# OAS Device inheritance graph imports its ADB/uiautomator compatibility code.
# Keeping that Python base versioned is intentional: production Agent containers
# never pull Git code or resources.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

WORKDIR /opt

# Keep the execution image independent from the Server checkout.  In
# particular, never copy a developer's config/devices JSON, log/error files
# or .git history to a device image: the Server remains the only owner of
# device configuration, and normal screenshots/logs stay inside the device.
# These are the complete Python/runtime inputs used by ``script.py``.
COPY agent/runtime/oas /opt/oas
# Fixed OCR models are supplied by the pinned BASE_IMAGE or a separate image
# layer. They are not committed to Git: production preflight rejects a missing
# model instead of downloading one at runtime.
COPY agent/oas_android_agent /opt/oas-agent/oas_android_agent
COPY agent/requirements.txt /opt/oas-agent/requirements.txt
COPY agent/init/oas_android_agent.rc /system/etc/init/oas_android_agent.rc
COPY agent/init/run-agent.sh /opt/oas-agent/run-agent.sh

# Only the pure-Python transport dependencies are installed here. The original
# OAS requirements file targets desktop Python and includes platform-specific
# packages; its OCR/vision dependencies must already be frozen into BASE_IMAGE.
RUN pip3 install --no-cache-dir -r /opt/oas-agent/requirements.txt
RUN chmod 0755 /opt/oas-agent/run-agent.sh

ENV OAS_AGENT_RUNTIME_ROOT=/opt/oas \
    OAS_AGENT_STATE_ROOT=/var/lib/oas-agent \
    OAS_AGENT_ENV_FILE=/data/oas-agent/agent.env \
    OAS_AGENT_NO_RUNTIME_DOWNLOAD=true \
    PYTHONPATH=/opt/oas-agent:/opt/oas

# Runtime identity, WSS URL and shared secret are supplied by container deployment.
# Android init may discard Docker ENV. ``run-agent.sh`` therefore also loads
# the optional, deployment-mounted OAS_AGENT_ENV_FILE; it is never copied into
# this image or the Server's device JSON.
# Do not replace the redroid image ENTRYPOINT: Android init starts this Agent as
# a late_start service after sys.boot_completed=1.
