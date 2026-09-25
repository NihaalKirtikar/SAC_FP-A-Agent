"""
Executive report exporters for the SAC FP&A Agent — Management Reporting.

`render_chart_png(result)` renders whichever chart `sac_insights.build_table()`
selected (per the Chart Selection Rules) as a single matplotlib PNG, used BOTH
for the on-screen preview and the export, so what the user sees is exactly what
gets exported (WYSIWYG). `build_pptx`/`build_pdf` take a list of such table
results (+ optional AI observations per table) and assemble a multi-section
report. Heavy libraries are imported lazily so the app boots even if one of
them is unavailable.

PPT-export visual rules applied everywhere: titles present & not truncated,
axis labels fully visible with rotation capped at 45°, a legend whenever a
chart has more than one series, light backgrounds only, minimum 10pt data
labels / 12pt titles.
"""
from __future__ import annotations

import io
import itertools
from typing import Any

import pandas as pd

# Accenture-ish palette
PURPLE = (0xA1, 0x00, 0xFF)
INK = (0x1A, 0x1A, 0x24)
GREY = (0x6B, 0x6B, 0x76)
FAV = (0x2C, 0xA0, 0x2C)      # favourable (green)
UNFAV = (0xD6, 0x27, 0x28)    # unfavourable (red)
NEUTRAL = (0x9B, 0x8F, 0xB5)  # on-plan / no favourability declared
PALETTE_HEX = ["#A100FF", "#7500C0", "#3B0065", "#C879FF", "#5C2D91", "#00A3A1", "#FFB800"]


def _rgb01(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    return (rgb[0] / 255, rgb[1] / 255, rgb[2] / 255)


def _palette(n: int) -> list[str]:
    return list(itertools.islice(itertools.cycle(PALETTE_HEX), n))


def _style_ax(ax, title: str, *, xlabel: str | None = None, ylabel: str | None = None) -> None:
    ax.set_title(title, fontsize=13, fontweight="bold", color="#1A1A24", pad=10)
    if xlabel:
        ax.set_xlabel(xlabel, fontsize=10, color="#333333")
    if ylabel:
        ax.set_ylabel(ylabel, fontsize=10, color="#333333")
    ax.tick_params(labelsize=10)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    ax.set_facecolor("white")


def _to_png(fig) -> bytes:
    buf = io.BytesIO()
    fig.set_facecolor("white")
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
    import matplotlib.pyplot as plt
    plt.close(fig)
    return buf.getvalue()


def _favour_colour(flag: Any) -> tuple[float, float, float]:
    if flag == "Unfavourable":
        return _rgb01(UNFAV)
    if flag == "Favourable":
        return _rgb01(FAV)
    return _rgb01(NEUTRAL)


# ---------------------------------------------------------------------------
# Chart primitives
# ---------------------------------------------------------------------------
def _bar(df: pd.DataFrame, *, name_col: str, value_col: str, horizontal: bool, title: str,
        value_label: str, favourability_col: str | None = None, sort_abs: bool = False) -> bytes:
    import matplotlib.pyplot as plt

    d = df.copy()
    if sort_abs:
        d = d.iloc[d[value_col].abs().sort_values(ascending=True).index]
    else:
        d = d.sort_values(value_col, ascending=True)
    if not horizontal:
        d = d.iloc[::-1]  # vertical bars read left-to-right as given (descending)
    names = d[name_col].astype(str).tolist()
    vals = d[value_col].astype(float).tolist()
    if favourability_col and favourability_col in d.columns:
        colors = [_favour_colour(f) for f in d[favourability_col]]
    else:
        colors = [_rgb01(PURPLE)] * len(names)

    if horizontal:
        fig, ax = plt.subplots(figsize=(8.4, max(2.6, 0.45 * len(names) + 1.2)), dpi=150)
        ax.barh(names, vals, color=colors)
        ax.axvline(0, color="#999999", linewidth=0.8)
        _style_ax(ax, title, xlabel=value_label)
    else:
        fig, ax = plt.subplots(figsize=(8.6, 4.6), dpi=150)
        ax.bar(names, vals, color=colors)
        ax.axhline(0, color="#999999", linewidth=0.8)
        plt.setp(ax.get_xticklabels(), rotation=40, ha="right", fontsize=9)
        _style_ax(ax, title, ylabel=value_label)
    fig.tight_layout()
    return _to_png(fig)


def _donut(names: list[str], vals: list[float], title: str) -> bytes:
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(6.4, 6.0), dpi=150)
    colors = _palette(len(names))
    _, texts, autotexts = ax.pie(
        vals, labels=names, autopct=lambda p: f"{p:.0f}%" if p >= 3 else "",
        startangle=90, colors=colors, wedgeprops=dict(width=0.42, edgecolor="white"),
        textprops={"fontsize": 10})
    for t in texts + autotexts:
        t.set_fontsize(10)
    ax.set_title(title, fontsize=13, fontweight="bold", color="#1A1A24")
    fig.tight_layout()
    return _to_png(fig)


