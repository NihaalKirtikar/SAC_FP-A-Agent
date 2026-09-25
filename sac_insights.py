"""
Insights engine for the SAC FP&A Agent — Management Reporting (table-builder edition).

ARCHITECTURE — "Rules before AI" (mirrors standard EPM/management-reporting practice):
  1. The user explicitly DECLARES every structural fact this engine needs — which
     column is a measure, its aggregation type, which column is a time dimension,
     whether a measure is cost-type or revenue-type, what "material" means. The
     engine never guesses these (it may pre-fill a *suggestion*, always overridable).
  2. Every number (pivots, variance, favourability, concentration, chart choice) is
     computed deterministically in pandas by fixed rules — never by the LLM.
  3. The LLM (Google Gemini) is used in exactly one place — `generate_observations()`
     — and only ever sees the small, already-computed "visible table" facts. It may
     summarize/point-out; it may never invent a number, apply a benchmark, guess
     causality, or recommend an action.

This keeps the report auditable: every figure traces back to a pandas computation
the user configured, not to a model's judgment.
"""
from __future__ import annotations

import json
import math
import os
import re
from typing import Any

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Vocabulary shared with the UI (app.py) — keep these in sync with cap_insights().
# ---------------------------------------------------------------------------
CHOOSE = "— choose —"                          # forces an explicit pick; never a silent default
AGG_TYPES = [CHOOSE, "SUM", "AVG", "COUNT", "COUNT DISTINCT", "MIN", "MAX", "LAST"]
REFERENCE_KINDS = ["None", "Budget", "Forecast", "Prior Year"]
PRESENTATIONS = ["Auto (let the rules decide)", "Composition (parts of a whole)",
                 "Driver / contribution analysis", "Variance waterfall"]
MEASURE_TYPES = ["Cost / expense", "Revenue / income"]  # kept short for segmented_control pills
CALC_OPS = ["+", "−", "×", "÷"]                # left-to-right, no operator precedence

_REFERENCE_KEYWORDS = {
    "Budget": ["budget", "plan", "bud"],
    "Forecast": ["forecast", "fcst", "estimate", "fc"],
    "Prior Year": ["prior", "py ", "py.", "lastyear", "last year", "ly", "previous"],
}
_TIME_NAME_HINTS = ("date", "month", "period", "year", "week", "quarter", "time")
_GEO_NAME_HINTS = ("country", "region", "state", "geo", "location", "city",
                   "market", "territory", "continent")
_YYYYMM_RE = re.compile(r"^(19|20)\d{2}(0[1-9]|1[0-2])$")


# ---------------------------------------------------------------------------
# Column heuristics — all produce SUGGESTIONS ONLY; the UI always lets the user
# override (Rule: never silently assume).
# ---------------------------------------------------------------------------
def numeric_columns(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]


def dimension_columns(df: pd.DataFrame, version_col: str | None) -> list[str]:
    """Non-numeric columns usable as a breakdown dimension (excludes version)."""
    return [c for c in df.columns
            if c != version_col and not pd.api.types.is_numeric_dtype(df[c])]


def guess_version_value(values: list[str], *keywords: str) -> str | None:
    low = [(v, str(v).lower()) for v in values]
    for kw in keywords:
        for original, l in low:
            if kw in l:
                return original
    return None


def guess_reference_value(values: list[str], reference_kind: str) -> str | None:
    return guess_version_value(values, *_REFERENCE_KEYWORDS.get(reference_kind, []))


def guess_time_dimension(df: pd.DataFrame, candidate_cols: list[str]) -> str | None:
    for c in candidate_cols:
        if pd.api.types.is_datetime64_any_dtype(df[c]):
            return c
    for c in candidate_cols:
        if any(h in str(c).lower() for h in _TIME_NAME_HINTS):
            return c
    for c in candidate_cols:
        sample = df[c].dropna().astype(str).head(20)
        if len(sample) and sample.map(lambda v: bool(_YYYYMM_RE.match(v))).mean() > 0.8:
            return c
    return None


def guess_geo_dimension(candidate_cols: list[str]) -> str | None:
    for c in candidate_cols:
        if any(h in str(c).lower() for h in _GEO_NAME_HINTS):
            return c
    return None


