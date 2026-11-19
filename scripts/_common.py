"""Shared setup for the command-line scripts."""

import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")


def setup_logging(log_file: str | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        path = ROOT / "data" / "logs" / log_file
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(path, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers)
    # Scrapling logs every fetch through its own handler as well; keep warnings only.
    scrapling = logging.getLogger("scrapling")
    scrapling.setLevel(logging.WARNING)
    scrapling.handlers.clear()


def sources_config() -> dict:
    return yaml.safe_load((ROOT / "config" / "sources.yaml").read_text("utf-8"))


def database_url() -> str | None:
    return os.environ.get("DATABASE_URL") or None
