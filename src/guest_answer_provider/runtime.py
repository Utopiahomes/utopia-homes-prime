"""Process entrypoint: `python -m guest_answer_provider.runtime`.

Fail-closed startup gate: a malformed environment must never fall back to a default runtime
identity or a partially-configured legacy upstream — it must refuse to start.
"""

from __future__ import annotations

import logging
import sys

import uvicorn

from guest_answer_provider.api import create_app
from guest_answer_provider.config import Config, ConfigError


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    try:
        config = Config.from_environment()
    except ConfigError as exc:
        print(f"guest answer provider startup gate failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    app = create_app(config=config)
    uvicorn.run(app, host="0.0.0.0", port=config.port, log_level=config.log_level.lower())


if __name__ == "__main__":
    main()