# ---------------------------------------------------------------------------
# Aggregation — one function, one rule per type (spec's Aggregation Rules table).
# ---------------------------------------------------------------------------
def _agg_series(s: pd.Series, kind: str) -> float:
    s = pd.to_numeric(s, errors="coerce")
    if kind == "SUM":
        return float(s.sum())
    if kind == "AVG":
        return float(s.dropna().mean()) if s.notna().any() else float("nan")
    if kind == "COUNT":
        return float(s.notna().sum())
    if kind == "COUNT DISTINCT":
        return float(s.dropna().nunique())
    if kind == "MIN":
        return float(s.min()) if s.notna().any() else float("nan")
    if kind == "MAX":
        return float(s.max()) if s.notna().any() else float("nan")
    raise ValueError(f"Unsupported aggregation: {kind}")


def _grouped_value(work: pd.DataFrame, group_cols: list[str], measure_col: str,
                   kind: str, time_dim: str | None) -> pd.Series:
    """One measure, aggregated per group_cols combo. LAST needs time_dim to sort by."""
    if kind == "LAST":
        if not time_dim:
            raise ValueError("LAST requires a time dimension to be set on this table.")
        ordered = work.sort_values(time_dim)
        if group_cols:
            return ordered.groupby(group_cols)[measure_col].last()
        return pd.Series({"Total": ordered[measure_col].iloc[-1] if len(ordered) else float("nan")})
    if group_cols:
        return work.groupby(group_cols).apply(
            lambda g: _agg_series(g[measure_col], kind), include_groups=False)
    return pd.Series({"Total": _agg_series(work[measure_col], kind)})


# ---------------------------------------------------------------------------
# Measure spec: a plain aggregated column, or a "calc" — 2+ columns (each
# always SUMmed first, same convention the old Ratio(%) feature used) combined
# LEFT-TO-RIGHT with +/-/x/÷ operators (no algebraic precedence — a simple
# calculator chain, e.g. terms=[Revenue, COGS], ops=["−"] => Revenue − COGS).
# A plain 2-term ÷ is exactly the old Ratio(%) shape, generalized rather than
# replaced.
# ---------------------------------------------------------------------------
def measure_label(spec: dict[str, Any]) -> str:
    if spec.get("kind") == "calc":
        return spec["label"]
    return f"{spec['col']} ({spec['agg']})"


def _apply_calc_op(op: str, a: pd.Series, b: pd.Series) -> pd.Series:
    if op == "+":
        return a + b
    if op == "−":
        return a - b
    if op == "×":
        return a * b
    if op == "÷":
        return a / b.replace(0, np.nan)
    raise ValueError(f"Unknown calc operator: {op!r}")


def _measure_value(work: pd.DataFrame, group_cols: list[str], spec: dict[str, Any],
                   time_dim: str | None) -> pd.Series:
    if spec.get("kind") == "calc":
        terms = spec["terms"]
        result = _grouped_value(work, group_cols, terms[0], "SUM", time_dim)
        for op, term in zip(spec["ops"], terms[1:]):
            val = _grouped_value(work, group_cols, term, "SUM", time_dim)
            result = _apply_calc_op(op, result, val)
        if spec.get("as_pct", False):
            result = result * 100.0
        return result
    return _grouped_value(work, group_cols, spec["col"], spec["agg"], time_dim)


# ---------------------------------------------------------------------------
# Favourability — declared, never inferred (Rule 2/3: KPI- and industry-agnostic).
# ---------------------------------------------------------------------------
def _favourability(variance: float, revenue_semantics: bool) -> str:
    if variance is None or (isinstance(variance, float) and math.isnan(variance)):
        return "N/A"
    if variance == 0:
        return "On plan"
    positive = variance > 0
    good = positive if revenue_semantics else (not positive)
    return "Favourable" if good else "Unfavourable"


