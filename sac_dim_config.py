"""
Master-data column-ROLE config for the Insights & Reporting Filters menu
(ID/Description lookup, hierarchy parent detection) — config/master_data_columns.yaml.

WHY THIS MODULE EXISTS: different SAC tenants/models name their master-data
description/hierarchy-parent columns differently (Description vs Text vs
ShortText vs ParentId vs Parent...). Hardcoding a pattern guess in Python for
one model's naming isn't reusable across models — this module makes the
pattern list AND per-dimension exact overrides live in one editable YAML
file instead, so a wrong guess is fixed by editing data, never code.

Everything here degrades gracefully: a missing/unreadable config file falls
back to the built-in defaults below, and a failed save just means an
override didn't stick — never a crash, never a hard dependency for the
Filters UI (which always still works on raw ids regardless).
"""
from __future__ import annotations

import os
from typing import Any

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "config", "master_data_columns.yaml")

_BUILTIN_DEFAULTS: dict[str, list[str]] = {
    "description": ["description", "text", "shorttext", "mediumtext", "longtext", "name"],
    "parent": ["parentid", "parent", "parentmember", "parentmemberid",
               "parentnode", "parentnodeid", "hierarchyparent"],
}

_cache: dict[str, Any] = {}


def _load() -> dict[str, Any]:
    if "cfg" not in _cache:
        try:
            import yaml
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            _cache["cfg"] = data if isinstance(data, dict) else {}
        except Exception:
            _cache["cfg"] = {}
    return _cache["cfg"]


_FILE_HEADER = (
    "# Master-data COLUMN-ROLE mapping for the Insights & Reporting Filters menu.\n"
    "# Auto-saved by the Filters ⋮ menu's 'Show raw columns' picker (or edit by\n"
    "# hand) -- add/adjust entries under `overrides:` to fix a wrong auto-detect\n"
    "# for a dimension, no code change needed. See sac_dim_config.py for the\n"
    "# full explanation.\n"
)


def _save(cfg: dict[str, Any]) -> bool:
    """Persist the config. NOTE: yaml.safe_dump can't preserve hand-written
    comments through a load->edit->save round-trip (PyYAML doesn't parse
    comments into the data at all) — every save rewrites the file with just
    _FILE_HEADER plus the data, so a save from the UI always leaves *some*
    explanation behind, even though any comment you added by hand elsewhere
    in the file won't survive the next UI-driven save."""
    try:
        import yaml
        os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
        with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(_FILE_HEADER)
            yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)
        _cache["cfg"] = cfg
        return True
    except Exception:
        return False


def _patterns(role: str) -> list[str]:
    cfg = _load()
    configured = (cfg.get("defaults") or {}).get(f"{role}_column_patterns")
    if isinstance(configured, list) and configured:
        return [str(p).strip().lower() for p in configured if str(p).strip()]
    return _BUILTIN_DEFAULTS.get(role, [])


def override_column(dim: str, role: str) -> str | None:
    """The exact, user-confirmed column name for this dimension+role, if one
    has been saved. None means 'no override yet — use the pattern defaults'."""
    overrides = _load().get("overrides") or {}
    dim_key = next((k for k in overrides if k.lower() == (dim or "").strip().lower()), None)
    if not dim_key:
        return None
    return (overrides[dim_key] or {}).get(f"{role}_column") or None


def set_override(dim: str, role: str, column: str | None) -> bool:
    """Write (column given) or clear (column=None) an exact column-name
    override for one dimension+role. This is what the Filters ⋮ menu's
    'Show raw columns' picker calls after the user confirms the real column —
    a one-time visual check becomes a permanent, reusable fix, no code change."""
    cfg = _load()
    overrides = cfg.setdefault("overrides", {})
    dim_key = next((k for k in overrides if k.lower() == (dim or "").strip().lower()), dim)
    entry = overrides.setdefault(dim_key, {})
    if column:
        entry[f"{role}_column"] = column
    else:
        entry.pop(f"{role}_column", None)
        if not entry:
            overrides.pop(dim_key, None)
    return _save(cfg)


def find_column(cols: list[str], dim: str, role: str, *, exclude: str | None = None) -> str | None:
    """Resolve the real column for `role` ('description' or 'parent') among
    `cols`: a saved per-dimension override first (if it's actually present in
    `cols`), else the configurable pattern list — exact name matches across
    all patterns before any substring match. `exclude` (normally the id
    column) is never returned, so a dimension with no real column for this
    role can't accidentally alias back onto its own id."""
    override = override_column(dim, role)
    if override and override in cols and override != exclude:
        return override
    low = {c: str(c).strip().lower() for c in cols}
    patterns = _patterns(role)
    for pat in patterns:
        exact = next((c for c in cols if low[c] == pat and c != exclude), None)
        if exact:
            return exact
    for pat in patterns:
        contains = next((c for c in cols if pat in low[c] and c != exclude), None)
        if contains:
            return contains
    return None
