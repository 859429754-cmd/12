from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


PRIMARY_DEEPSEEK_KEYS = ["DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"]
BACKUP_DEEPSEEK_KEYS = ["DEEPSEEK_BACKUP_API_KEY"]

TREND_GATE_KEYS = ["GATEIO_TREND_API_KEY", "GATEIO_TREND_API_SECRET"]
FOLLOWER_GATE_KEYS = ["GATEIO_FOLLOWER_API_KEY", "GATEIO_FOLLOWER_API_SECRET"]
LEGACY_GATE_KEYS = ["GATEIO_API_KEY", "GATEIO_API_SECRET"]

CONSOLE_ACCOUNT_KEYS = [
    "CONSOLE_ADMIN_USER",
    "CONSOLE_ADMIN_PASSWORD",
    "CONSOLE_ACCOUNT1_USER",
    "CONSOLE_ACCOUNT1_PASSWORD",
    "CONSOLE_ACCOUNT2_USER",
    "CONSOLE_ACCOUNT2_PASSWORD",
]

LARGE_FUNDS_KEYS = ["CONSOLE_PASSWORD_STRENGTH_CONFIRMED"]

WEAK_PASSWORDS = {
    "123456",
    "1234567",
    "12345678",
    "password",
    "admin",
    "account1",
    "account2",
    "yx",
    "wx",
}


@dataclass(frozen=True)
class KeyStatus:
    present: bool
    nonempty: bool
    length: int


@dataclass(frozen=True)
class RuntimeEnvAuditResult:
    ok: bool
    mode: str
    env_file: str
    missing: list[str]
    empty: list[str]
    failures: list[str]
    warnings: list[str]
    keys: dict[str, KeyStatus]

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["keys"] = {key: asdict(status) for key, status in self.keys.items()}
        return payload


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def key_status(values: dict[str, str], keys: Iterable[str]) -> dict[str, KeyStatus]:
    return {
        key: KeyStatus(
            present=key in values,
            nonempty=bool(values.get(key, "").strip()),
            length=len(values.get(key, "")),
        )
        for key in keys
    }


def truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def missing_or_empty(values: dict[str, str], required: Iterable[str]) -> tuple[list[str], list[str]]:
    missing = [key for key in required if key not in values]
    empty = [key for key in required if key in values and not values.get(key, "").strip()]
    return missing, empty


def has_pair(values: dict[str, str], pair: Iterable[str]) -> bool:
    return all(values.get(key, "").strip() for key in pair)


def weak_console_passwords(values: dict[str, str]) -> list[str]:
    weak: list[str] = []
    for key in ["CONSOLE_ADMIN_PASSWORD", "CONSOLE_ACCOUNT1_PASSWORD", "CONSOLE_ACCOUNT2_PASSWORD"]:
        value = values.get(key, "")
        normalized = value.strip().lower()
        if not value:
            continue
        if normalized in WEAK_PASSWORDS or len(value) < 10 or not re.search(r"[A-Za-z]", value) or not re.search(r"\d", value):
            weak.append(key)
    return weak


def audit_runtime_env(path: Path, mode: str = "base", allow_weak_passwords: bool = False) -> RuntimeEnvAuditResult:
    values = parse_env_file(path)
    mode = mode.lower().replace("_", "-")
    if mode not in {"base", "cloud-live"}:
        raise ValueError(f"unsupported mode: {mode}")

    required: list[str] = [*PRIMARY_DEEPSEEK_KEYS]
    warnings: list[str] = []
    failures: list[str] = []

    if mode == "cloud-live":
        required.extend([*BACKUP_DEEPSEEK_KEYS, *TREND_GATE_KEYS, *FOLLOWER_GATE_KEYS, *CONSOLE_ACCOUNT_KEYS, *LARGE_FUNDS_KEYS])
    missing, empty = missing_or_empty(values, required)

    if not path.exists():
        failures.append("env_file_missing")

    if mode == "base":
        if not has_pair(values, TREND_GATE_KEYS) and not has_pair(values, LEGACY_GATE_KEYS):
            failures.append("missing_gate_credentials_pair")
        if has_pair(values, LEGACY_GATE_KEYS) and not has_pair(values, TREND_GATE_KEYS):
            warnings.append("using_legacy_gate_credentials")

    if mode == "cloud-live":
        if truthy(values.get("CONSOLE_AUTH_DISABLED")):
            failures.append("console_auth_disabled")
        if not truthy(values.get("CONSOLE_PASSWORD_STRENGTH_CONFIRMED")):
            failures.append("password_strength_not_confirmed")
        weak = weak_console_passwords(values)
        if weak and not allow_weak_passwords:
            failures.extend([f"weak_password:{key}" for key in weak])
        elif weak:
            warnings.extend([f"weak_password_allowed:{key}" for key in weak])
        if not values.get("CONSOLE_CORS_ORIGINS", "").strip():
            warnings.append("console_cors_origins_empty")
        if values.get("GATEIO_RANGE_API_KEY") and not values.get("GATEIO_RANGE_API_SECRET"):
            warnings.append("range_gate_key_without_secret")
        if values.get("GATEIO_RANGE_API_SECRET") and not values.get("GATEIO_RANGE_API_KEY"):
            warnings.append("range_gate_secret_without_key")

    tracked_keys = sorted(set(required + TREND_GATE_KEYS + FOLLOWER_GATE_KEYS + LEGACY_GATE_KEYS + ["CONSOLE_AUTH_DISABLED", "CONSOLE_CORS_ORIGINS", "GATEIO_RANGE_API_KEY", "GATEIO_RANGE_API_SECRET"]))
    ok = not missing and not empty and not failures
    return RuntimeEnvAuditResult(
        ok=ok,
        mode=mode,
        env_file=str(path),
        missing=missing,
        empty=empty,
        failures=failures,
        warnings=warnings,
        keys=key_status(values, tracked_keys),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit .env.runtime completeness without printing secret values.")
    parser.add_argument("--env-file", default=".env.runtime", help="Path to .env.runtime.")
    parser.add_argument("--mode", choices=["base", "cloud-live"], default="base")
    parser.add_argument("--allow-weak-passwords", action="store_true", help="Allow weak console passwords for small-funds grey testing only.")
    parser.add_argument("--json-out", help="Optional JSON output path.")
    args = parser.parse_args()

    result = audit_runtime_env(Path(args.env_file).resolve(), mode=args.mode, allow_weak_passwords=args.allow_weak_passwords)
    payload = result.to_dict()
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    print(text)
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(text + "\n", encoding="utf-8")
    raise SystemExit(0 if result.ok else 1)


if __name__ == "__main__":
    main()
