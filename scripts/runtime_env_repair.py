from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path


DEFAULT_NON_SECRET_VALUES = {
    "CONSOLE_ADMIN_USER": "admin",
    "CONSOLE_ACCOUNT1_USER": "account1",
    "CONSOLE_ACCOUNT2_USER": "account2",
    "CONSOLE_RANGE_USER": "range",
    "CONSOLE_COOKIE_SECURE": "0",
}


@dataclass(frozen=True)
class RuntimeEnvRepairResult:
    ok: bool
    env_file: str
    dry_run: bool
    backup_file: str | None
    changed_keys: list[str]
    warnings: list[str]


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, value = line.split("=", 1)
        clean_key = key.strip().lstrip("\ufeff")
        if clean_key:
            values[clean_key] = value.strip().strip('"').strip("'")
    return values


def render_env(values: dict[str, str]) -> str:
    lines = [f"{key}={values[key]}" for key in sorted(values)]
    return "\n".join(lines) + "\n"


def repair_runtime_env(path: Path, *, cors_origin: str | None = None, apply: bool = False) -> RuntimeEnvRepairResult:
    original_text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    values = parse_env(path)
    changed: list[str] = []
    warnings: list[str] = []

    if original_text.startswith("\ufeff") and "DEEPSEEK_API_KEY" in values:
        changed.append("strip_utf8_bom")

    if values.get("GATEIO_API_KEY") and values.get("GATEIO_API_SECRET"):
        if not values.get("GATEIO_TREND_API_KEY"):
            values["GATEIO_TREND_API_KEY"] = values["GATEIO_API_KEY"]
            changed.append("GATEIO_TREND_API_KEY")
        if not values.get("GATEIO_TREND_API_SECRET"):
            values["GATEIO_TREND_API_SECRET"] = values["GATEIO_API_SECRET"]
            changed.append("GATEIO_TREND_API_SECRET")
    else:
        warnings.append("legacy_gate_pair_missing")

    for key, value in DEFAULT_NON_SECRET_VALUES.items():
        if not values.get(key):
            values[key] = value
            changed.append(key)

    if cors_origin and not values.get("CONSOLE_CORS_ORIGINS"):
        values["CONSOLE_CORS_ORIGINS"] = cors_origin
        changed.append("CONSOLE_CORS_ORIGINS")

    backup_file: str | None = None
    if apply and changed:
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(path.name + f".bak-{timestamp}")
        if path.exists():
            backup.write_text(original_text, encoding="utf-8")
            backup_file = str(backup)
        path.write_text(render_env(values), encoding="utf-8")

    return RuntimeEnvRepairResult(
        ok=True,
        env_file=str(path),
        dry_run=not apply,
        backup_file=backup_file,
        changed_keys=changed,
        warnings=warnings,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Repair non-secret .env.runtime portability issues without printing secret values.")
    parser.add_argument("--env-file", default=".env.runtime")
    parser.add_argument("--cors-origin", default=None)
    parser.add_argument("--apply", action="store_true", help="Write repaired env file and create a backup.")
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args()

    result = repair_runtime_env(Path(args.env_file), cors_origin=args.cors_origin, apply=args.apply)
    text = json.dumps(asdict(result), ensure_ascii=False, indent=2)
    print(text)
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
