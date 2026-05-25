from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ai_quant_trader.core.models import MacroEntity


class MacroEntityStore:
    """宏观实体库。

    保存会影响交易解释的关键实体，例如美联储主席、美国总统、财政部长、
    重要央行官员等。实体信息不能写死在 Prompt 里，必须可刷新、可审计。
    """

    def __init__(self, path: str = "data/macro_entities.json"):
        self.path = Path(path)

    def load(self) -> list[MacroEntity]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        output: list[MacroEntity] = []
        for item in data if isinstance(data, list) else []:
            try:
                output.append(MacroEntity.model_validate(item))
            except Exception:
                continue
        return output

    def save(self, entities: list[MacroEntity]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps([item.model_dump(mode="json") for item in entities], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def upsert(self, entity: MacroEntity) -> None:
        entities = self.load()
        entities = [item for item in entities if not (item.role == entity.role and item.region == entity.region)]
        entities.append(entity)
        self.save(entities)

    def stale_roles(self, max_age_hours: int) -> list[MacroEntity]:
        cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)
        return [item for item in self.load() if item.observed_at.astimezone(UTC) < cutoff]

    def context_text(self) -> str:
        entities = self.load()
        if not entities:
            return "宏观实体库暂无可用记录。"
        lines = ["宏观实体库："]
        for item in sorted(entities, key=lambda x: (x.region, x.role)):
            observed = item.observed_at.astimezone(UTC).strftime("%Y-%m-%d")
            lines.append(f"- {item.region} {item.role}: {item.name}，来源 {item.source}，校验日期 {observed}")
        return "\n".join(lines)

