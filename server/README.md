# OAS Android Control Plane

The Server stores per-device JSON configuration, grants one task lease per
device, receives task results and scheduling patches, and exposes the protected
device-management UI. It never runs an OAS task or receives normal screenshots,
OCR input, or normal Agent logs.

Install and run:

```sh
python -m pip install -r server/requirements.txt
set OAS_AGENT_SHARED_SECRET=<agent-shared-secret>
set OAS_AGENT_ADMIN_PASSWORD=<admin-password>
cd server
python main.py
```

On Linux/macOS, use `export` instead of `set`. The management UI is available at
`/agents/ui`; the Agent connects to `/agent/ws`. New devices are created from
`config/template.agent-device.json` through the management API/UI. Device
JSON and uploaded error bundles are runtime data and are ignored by Git.
