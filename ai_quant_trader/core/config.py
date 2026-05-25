from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from ai_quant_trader.core.models import AppConfig


def load_dotenv(path: str | Path = ".env") -> None:
    """轻量级 .env 加载，避免为部署额外引入依赖。"""

    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        clean_key = key.strip().lstrip("\ufeff")
        if clean_key:
            os.environ.setdefault(clean_key, value.strip().strip('"').strip("'"))


def load_config(path: str | Path = "config/config.yaml") -> AppConfig:
    load_dotenv()
    load_dotenv(".env.runtime")
    config_path = Path(path)
    data: dict[str, Any] = {}
    if config_path.exists():
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return AppConfig.model_validate(data)