# ---------------------------------------------------------------------------
# THE TABLE — build one table from a TableSpec (see cap_insights() for the shape).
# Returns a dict with an "error" key on failure (never raises to the caller), or
# a full result: {"meta": {...}, "display_df": DataFrame, "chart_kind": str}.
# ---------------------------------------------------------------------------
def build_table(df: pd.DataFrame, spec: dict[str, Any], *, version_col: str,
                actual_version: str) -> dict[str, Any]:
    work = df.copy()
    for col, selected in (spec.get("filters") or {}).items():
        if selected and col in work.columns:
            work = work[work[col].astype(str).isin([str(v) for v in selected])]

    # row_dims/col_dims are LISTS — a "designer panel" Rows/Columns well can hold
    # more than one field, exactly like a spreadsheet PivotTable. Internally we
    # still boil each side down to one composite "row"/"col" key (so every
    # downstream consumer — variance merge, chart selection, chart rendering —
    # keeps reasoning about a single row axis and a single optional column axis),
    # but when a side has 2+ dimensions we ALSO keep their individual columns in
    # the display frame so the breakdown stays readable, not just a joined string.
    row_dims = [d for d in (spec.get("row_dims") or []) if d]
    col_dims = [d for d in (spec.get("col_dims") or []) if d]
    time_dim = spec.get("time_dim") or None
    measures: list[dict[str, Any]] = spec.get("measures") or []
    if not measures:
        return {"error": "Add at least one measure."}

    group_cols = row_dims + col_dims
    try:
        actual_slice = work[work[version_col].astype(str) == str(actual_version)]
        actual_vals = {measure_label(m): _measure_value(actual_slice, group_cols, m, time_dim)
                       for m in measures}
    except ValueError as e:
        return {"error": str(e)}

    ref_kind = spec.get("reference_kind") or "None"
    ref_version = spec.get("reference_version")
    has_reference = ref_kind != "None" and bool(ref_version) and len(measures) == 1
    ref_vals = None
    if has_reference:
        ref_slice = work[work[version_col].astype(str) == str(ref_version)]
        ref_vals = _measure_value(ref_slice, group_cols, measures[0], time_dim)

    mlabels = [measure_label(m) for m in measures]
    is_time = bool(time_dim) and time_dim in group_cols

    # ---- Assemble the display dataframe (long form: one row per group combo) ----
    # _name_axis is applied identically to the actual side AND (further below) the
    # reference side, so both frames always agree on their index/column names —
    # including the group_cols==[] single-total case, where pandas otherwise leaves
    # the index unnamed and a later merge would KeyError on a missing "row" column.
    def _name_axis(s: pd.Series) -> pd.Series:
        if not group_cols:
            return s.rename_axis("row")            # single total, already "row"
        if len(group_cols) == 1:
            return s.rename_axis(group_cols[0])    # keep the real dimension name
        return s.rename_axis(group_cols)            # MultiIndex, real dimension names

    def _compose_row_col(frame_: pd.DataFrame, *, drop_raw: bool) -> pd.DataFrame:
        """Add composite 'row'/'col' identity columns from row_dims/col_dims. When
        a side has 2+ dims the composite is a ' · '-joined label; drop_raw controls
        whether the individual per-dimension columns are kept alongside it (kept
        on the actual/display side for readability, always dropped on the
        reference side to avoid duplicate-column merge conflicts)."""
        if row_dims:
            frame_["row"] = (frame_[row_dims[0]].astype(str) if len(row_dims) == 1
                             else frame_[row_dims].astype(str).agg(" · ".join, axis=1))
            if drop_raw or len(row_dims) == 1:
                frame_ = frame_.drop(columns=[c for c in row_dims if c in frame_.columns])
        if col_dims:
            frame_["col"] = (frame_[col_dims[0]].astype(str) if len(col_dims) == 1
                             else frame_[col_dims].astype(str).agg(" · ".join, axis=1))
            if drop_raw or len(col_dims) == 1:
                frame_ = frame_.drop(columns=[c for c in col_dims if c in frame_.columns])
        return frame_

    frame = pd.concat({lbl: _name_axis(actual_vals[lbl]) for lbl in mlabels}, axis=1)
    frame = frame.reset_index().fillna(0.0)
    if group_cols:
        frame = _compose_row_col(frame, drop_raw=False)

    n_row_members = frame["row"].nunique() if "row" in frame.columns else 1
    has_col_dim = bool(col_dims)
    is_geo = (len(row_dims) == 1 and not has_col_dim
             and guess_geo_dimension(row_dims) == row_dims[0])

    result_meta: dict[str, Any] = {
        "title": spec.get("title") or "Table",
        "row_dims": row_dims, "col_dims": col_dims, "time_dim": time_dim,
        "row_dim_label": ", ".join(row_dims) if row_dims else "(none — single total)",
        "col_dim_label": ", ".join(col_dims) if col_dims else None,
        "measures": mlabels, "mode": "single" if len(measures) == 1 else "multi",
        "has_reference": has_reference, "reference_kind": ref_kind,
        "reference_version": ref_version, "actual_version": actual_version,
        "is_time": is_time, "is_geo": is_geo, "n_row_members": n_row_members,
        "has_col_dim": has_col_dim, "has_row_dim": bool(row_dims),
        "presentation": spec.get("presentation", PRESENTATIONS[0]),
    }

    # ---- Single-measure mode: fold in the reference/variance columns ----
    if len(measures) == 1:
        lbl = mlabels[0]
        frame = frame.rename(columns={lbl: "actual"})
        measure_type = spec.get("measure_type", "cost")
        result_meta["measure_type"] = measure_type
        result_meta["favourable_when"] = ("actual > reference" if measure_type == "revenue"
                                          else "actual < reference")
        if has_reference:
            ref_frame = _name_axis(ref_vals).reset_index()
            ref_frame = ref_frame.rename(columns={ref_frame.columns[-1]: "reference"})
            if group_cols:
                ref_frame = _compose_row_col(ref_frame, drop_raw=True)
            merge_on = [c for c in ("row", "col") if c in frame.columns]
            frame = frame.merge(ref_frame, on=merge_on, how="outer").fillna(0.0)
            frame["variance"] = frame["actual"] - frame["reference"]
            frame["variance_pct"] = np.where(frame["reference"] != 0,
                                             frame["variance"] / frame["reference"].abs() * 100.0,
                                             np.nan)
            revenue = measure_type == "revenue"
            frame["favourability"] = frame["variance"].apply(lambda v: _favourability(v, revenue))
            total_abs = float(frame["variance"].abs().sum()) or 1.0
            frame["share_of_variance_pct"] = frame["variance"].abs() / total_abs * 100.0
            n_row_members = frame["row"].nunique() if "row" in frame.columns else 1
            result_meta["n_row_members"] = n_row_members

        chart_kind = _choose_chart_single(
            has_row_dim=result_meta["has_row_dim"], n_members=n_row_members,
            has_col_dim=has_col_dim, is_time=is_time, is_geo=is_geo,
            presentation=result_meta["presentation"], has_reference=has_reference)
    else:
        chart_kind = _choose_chart_multi(n_measures=len(measures), is_time=is_time)

    result_meta["chart_kind"] = chart_kind
    sort_col = "variance" if ("variance" in frame.columns) else ("actual" if "actual" in frame.columns else None)
    if sort_col and "row" in frame.columns and not has_col_dim:
        frame = frame.sort_values(sort_col, key=lambda s: s.abs() if sort_col == "variance" else s,
                                   ascending=False).reset_index(drop=True)
    return {"meta": result_meta, "display_df": frame}


