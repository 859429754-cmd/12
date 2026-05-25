from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class KnowledgeSection:
    name: str
    content: str


class TradingKnowledgeBase:
    """本地交易知识库。

    大模型本身不可靠地保存长期记忆，所以这里把交易员经验、策略说明、
    风控规则和消息面框架放在本地文件中。每次调用 AI 时，系统可以按场景
    读取相关章节注入上下文，做到可版本化、可审计、可回滚。
    """

    def __init__(self, root: str = "knowledge"):
        self.root = Path(root)

    def load_sections(self, names: list[str] | None = None) -> list[KnowledgeSection]:
        if not self.root.exists():
            return []
        files = sorted(self.root.glob("*.md"))
        wanted = set(names or [])
        sections: list[KnowledgeSection] = []
        for path in files:
            stem = path.stem
            if wanted and stem not in wanted:
                continue
            sections.append(KnowledgeSection(name=stem, content=path.read_text(encoding="utf-8")))
        return sections

    def build_context(self, names: list[str] | None = None, max_chars: int = 12_000) -> str:
        chunks: list[str] = []
        used = 0
        for section in self.load_sections(names):
            text = f"\n## {section.name}\n{section.content.strip()}\n"
            if used + len(text) > max_chars:
                break
            chunks.append(text)
            used += len(text)
        return "\n".join(chunks).strip()

