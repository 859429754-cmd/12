from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_console_vite_splits_chart_vendor_without_lazy_chart_import() -> None:
    vite_config = (REPO_ROOT / "console" / "vite.config.ts").read_text(encoding="utf-8")
    app_source = (REPO_ROOT / "console" / "src" / "App.tsx").read_text(encoding="utf-8")

    assert "manualChunks" in vite_config
    assert "lightweight-charts" in vite_config
    assert "chart-vendor" in vite_config
    assert 'lazy(() => import("./MarketChart")' not in app_source
    assert 'import { MarketChart } from "./MarketChart";' in app_source
