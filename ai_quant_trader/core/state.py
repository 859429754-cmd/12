from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RuntimeState:
    """运行期交易与报告开关。

    开仓授权和分析报告开关分离：
    - enabled_symbols 控制某个标的是否允许新开仓。
    - report_symbols 控制小时简报是否分析并展示某个标的。
    - major_news_only 控制新闻部分是否只推送重要宏观、政治、加密快讯。
    """

    opening_paused: bool = True
    enabled_symbols: set[str] = field(default_factory=set)
    report_symbols: set[str] = field(default_factory=set)
    major_news_only: bool = False

    def authorize_symbol(self, symbol: str) -> None:
        self.enabled_symbols.add(symbol)
        self.report_symbols.add(symbol)

    def pause_symbol(self, symbol: str) -> None:
        self.enabled_symbols.discard(symbol)

    def enable_report(self, symbol: str) -> None:
        self.report_symbols.add(symbol)

    def disable_report(self, symbol: str) -> None:
        self.report_symbols.discard(symbol)
        self.enabled_symbols.discard(symbol)

    def can_open(self, symbol: str) -> bool:
        return not self.opening_paused and symbol in self.enabled_symbols

    def should_report(self, symbol: str) -> bool:
        return symbol in self.report_symbols