# ---------------------------------------------------------------------------
# CHART SELECTION RULES — explicit user-declared intent (waterfall/driver/
# composition) takes precedence over shape-inferred rules; ties resolved by the
# literal order of the spec's rule table. See code comments for the mapping.
# ---------------------------------------------------------------------------
def _choose_chart_single(*, has_row_dim: bool, n_members: int, has_col_dim: bool,
                         is_time: bool, is_geo: bool, presentation: str,
                         has_reference: bool) -> str:
    if presentation.startswith("Variance waterfall") and has_reference:
        return "waterfall"
    if presentation.startswith("Driver") and has_reference:
        return "driver_bar"
    if presentation.startswith("Composition") and has_row_dim and not has_col_dim:
        return "donut" if n_members <= 6 else "bar_composition"
    if not has_row_dim:
        return "tile"                                  # single KPI, no breakdown
    if is_time and not has_col_dim:
        return "line"
    if is_geo:
        return "geo_fallback_bar"                       # no real geo-map renderer
    if has_col_dim:
        return "heatmap"                                # 2 dims + 1 measure
    return "hbar" if n_members <= 7 else "vbar"


def _choose_chart_multi(*, n_measures: int, is_time: bool) -> str:
    if is_time:
        return "multi_line" if n_measures > 1 else "line"
    if n_measures == 2:
        return "scatter"
    return "grouped_bar"


# ---------------------------------------------------------------------------
# Facts payload for the "Get Insights" AI feature and for export lineage —
# always exactly the CURRENT VISIBLE table, nothing else.
# ---------------------------------------------------------------------------
def _clean(x: Any) -> float | None:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) else round(x, 2)


