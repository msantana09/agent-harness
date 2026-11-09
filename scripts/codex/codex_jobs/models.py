"""Resolve a model tier or raw slug plus an effort against the Codex model catalog.

Tiers map to slugs in `scripts/codex/models.sh` (`CODEX_MODEL_<TIER>=slug`); the catalog
`~/.codex/models_cache.json` says which slugs exist and which efforts each one supports.
Both paths are parameters; the caller supplies the real ones.
"""

import json
from dataclasses import dataclass
from pathlib import Path

TIERS = ("luna", "terra", "sol", "astra")


class ModelResolutionError(Exception):
    """The requested model or effort cannot be used; the message lists the valid values."""


@dataclass(frozen=True)
class Resolution:
    tier: str | None
    slug: str
    effort: str
    supported_efforts: tuple[str, ...]
    fetched_at: str | None


def load_tiers(models_sh_path: Path) -> dict[str, str]:
    """Read tier -> slug from models.sh, the one parser of that file."""
    models_sh_path = Path(models_sh_path)
    try:
        text = models_sh_path.read_text()
    except FileNotFoundError:
        raise ModelResolutionError(f"tier file not found: {models_sh_path}") from None
    values: dict[str, str] = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            key, sep, value = line.partition("=")
            if not sep:
                raise ModelResolutionError(f"{models_sh_path}: not a KEY=value line: {line!r}")
            values[key] = value
    tiers = {}
    for tier in TIERS:
        key = f"CODEX_MODEL_{tier.upper()}"
        if key not in values:
            raise ModelResolutionError(f"{models_sh_path} has no {key}")
        tiers[tier] = values[key]
    return tiers


def load_catalog(catalog_path: Path) -> tuple[dict[str, tuple[str, ...]], str | None]:
    """Return ({slug: supported efforts}, fetched_at) from the Codex model catalog."""
    catalog_path = Path(catalog_path)
    try:
        raw = json.loads(catalog_path.read_text())
    except FileNotFoundError:
        raise ModelResolutionError(f"model catalog not found: {catalog_path}") from None
    except json.JSONDecodeError as exc:
        raise ModelResolutionError(
            f"model catalog {catalog_path} is not valid JSON: {exc}"
        ) from None
    try:
        models = {
            entry["slug"]: tuple(level["effort"] for level in entry["supported_reasoning_levels"])
            for entry in raw["models"]
        }
    except (KeyError, TypeError) as exc:
        raise ModelResolutionError(
            f"model catalog {catalog_path} lacks models[].slug / supported_reasoning_levels[].effort ({exc!r})"
        ) from None
    return models, raw.get("fetched_at")


def resolve_model(
    tier_or_slug: str, effort: str, *, catalog_path: Path, models_sh_path: Path
) -> Resolution:
    """Return the validated (slug, effort) for a tier or raw slug.

    Raises `ModelResolutionError` for a slug missing from the catalog or an effort the slug
    does not list; the message names the valid values.
    """
    tiers = load_tiers(models_sh_path)
    catalog, fetched_at = load_catalog(catalog_path)

    tier = tier_or_slug if tier_or_slug in tiers else None
    slug = tiers[tier] if tier else tier_or_slug
    if slug not in catalog:
        if tier:
            raise ModelResolutionError(
                f"tier {tier!r} maps to {slug!r} in {models_sh_path}, which is not in the catalog "
                f"{catalog_path}; catalog slugs: {', '.join(catalog)}"
            )
        raise ModelResolutionError(
            f"unknown model {tier_or_slug!r}; tiers: {', '.join(tiers)}; "
            f"catalog slugs: {', '.join(catalog)}"
        )
    supported = catalog[slug]
    if effort not in supported:
        raise ModelResolutionError(
            f"effort {effort!r} is not supported by {slug}; valid efforts: {', '.join(supported)}"
        )
    return Resolution(
        tier=tier, slug=slug, effort=effort, supported_efforts=supported, fetched_at=fetched_at
    )
