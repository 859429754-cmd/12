from __future__ import annotations

from pathlib import Path


SUSPICIOUS_MOJIBAKE = (
    "妯",
    "绛",
    "暐",
    "鍙",
    "鎺",
    "歿",
    "乨",
    "乵",
    "乿",
    "鐨",
    "閽",
    "拤",
)


def test_utf8_core_files_do_not_contain_common_mojibake() -> None:
    protected = [
        Path("ai_quant_trader/api/server.py"),
        Path("ai_quant_trader/brain/deepseek.py"),
        Path("ai_quant_trader/data/news.py"),
        Path("ai_quant_trader/data/news_memory.py"),
        Path("ai_quant_trader/brain/knowledge.py"),
        Path("ai_quant_trader/brain/wakeup.py"),
        Path("ai_quant_trader/data/macro_entities.py"),
        Path("tests/test_console_api.py"),
    ]
    for path in protected:
        text = path.read_text(encoding="utf-8")
        assert not any(token in text for token in SUSPICIOUS_MOJIBAKE), path
