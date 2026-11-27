# OnmyojiAutoScript Android C/S

Standalone control-plane/execution-plane project for Root Android phones and
redroid. The repository contains both sides:

- `server/`: FastAPI control plane, device configuration, scheduling, WSS, and
  the administrator UI.
- `agent/`: Android/redroid Agent, RootShell backend, task runner, immutable OAS
  runtime source, and image/init packaging.

Normal screenshots, OCR input, and task logs remain in `agent/` at runtime. The
only device artifacts sent to `server/` are failure bundles containing error
screenshots, a log tail, and sanitized context.

See `agent/README.md` and `server/README.md` for their startup requirements.