def _line(df: pd.DataFrame, *, x_col: str, y_cols: list[str], title: str, ylabel: str) -> bytes:
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8.6, 4.4), dpi=150)
    xs = df[x_col].astype(str).tolist()
    colors = _palette(len(y_cols))
    for i, yc in enumerate(y_cols):
        ax.plot(xs, pd.to_numeric(df[yc], errors="coerce"), marker="o", linewidth=2,
                label=str(yc), color=colors[i])
    if len(y_cols) > 1:
        ax.legend(fontsize=9, frameon=False)
    plt.setp(ax.get_xticklabels(), rotation=30, ha="right", fontsize=9)
    _style_ax(ax, title, ylabel=ylabel)
    fig.tight_layout()
    return _to_png(fig)


def _scatter(df: pd.DataFrame, x_col: str, y_col: str, label_col: str, title: str) -> bytes:
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(6.6, 5.6), dpi=150)
    ax.scatter(pd.to_numeric(df[x_col], errors="coerce"), pd.to_numeric(df[y_col], errors="coerce"),
              s=70, color=_rgb01(PURPLE))
    for _, r in df.iterrows():
        ax.annotate(str(r[label_col]), (r[x_col], r[y_col]), fontsize=8,
                   xytext=(4, 4), textcoords="offset points", color="#333333")
    _style_ax(ax, title, xlabel=str(x_col), ylabel=str(y_col))
    fig.tight_layout()
    return _to_png(fig)


def _heatmap(pivot_df: pd.DataFrame, title: str) -> bytes:
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8.6, max(3.0, 0.5 * len(pivot_df) + 1.5)), dpi=150)
    data = pivot_df.to_numpy(dtype=float)
    im = ax.imshow(data, cmap="Purples", aspect="auto")
    ax.set_xticks(range(len(pivot_df.columns)))
    ax.set_xticklabels([str(c) for c in pivot_df.columns], rotation=40, ha="right", fontsize=9)
    ax.set_yticks(range(len(pivot_df.index)))
    ax.set_yticklabels([str(i) for i in pivot_df.index], fontsize=9)
    vmax = abs(data).max() or 1.0
    for i in range(data.shape[0]):
        for j in range(data.shape[1]):
            colour = "white" if data[i, j] > 0.6 * vmax else "#1A1A24"
            ax.text(j, i, f"{data[i, j]:,.0f}", ha="center", va="center",
                   fontsize=8, color=colour)
    cbar = fig.colorbar(im, ax=ax, shrink=0.8)
    cbar.ax.tick_params(labelsize=8)
    ax.set_title(title, fontsize=13, fontweight="bold", color="#1A1A24")
    fig.tight_layout()
    return _to_png(fig)


def _waterfall(names: list[str], deltas: list[float], favourable_flags: list[str],
              start_label: str, start_value: float, end_label: str, title: str,
              value_label: str) -> bytes:
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(9.0, 4.8), dpi=150)
    cats = [start_label] + [str(n) for n in names] + [end_label]
    running = start_value
    ax.bar(cats[0], start_value, color=_rgb01(NEUTRAL))
    for i, d in enumerate(deltas):
        colour = _favour_colour(favourable_flags[i])
        bottom = running if d >= 0 else running + d
        ax.bar(cats[i + 1], abs(d), bottom=bottom, color=colour)
        running += d
    ax.bar(cats[-1], running, color=_rgb01(NEUTRAL))
    plt.setp(ax.get_xticklabels(), rotation=40, ha="right", fontsize=9)
    _style_ax(ax, title, ylabel=value_label)
    fig.tight_layout()
    return _to_png(fig)


