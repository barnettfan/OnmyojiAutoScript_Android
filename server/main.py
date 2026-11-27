"""Run the standalone OAS Android control plane."""
from __future__ import annotations

import os

import uvicorn


def main() -> None:
    """Start the HTTP/WSS control plane without a local OAS execution runtime."""
    uvicorn.run(
        "app:app",
        host=os.environ.get("OAS_CONTROL_PLANE_HOST", "0.0.0.0"),
        port=int(os.environ.get("OAS_CONTROL_PLANE_PORT", "22270")),
    )


if __name__ == "__main__":
    main()
