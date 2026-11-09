"""Tests for codex_jobs.models: tier lookup in models.sh and validation against the catalog."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from codex_jobs.models import ModelResolutionError, resolve_model

MODELS_SH = """# Codex model slug per tier.
CODEX_MODEL_SOL=gpt-6.1-sol
CODEX_MODEL_ASTRA=gpt-6-astra
CODEX_MODEL_LUNA=gpt-6-luna
CODEX_MODEL_TERRA=gpt-6.1-terra
"""
FULL = ["low", "medium", "high", "xhigh", "max"]


def _entry(slug: str, efforts: list[str]) -> dict:
    return {
        "slug": slug,
        "display_name": slug,
        "default_reasoning_level": "medium",
        "supported_reasoning_levels": [{"effort": e, "description": e} for e in efforts],
        "visibility": "list",
    }


@pytest.fixture
def paths(tmp_path):
    models_sh = tmp_path / "models.sh"
    models_sh.write_text(MODELS_SH)
    catalog = tmp_path / "models_cache.json"
    catalog.write_text(
        json.dumps(
            {
                "fetched_at": "2026-11-09T00:20:39.346784Z",
                "models": [
                    _entry("gpt-6.1-sol", [*FULL, "ultra"]),
                    _entry("gpt-6-astra", [*FULL, "ultra"]),
                    _entry("gpt-6.1-terra", [*FULL, "ultra"]),
                    _entry("gpt-6-luna", FULL),
                ],
            }
        )
    )
    return {"catalog_path": catalog, "models_sh_path": models_sh}


def test_tier_resolves_to_slug_with_catalog_timestamp(paths):
    resolved = resolve_model("terra", "high", **paths)
    assert (resolved.tier, resolved.slug, resolved.effort) == ("terra", "gpt-6.1-terra", "high")
    assert resolved.fetched_at == "2026-11-09T00:20:39.346784Z"


def test_raw_slug_in_catalog_is_accepted(paths):
    resolved = resolve_model("gpt-6-astra", "low", **paths)
    assert (resolved.tier, resolved.slug) == (None, "gpt-6-astra")


def test_raw_slug_missing_from_catalog_is_refused(paths):
    with pytest.raises(ModelResolutionError) as err:
        resolve_model("gpt-9-nova", "low", **paths)
    assert "gpt-9-nova" in str(err.value)
    assert "gpt-6-luna" in str(err.value)


def test_ultra_on_luna_refused_with_its_effort_list(paths):
    with pytest.raises(ModelResolutionError) as err:
        resolve_model("luna", "ultra", **paths)
    assert "low, medium, high, xhigh, max" in str(err.value)
    assert "ultra" not in str(err.value).split("valid efforts:")[1]


def test_ultra_on_sol_is_accepted(paths):
    assert resolve_model("sol", "ultra", **paths).effort == "ultra"


def test_missing_catalog_raises_with_its_path(paths, tmp_path):
    missing = tmp_path / "absent" / "models_cache.json"
    with pytest.raises(ModelResolutionError) as err:
        resolve_model("sol", "high", catalog_path=missing, models_sh_path=paths["models_sh_path"])
    assert str(missing) in str(err.value)


def test_tier_pinned_to_a_slug_outside_the_catalog_is_refused(paths):
    paths["models_sh_path"].write_text(MODELS_SH.replace("gpt-6-luna", "gpt-7-luna"))
    with pytest.raises(ModelResolutionError, match="gpt-7-luna"):
        resolve_model("luna", "low", **paths)