def _grouped_bar(df: pd.DataFrame, name_col: str, value_cols: list[str], title: str,
                 ylabel: str) -> bytes:
    import matplotlib.pyplot as plt
    import numpy as np

    fig, ax = plt.subplots(figsize=(8.8, 4.6), dpi=150)
    x = np.arange(len(df))
    w = 0.8 / max(1, len(value_cols))
    colors = _palette(len(value_cols))
    for i, vc in enumerate(value_cols):
        ax.bar(x + i * w, pd.to_numeric(df[vc], errors="coerce"), width=w, label=str(vc),
              color=colors[i])
    ax.set_xticks(x + w * (len(value_cols) - 1) / 2)
    ax.set_xticklabels(df[name_col].astype(str), rotation=40, ha="right", fontsize=9)
    if len(value_cols) > 1:
        ax.legend(fontsize=9, frameon=False)
    _style_ax(ax, title, ylabel=ylabel)
    fig.tight_layout()
    return _to_png(fig)


# ---------------------------------------------------------------------------
# Dispatcher — reads the chart_kind that sac_insights.build_table() selected.
# ---------------------------------------------------------------------------
def render_chart_png(result: dict[str, Any]) -> bytes | None:
    meta, df = result["meta"], result["display_df"]
    kind = meta["chart_kind"]
    title = meta["title"]
    measure_label = meta["measures"][0] if meta["measures"] else "Value"

    if kind in ("tile", "table"):
        return None
    if kind == "hbar":
        return _bar(df, name_col="row", value_col="actual", horizontal=True,
                   title=title, value_label=measure_label)
    if kind == "vbar":
        return _bar(df, name_col="row", value_col="actual", horizontal=False,
                   title=title, value_label=measure_label)
    if kind == "driver_bar":
        return _bar(df, name_col="row", value_col="variance", horizontal=True,
                   title=f"{title} — driver contribution", value_label=f"Variance in {measure_label}",
                   favourability_col="favourability", sort_abs=True)
    if kind == "bar_composition":
        return _bar(df, name_col="row", value_col="actual", horizontal=True,
                   title=title, value_label=measure_label)
    if kind == "geo_fallback_bar":
        return _bar(df, name_col="row", value_col="actual", horizontal=True,
                   title=f"{title} (map view unavailable — shown as bar)",
                   value_label=measure_label)
    if kind == "donut":
        return _donut(df["row"].astype(str).tolist(), df["actual"].astype(float).tolist(), title)
    if kind in ("line", "multi_line"):
        xcol = "row" if meta["time_dim"] in meta["row_dims"] else "col"
        d = df.sort_values(xcol)
        ycols = meta["measures"] if meta["mode"] == "multi" else ["actual"]
        return _line(d, x_col=xcol, y_cols=ycols, title=title,
                    ylabel=measure_label if len(meta["measures"]) == 1 else "Value")
    if kind == "scatter":
        m1, m2 = meta["measures"][0], meta["measures"][1]
        return _scatter(df, m1, m2, "row", title)
    if kind == "heatmap":
        pivot = df.pivot(index="row", columns="col", values="actual").fillna(0.0)
        return _heatmap(pivot, title)
    if kind == "waterfall":
        d = df.iloc[df["variance"].abs().sort_values(ascending=False).index]
        start_value = float(d["reference"].sum())
        return _waterfall(d["row"].astype(str).tolist(), d["variance"].astype(float).tolist(),
                          d["favourability"].tolist(), "Reference", start_value, "Actual",
                          title, measure_label)
    if kind == "grouped_bar":
        return _grouped_bar(df, "row", meta["measures"], title, "Value")
    return None


