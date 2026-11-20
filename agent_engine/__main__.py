"""Run the Agent Engine: ``python -m agent_engine [--host H] [--port P]``."""
from __future__ import annotations

import argparse
import logging

import uvicorn

from .app import create_app
from .config import EngineConfig


def main():
    parser = argparse.ArgumentParser(description="Code Studio Agent Engine")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    config = EngineConfig.from_env()
    if args.host:
        config.host = args.host
    if args.port:
        config.port = args.port
    logging.basicConfig(level=logging.INFO)
    if config.host not in ("127.0.0.1", "localhost", "::1"):
        logging.warning("Engine bound to %s: put it behind TLS / a private network", config.host)
    uvicorn.run(create_app(config), host=config.host, port=config.port, log_level="info")


if __name__ == "__main__":
    main()
