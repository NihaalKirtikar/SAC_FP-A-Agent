"""
Playbook loader for the SAC FP&A Agent — structured FP&A/industry reference
data (GL/account taxonomy, KPI formula library, report archetypes, a short
narrative style guide), NOT a giant document fed wholesale to an LLM.

Design principle, matching the rest of this app ("rules before AI, never
silently assume a KPI's meaning"): this module only ever SUGGESTS — a
possible GL category for a measure name, a small relevant excerpt for the
AI prompt — and every suggestion still needs the user's own confirmation
(the Cost/Revenue toggle in the table builder) or is clearly labelled as
background context, never a source of numbers. See playbook/style_guide.md
for the full reasoning; this module is just the loader + lookup mechanics.

Everything here is read-only reference data loaded from playbook/*.yaml —
editing those files (or adding a new industry section) requires no code
change. Any load/parse failure degrades gracefully to "no suggestion
available" rather than breaking the app.
"""
from __future__ import annotations

import os
from typing import Any

_PLAYBOOK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "playbook")

INDUSTRIES = ["Generic (any industry)", "Utilities — Power Distribution (Discom)"]
_INDUSTRY_KEY = {
    "Generic (any industry)": None,
    "Utilities — Power Distribution (Discom)": "utilities_discom",
}


def _industry_key(industry_label: str | None) -> str | None:
    return _INDUSTRY_KEY.get(industry_label or "Generic (any industry)")


def _load_yaml(filename: str) -> dict[str, Any]:
    path = os.path.join(_PLAYBOOK_DIR, filename)
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}   # missing file / bad YAML / pyyaml unavailable -> no playbook, not a crash


# Simple process-lifetime cache — these files are small and static per run;
# Streamlit reruns the whole script constantly, so avoid re-parsing on every
# rerun. Cleared automatically by the importlib.reload() app.py already does
# on every run for its OTHER local modules... this one doesn't need that: it
# holds no executable logic the user is actively editing turn-to-turn like
# sac_insights.py/sac_report_export.py, just reference data.
_cache: dict[str, Any] = {}


def _taxonomy() -> dict[str, Any]:
    return _cache.setdefault("taxonomy", _load_yaml("gl_taxonomy.yaml"))


def _kpis() -> dict[str, Any]:
    return _cache.setdefault("kpis", _load_yaml("kpi_library.yaml"))


def _archetypes() -> dict[str, Any]:
    return _cache.setdefault("archetypes", _load_yaml("report_archetypes.yaml"))


def style_guide() -> str:
    if "style_guide" not in _cache:
        path = os.path.join(_PLAYBOOK_DIR, "style_guide.md")
        try:
            with open(path, "r", encoding="utf-8") as f:
                _cache["style_guide"] = f.read()
        except Exception:
            _cache["style_guide"] = ""
    return _cache["style_guide"]


def _taxonomy_sections(industry_label: str | None) -> list[dict[str, Any]]:
    """All GL-taxonomy entries across every sub-category, generic first then
    the selected industry's own section (if any)."""
    tax = _taxonomy()
    out: list[dict[str, Any]] = []
    for section in (tax.get("generic") or {}).values():
        out.extend(section or [])
    key = _industry_key(industry_label)
    if key and key in tax:
        for section in (tax.get(key) or {}).values():
            out.extend(section or [])
    return out


def _kpi_entries(industry_label: str | None) -> list[dict[str, Any]]:
    kpis = _kpis()
    out: list[dict[str, Any]] = []
    for section in (kpis.get("generic") or {}).values():
        out.extend(section or [])
    key = _industry_key(industry_label)
    if key and key in kpis:
        for section in (kpis.get(key) or {}).values():
            out.extend(section or [])
    return out


def suggest_category(name: str, *, industry: str | None = None) -> dict[str, Any] | None:
    """Best-effort GL-taxonomy match for a measure/column name. Returns the
    matching entry dict (id/label/favourability/note) or None — a SUGGESTION
    only; the table builder's own Cost/Revenue toggle is still what actually
    decides favourability, never this."""
    if not name:
        return None
    low = name.strip().lower()
    for entry in _taxonomy_sections(industry):
        for pat in entry.get("patterns") or []:
            if pat.lower() in low:
                return entry
    return None


def archetypes(industry: str | None = None) -> list[dict[str, Any]]:
    """Report-archetype checklist entries for this industry (generic + the
    selected industry's own, if any). Reference/checklist data only — nothing
    in this app auto-applies these yet."""
    arche = _archetypes()
    out = list(arche.get("generic") or [])
    key = _industry_key(industry)
    if key and key in arche:
        out += list(arche.get(key) or [])
    return out


def relevant_context(measure_names: list[str], *, industry: str | None = None,
                     max_entries: int = 6) -> str:
    """A SMALL, targeted excerpt of taxonomy + KPI definitions relevant to
    THESE specific measure names — the retrieval step that keeps the prompt
    grounded without ever sending the whole playbook. Returns "" if nothing
    matches (never a hard dependency for the AI call to proceed)."""
    matched_gl_ids: list[str] = []
    lines: list[str] = []
    for m in measure_names:
        entry = suggest_category(m, industry=industry)
        if entry and entry["id"] not in matched_gl_ids:
            matched_gl_ids.append(entry["id"])
            note = f" — {entry['note']}" if entry.get("note") else ""
            lines.append(f"- {entry['label']} ({entry['favourability']}){note}")
        if len(lines) >= max_entries:
            break
    kpi_lines: list[str] = []
    for kpi in _kpi_entries(industry):
        if any(g in matched_gl_ids for g in (kpi.get("gl_refs") or [])):
            kpi_lines.append(f"- {kpi['label']}: {kpi['formula']} — {kpi.get('note') or ''}".rstrip(" —"))
        if len(kpi_lines) >= max_entries:
            break
    if not lines and not kpi_lines:
        return ""
    parts = []
    if lines:
        parts.append("Relevant account/measure definitions:\n" + "\n".join(lines))
    if kpi_lines:
        parts.append("Relevant KPI formulas:\n" + "\n".join(kpi_lines))
    return "\n\n".join(parts)