def build_view_facts(result: dict[str, Any], *, max_rows: int = 40) -> dict[str, Any]:
    meta, df = result["meta"], result["display_df"]
    rows = df.head(max_rows).copy()
    for c in rows.columns:
        if pd.api.types.is_numeric_dtype(rows[c]):
            rows[c] = rows[c].map(_clean)
    return {
        "table_title": meta["title"], "row_dimension": meta["row_dim_label"],
        "column_dimension": meta["col_dim_label"], "measures": meta["measures"], "mode": meta["mode"],
        "has_comparison": meta["has_reference"],
        "reference_kind": meta.get("reference_kind"),
        "favourable_when": meta.get("favourable_when"),
        "rows_shown": len(rows), "rows_total": len(df),
        "visible_table": rows.to_dict("records"),
    }


# ---------------------------------------------------------------------------
# GET INSIGHTS — the only place AI is used. Strictly observational.
# ---------------------------------------------------------------------------
_DISCLAIMER = "AI-generated observations — verify against source data."
_OBS_MAX_WORDS = 450  # was 200 — raised so Gemini has room for real depth per bullet

_OBS_SYSTEM = (
    "You are an SAP Analytics Cloud management-reporting assistant. This is the ONLY "
    "place in the app you use AI judgment — everywhere else, fixed rules decide. You "
    "are given the CURRENT VISIBLE STATE of one table the user built: its rows, "
    "columns, measures, aggregation types, and — if a comparison is configured — the "
    "variance figures and which direction is favourable. You may also be given a "
    "PLAYBOOK EXCERPT: reference FP&A/industry definitions relevant to this table's "
    "measures — use its terminology and formulas where they genuinely apply, but it "
    "is background knowledge, not a source of numbers; every figure you state must "
    "still come from the table data. Rules:\n"
    "1. Cover EVERY row/member that is material (not just the top 3-5) — for a small "
    "table (<=10 rows) discuss all of them; for a larger one, cover the material ones "
    "individually and group the immaterial remainder into one summary line.\n"
    "2. Call out notable variances or anomalies visible in the data given, and any "
    "concentration pattern (e.g. how few members explain most of the movement).\n"
    "3. Suggest exactly 2-3 follow-up questions the user might investigate next.\n"
    "4. Do NOT invent, estimate, or infer any number not present in the data given.\n"
    "5. Do NOT use causal language ('because', 'due to', 'caused by', 'driven by') "
    "unless the data given explicitly states a cause — describe patterns, don't "
    "explain them.\n"
    "6. Do NOT apply industry benchmarks/norms, and do not call a number 'good' or "
    "'bad' beyond the favourable/unfavourable label already computed for you.\n"
    "7. Do NOT recommend actions. Observations only.\n"
    f"8. Output plain bullet points, no headings, maximum {_OBS_MAX_WORDS} words total — "
    "use the room: every bullet should name the member, its value(s), and the "
    "variance/share number, not just a vague trend statement.\n"
    "9. Do not add your own disclaimer line — one is appended automatically."
)


def llm_status() -> tuple[bool, str]:
    if not (os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")):
        return False, "No GOOGLE_API_KEY set — using a built-in template summary."
    try:
        import google.genai  # noqa: F401
    except Exception:
        return False, "google-genai not installed — using a built-in template summary."
    return True, "Gemini (%s)" % (os.getenv("SAC_LLM_MODEL") or "gemini-2.5-flash")


def _cap_words(text: str, max_words: int = _OBS_MAX_WORDS) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]).rstrip(",.;: ") + "…"


