# OAS Android Agent

This is the Android/redroid execution plane. It opens an outbound WSS connection
to the standalone control plane and executes all screenshot, OCR, task-state,
and RootShell input work locally.

Build from the repository root:

```sh
docker build --build-arg BASE_IMAGE=<redroid-python-base> \
  -f agent/Dockerfile -t oas-redroid-agent:2026.11.26 .
```

The base image must provide Android-compatible CPython and the fixed OAS vision
dependencies. It must also contain the pinned OCR model files at
`/opt/oas/toolkit/.paddlex`; the models are intentionally not committed to Git.

Deployment injects these values through `/data/oas-agent/agent.env`:

```text
OAS_AGENT_DEVICE_ID=redroid-cn-01
OAS_AGENT_SERVER_URL=wss://oas.example.com
OAS_AGENT_SHARED_SECRET=<secret>
OAS_AGENT_VERSION=2026.11.26
OAS_AGENT_RESOURCE_VERSION=2026.11.26
```

Android init starts `python3 -m oas_android_agent` only after
`sys.boot_completed=1`. The Agent does not expose or connect to ADB.
