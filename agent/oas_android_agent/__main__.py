"""允许以 ``python -m oas_android_agent`` 启动常驻 Agent。"""

from .service import main


if __name__ == "__main__":
    raise SystemExit(main())
