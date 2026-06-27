from __future__ import annotations

from pathlib import Path

import yaml

from scripts.ai_sizing_policy_control import set_policy


def test_ai_sizing_policy_control_switches_policy_and_creates_backup(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "risk": {
                    "max_total_leverage": 4.0,
                    "ai_sizing_policy": "legacy_factor_ranked",
                }
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = set_policy(config, "calibrated_v1_controlled", max_tier_lift=1, min_factor_coverage=0.7)

    updated = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert result["previous_policy"] == "legacy_factor_ranked"
    assert result["new_policy"] == "calibrated_v1_controlled"
    assert Path(result["backup"]).exists()
    assert updated["risk"]["ai_sizing_policy"] == "calibrated_v1_controlled"
    assert updated["risk"]["calibrated_max_tier_lift"] == 1
    assert updated["risk"]["calibrated_min_factor_coverage"] == 0.7
    assert "legacy_factor_ranked" in result["rollback_command"]
    assert result["legacy_rollback_command"].endswith("--policy legacy_factor_ranked")
