from __future__ import annotations

import uvicorn

from zhimeng_runtime.config import RuntimeSettings


def main() -> None:
    settings = RuntimeSettings.from_environment()
    uvicorn.run(
        "zhimeng_runtime.app:app",
        host=settings.host,
        port=settings.port,
        factory=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