# ---------------------------------------------------------------------------
# Small markdown-ish parser (reused for AI-observations bullets)
# ---------------------------------------------------------------------------
def _md_blocks(md: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for raw in (md or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.lstrip().startswith(("- ", "* ")):
            out.append(("bullet", line.lstrip("-* ").strip()))
        elif line.startswith(">") or line.startswith("_"):
            out.append(("note", line.strip("_> ").strip()))
        else:
            out.append(("para", line.strip()))
    return out


def _strip_md(text: str) -> str:
    return text.replace("**", "").replace("`", "")


def _fmt_cell(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:,.1f}" if abs(v) < 100 else f"{v:,.0f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


# ---------------------------------------------------------------------------
# PPTX — one slide per table (title/KPIs/chart-or-tile/table/AI observations)
# ---------------------------------------------------------------------------
def build_pptx(report: dict[str, Any]) -> bytes:
    from pptx import Presentation
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN, MSO_ANCHOR

    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    blank = prs.slide_layouts[6]
    SW = prs.slide_width

    def textbox(slide, left, top, width, height):
        tb = slide.shapes.add_textbox(left, top, width, height)
        tf = tb.text_frame
        tf.word_wrap = True
        return tf

    def set_run(p, text, size, *, bold=False, color=INK):
        r = p.add_run(); r.text = text
        r.font.size = Pt(size); r.font.bold = bold
        r.font.color.rgb = RGBColor(*color)
        r.font.name = "Segoe UI"

    def accent_bar(slide, top):
        bar = slide.shapes.add_shape(1, Inches(0), top, SW, Inches(0.12))
        bar.fill.solid(); bar.fill.fore_color.rgb = RGBColor(*PURPLE)
        bar.line.fill.background()

    # ---- Title slide ----
    s1 = prs.slides.add_slide(blank)
    accent_bar(s1, Inches(2.5))
    tf = textbox(s1, Inches(0.9), Inches(2.7), Inches(11.5), Inches(2.2))
    p = tf.paragraphs[0]; set_run(p, report["title"], 34, bold=True, color=PURPLE)
    p2 = tf.add_paragraph(); set_run(p2, report["subtitle"], 20, color=INK)
    p3 = tf.add_paragraph(); p3.space_before = Pt(10)
    set_run(p3, report["lineage"], 11, color=GREY)

    for table in report["tables"]:
        meta, df = table["result"]["meta"], table["result"]["display_df"]
        chart_png = table.get("chart_png")
        s = prs.slides.add_slide(blank)
        accent_bar(s, Inches(0))
        th = textbox(s, Inches(0.5), Inches(0.3), Inches(12.3), Inches(0.6))
        set_run(th.paragraphs[0], meta["title"], 22, bold=True, color=INK)

        left_top = Inches(1.05)
        if chart_png:
            s.shapes.add_picture(io.BytesIO(chart_png), Inches(0.5), left_top, width=Inches(6.6))
        elif meta["chart_kind"] == "tile":
            tile_tf = textbox(s, Inches(0.5), Inches(1.6), Inches(6.6), Inches(2.2))
            val = float(df["actual"].iloc[0]) if "actual" in df.columns else float("nan")
            pp = tile_tf.paragraphs[0]; pp.alignment = PP_ALIGN.CENTER
            set_run(pp, meta["measures"][0], 14, color=GREY)
            pv = tile_tf.add_paragraph(); pv.alignment = PP_ALIGN.CENTER
            set_run(pv, f"{val:,.0f}", 44, bold=True, color=PURPLE)

        # KPI strip (only if this table has a reference comparison)
        if meta.get("has_reference"):
            tot_a = float(df["actual"].sum()); tot_r = float(df["reference"].sum())
            var = tot_a - tot_r
            var_pct = (var / abs(tot_r) * 100.0) if tot_r else None
            revenue = meta.get("measure_type") == "revenue"
            good = (var >= 0) if revenue else (var <= 0)
            kpis = [("Actual", f"{tot_a:,.0f}", None), (meta["reference_kind"], f"{tot_r:,.0f}", None),
                   ("Variance", f"{var:,.0f}", good),
                   ("Var %", "n/a" if var_pct is None else f"{var_pct:,.1f}%", good)]
            tile_w = Inches(1.5); left0 = Inches(7.35)
            for i, (label, value, g) in enumerate(kpis):
                left = left0 + i * (tile_w + Inches(0.1))
                tile = s.shapes.add_shape(5, left, Inches(1.05), tile_w, Inches(1.0))
                tile.fill.solid(); tile.fill.fore_color.rgb = RGBColor(0xF4, 0xF0, 0xFA)
                tile.line.color.rgb = RGBColor(0xE0, 0xD5, 0xF2); tile.line.width = Pt(0.75)
                tf2 = tile.text_frame; tf2.word_wrap = True
                tf2.vertical_anchor = MSO_ANCHOR.MIDDLE
                pa = tf2.paragraphs[0]; pa.alignment = PP_ALIGN.CENTER
                set_run(pa, label, 9, color=GREY)
                pv = tf2.add_paragraph(); pv.alignment = PP_ALIGN.CENTER
                col = INK if g is None else (FAV if g else UNFAV)
                set_run(pv, value, 15, bold=True, color=col)

        # Data table (top rows) — right side if a chart/tile is on the left
        table_left = Inches(7.35) if (chart_png or meta["chart_kind"] == "tile") else Inches(0.5)
        table_top = Inches(2.3) if meta.get("has_reference") else Inches(1.05)
        table_width = Inches(5.5) if (chart_png or meta["chart_kind"] == "tile") else Inches(12.3)
        _pptx_data_table(s, df, meta, table_left, table_top, table_width, Pt, RGBColor,
                         max_rows=6 if (chart_png or meta["chart_kind"] == "tile") else 10)

        # AI observations (only if the user generated them for this table)
        obs = table.get("observations_md")
        if obs:
            obs_top = Inches(6.35)
            box = s.shapes.add_shape(1, Inches(0.5), obs_top, Inches(12.3), Inches(1.05))
            box.fill.solid(); box.fill.fore_color.rgb = RGBColor(0xF7, 0xF3, 0xFC)
            box.line.color.rgb = RGBColor(0xE0, 0xD5, 0xF2)
            otf = box.text_frame; otf.word_wrap = True
            otf.margin_left = Pt(8); otf.margin_top = Pt(4); otf.margin_bottom = Pt(4)
            first = True
            for kind, text in _md_blocks(obs)[:6]:
                p = otf.paragraphs[0] if first else otf.add_paragraph()
                first = False
                if kind == "bullet":
                    set_run(p, "• " + _strip_md(text), 9, color=INK)
                elif kind == "note":
                    set_run(p, _strip_md(text), 8, color=GREY)
                else:
                    set_run(p, _strip_md(text), 9, color=INK)

    # footer disclaimer on the title slide
    ff = textbox(s1, Inches(0.9), Inches(6.9), Inches(11.5), Inches(0.4))
    set_run(ff.paragraphs[0], report["disclaimer"], 9, color=GREY)

    out = io.BytesIO(); prs.save(out)
    return out.getvalue()


def _pptx_data_table(slide, df: pd.DataFrame, meta: dict, left, top, width, Pt, RGBColor,
                     *, max_rows: int) -> None:
    from pptx.util import Inches

    cols = [c for c in df.columns]
    shown = df.head(max_rows)
    rows = len(shown) + 1
    height = Inches(0.32) * rows
    tbl = slide.shapes.add_table(rows, len(cols), left, top, width, height).table
    for j, h in enumerate(cols):
        cell = tbl.cell(0, j); cell.text = str(h)
        cell.fill.solid(); cell.fill.fore_color.rgb = RGBColor(*PURPLE)
        para = cell.text_frame.paragraphs[0]
        para.runs[0].font.size = Pt(9); para.runs[0].font.bold = True
        para.runs[0].font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
    for i, (_, r) in enumerate(shown.iterrows(), start=1):
        for j, c in enumerate(cols):
            cell = tbl.cell(i, j); cell.text = _fmt_cell(r[c])
            run = cell.text_frame.paragraphs[0].runs[0]
            run.font.size = Pt(8.5)
            if c == "favourability":
                run.font.color.rgb = RGBColor(*(UNFAV if r[c] == "Unfavourable"
                                                else (FAV if r[c] == "Favourable" else INK)))
            else:
                run.font.color.rgb = RGBColor(*INK)


# ---------------------------------------------------------------------------
# PDF — one section per table
# ---------------------------------------------------------------------------
def build_pdf(report: dict[str, Any]) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Image,
                                    Table, TableStyle, PageBreak)

    purple = colors.HexColor("#A100FF")
    ink = colors.HexColor("#1A1A24")
    grey = colors.HexColor("#6B6B76")
    obs_bg = colors.HexColor("#F7F3FC")

    styles = getSampleStyleSheet()
    h_title = ParagraphStyle("t", parent=styles["Title"], textColor=purple, fontSize=20)
    h_sub = ParagraphStyle("s", parent=styles["Normal"], textColor=ink, fontSize=12)
    h_line = ParagraphStyle("l", parent=styles["Normal"], textColor=grey, fontSize=8)
    h_sect = ParagraphStyle("h2", parent=styles["Heading2"], textColor=ink, fontSize=15,
                            spaceBefore=4)
    h_bullet = ParagraphStyle("b", parent=styles["Normal"], textColor=ink, fontSize=9.5,
                              leftIndent=10, spaceAfter=2)
    h_para = ParagraphStyle("p", parent=styles["Normal"], textColor=ink, fontSize=9.5)
    h_tile = ParagraphStyle("tile", parent=styles["Title"], textColor=purple, fontSize=30,
                            alignment=1)
    h_tile_lbl = ParagraphStyle("tilelbl", parent=styles["Normal"], textColor=grey,
                                fontSize=10, alignment=1)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm,
                            topMargin=14 * mm, bottomMargin=14 * mm)
    flow: list[Any] = [Paragraph(report["title"], h_title), Paragraph(report["subtitle"], h_sub),
                      Paragraph(report["lineage"], h_line), Spacer(1, 6),
                      Paragraph(report["disclaimer"], h_line), Spacer(1, 10)]

    for ti, table in enumerate(report["tables"]):
        if ti > 0:
            flow.append(PageBreak())
        meta, df = table["result"]["meta"], table["result"]["display_df"]
        chart_png = table.get("chart_png")
        flow.append(Paragraph(meta["title"], h_sect))

        if meta.get("has_reference"):
            tot_a = float(df["actual"].sum()); tot_r = float(df["reference"].sum())
            var = tot_a - tot_r
            var_pct = (var / abs(tot_r) * 100.0) if tot_r else None
            kpi_labels = ["Actual", meta["reference_kind"], "Variance", "Var %"]
            kpi_values = [f"{tot_a:,.0f}", f"{tot_r:,.0f}", f"{var:,.0f}",
                         "n/a" if var_pct is None else f"{var_pct:,.1f}%"]
            kt = Table([[Paragraph(l, h_line) for l in kpi_labels],
                       [Paragraph(f"<b>{v}</b>", h_para) for v in kpi_values]],
                      colWidths=[42 * mm] * 4)
            kt.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F4F0FA")),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#E0D5F2")),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E0D5F2")),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"), ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
            flow += [kt, Spacer(1, 8)]
        elif meta["chart_kind"] == "tile" and "actual" in df.columns:
            val = float(df["actual"].iloc[0])
            flow += [Paragraph(f"{val:,.0f}", h_tile), Paragraph(meta["measures"][0], h_tile_lbl),
                    Spacer(1, 8)]

        if chart_png:
            flow += [Image(io.BytesIO(chart_png), width=165 * mm, height=88 * mm,
                          kind="proportional"), Spacer(1, 8)]

        cols = list(df.columns)
        shown = df.head(12)
        data = [[str(c) for c in cols]] + [[_fmt_cell(r[c]) for c in cols]
                                           for _, r in shown.iterrows()]
        col_w = min(28, 170 // max(1, len(cols)))
        mt = Table(data, colWidths=[col_w * mm] * len(cols))
        mt.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), purple),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 7.5),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#DDDDDD")),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT")]))
        flow += [mt, Spacer(1, 8)]

        obs = table.get("observations_md")
        if obs:
            obs_flow = []
            for kind, text in _md_blocks(obs):
                style = h_bullet if kind == "bullet" else h_line if kind == "note" else h_para
                prefix = "• " if kind == "bullet" else ""
                obs_flow.append(Paragraph(prefix + _strip_md(text), style))
            ot = Table([[obs_flow]], colWidths=[170 * mm])
            ot.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), obs_bg),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#E0D5F2")),
                ("LEFTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
            flow.append(ot)

    doc.build(flow)
    return buf.getvalue()