def generate_observations(facts: dict[str, Any], *, playbook_context: str = "") -> tuple[str, str]:
    """Return (markdown_bullets_with_disclaimer, source). source is 'gemini'|'fallback'.
    playbook_context (optional): a SMALL, pre-filtered excerpt of relevant playbook
    definitions/terminology for this table's own measures — grounding, not a source
    of numbers. Callers get this from sac_playbook.relevant_context(...)."""
    ok, _ = llm_status()
    if not ok:
        return _fallback_observations(facts) + f"\n\n_{_DISCLAIMER}_", "fallback"
    try:
        from google import genai
        from google.genai import types

        api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
        model = os.getenv("SAC_LLM_MODEL") or "gemini-2.5-flash"
        client = genai.Client(api_key=api_key)
        # ~450 words is ~650-750 tokens of actual answer; give real headroom above that.
        cfg_kwargs = dict(system_instruction=_OBS_SYSTEM, temperature=0.2,
                          max_output_tokens=1600)
        try:
            cfg_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
        except Exception:
            pass
        contents = "Current visible table state:\n\n" + json.dumps(facts, indent=2)
        if playbook_context:
            contents += "\n\nPLAYBOOK EXCERPT (background knowledge, not a number source):\n\n" + playbook_context
        resp = client.models.generate_content(
            model=model, contents=contents,
            config=types.GenerateContentConfig(**cfg_kwargs),
        )
        text = (resp.text or "").strip()
        if not text:
            return _fallback_observations(facts) + f"\n\n_{_DISCLAIMER}_", "fallback"
        # Belt-and-suspenders: strip any disclaimer-ish line the model added, cap
        # words, then append OUR canonical disclaimer — never trust the model's copy.
        kept = [ln for ln in text.splitlines() if "verify against source data" not in ln.lower()]
        body = _cap_words("\n".join(kept).strip())
        return body + f"\n\n_{_DISCLAIMER}_", "gemini"
    except Exception as e:
        note = f"\n\n> _AI observations unavailable ({e}); showing a template summary._"
        return _fallback_observations(facts) + f"\n\n_{_DISCLAIMER}_" + note, "fallback"


def _fallback_observations(facts: dict[str, Any]) -> str:
    rows = facts.get("visible_table") or []
    lines: list[str] = []
    if not rows:
        return "- No data is currently visible in this table."
    key_val = "variance" if facts.get("has_comparison") else ("actual" if rows and "actual" in rows[0] else None)
    if key_val is None:
        for c in rows[0]:
            if isinstance(rows[0][c], (int, float)):
                key_val = c; break
    if key_val:
        by_val = sorted(rows, key=lambda r: abs(r.get(key_val) or 0), reverse=True)
        top, bottom = by_val[0], by_val[-1]
        rlabel = lambda r: r.get("row", r.get("col", "Total"))
        lines.append(f"- Largest {key_val.replace('_', ' ')}: **{rlabel(top)}** at "
                     f"{top.get(key_val):,.0f}.")
        if len(by_val) > 1:
            lines.append(f"- Smallest: **{rlabel(bottom)}** at {bottom.get(key_val):,.0f}.")
        if facts.get("has_comparison"):
            unfav = [r for r in rows if r.get("favourability") == "Unfavourable"]
            fav = [r for r in rows if r.get("favourability") == "Favourable"]
            lines.append(f"- {len(unfav)} of {len(rows)} row(s) are Unfavourable, "
                         f"{len(fav)} Favourable ({facts.get('favourable_when')}).")
    lines.append(f"- {facts.get('rows_shown')} of {facts.get('rows_total')} row(s) shown "
                f"for **{facts.get('table_title')}**.")
    lines += [
        "",
        "Follow-up questions to consider:",
        "- What is driving the largest gap shown above?",
        "- How does this look over a longer time window?",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Demo data — lets the whole pipeline run without a live SAC connection.
# Includes a time dimension, a geo-ish dimension, two measures (for ratio /
# correlation), and four version types (Actual/Budget/Forecast/Prior Year).
# ---------------------------------------------------------------------------
def sample_dataframe() -> pd.DataFrame:
    rng = np.random.default_rng(7)
    centres = ["Sales", "Marketing", "R&D", "Operations",
               "Finance", "HR", "IT", "Supply Chain"]
    region_of = {"Sales": "North America", "Marketing": "Europe", "R&D": "Asia Pacific",
                "Operations": "Europe", "Finance": "North America", "HR": "North America",
                "IT": "Asia Pacific", "Supply Chain": "Europe"}
    months = ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]
    versions = {"public.Actual": 1.0, "public.Budget": 1.0,
               "public.Forecast": 1.03, "public.PriorYear": 0.9}
    rows = []
    for cc in centres:
        base = float(rng.integers(50, 500)) * 1000.0
        head = int(rng.integers(8, 60))
        for mi, m in enumerate(months):
            drift = 1.0 + 0.01 * mi
            for ver, factor in versions.items():
                noise = 1.0 + float(rng.normal(0.0, 0.08 if ver == "public.Actual" else 0.0))
                amount = base * factor * drift * noise
                rows.append({"Cost Center": cc, "Region": region_of[cc], "Month": m,
                            "Version": ver, "Account": "Total Opex",
                            "Amount": round(amount), "Headcount": head})
    return pd.DataFrame(rows)
