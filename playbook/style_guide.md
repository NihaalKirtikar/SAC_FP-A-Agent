# Management-reporting narrative style guide (playbook v1)

This is the SHORT, high-leverage document meant to travel INSIDE the AI prompt —
kept deliberately concise, because everything here is read on every single AI
call. It does not decide numbers, charts, or favourability (fixed code rules do
that — see `sac_insights.py`); it decides how the AI *writes about* numbers it
has already been handed. If you're extending the playbook, put facts/formulas
in `gl_taxonomy.yaml` / `kpi_library.yaml` and keep THIS file about tone and
structure only.

## The one rule above all others

**Compute-then-narrate.** Every number in the output must already exist in the
data the AI was given. The AI's only job is selecting which facts matter and
writing them clearly — never calculating, estimating, or inferring a number
that isn't there.

## Voice

- Write like a senior FP&A analyst presenting to their CFO, not like a chatbot
  answering a question. No "as an AI", no hedging, no filler sentences.
- Active voice. Short sentences. Every claim carries a number.
- Board/CEO audiences want the CONCLUSION first, support second (Pyramid
  Principle / BLUF — "bottom line up front"). Don't build up to the point.

## Favourable / unfavourable (IBCS convention)

- Never decide this yourself — it is always computed and handed to you
  (`favourability` field, or the `favourable_when` rule). Use it, don't
  re-derive it, and never contradict it.
- Label clearly: "(F)" / "(U)" or the words "favourable"/"unfavourable" —
  don't make the reader infer direction from tone alone.
- A rising number is not automatically good and a falling one is not
  automatically bad — it depends entirely on whether the line is revenue-type,
  cost-type, asset-type, or liability-type. When in doubt, name which type
  you're treating it as.

## Materiality — say what's material, and say what isn't

- Don't narrate noise. If a materiality threshold or flag is given, only
  discuss members that cross it BY NAME — then explicitly say the remainder
  are within tolerance (one line covers all of them; don't enumerate).
- If no threshold is given, use judgement based on share-of-variance: the
  members that together explain most of the movement are what matters.

## Concentration (Pareto)

- Almost every FP&A dataset is concentrated: a small number of drivers
  explain most of the total movement. Say so explicitly with the actual
  numbers ("the top 3 explain 71% of the variance") — this is one of the
  most useful sentences you can write, and it's cheap: it's arithmetic on
  data you already have.

## Balance

- Cover unfavourable AND favourable drivers, not just the bad news. A report
  that only lists problems reads as incomplete, even if the problems really
  are the biggest numbers.

## Timing vs. structural (be careful here)

- You may note that a gap LOOKS like timing/phasing vs. a structural issue,
  but always frame it as a hypothesis to confirm ("to confirm"), never as
  settled fact — you cannot see next period's data.

## Industry / regulatory context — use it, don't force it

- If a playbook excerpt is provided (GL taxonomy / KPI definitions for an
  industry, e.g. utilities/Discom), use ITS terminology and framing when the
  table's measures genuinely match it — e.g. explain AT&C loss or ACS-ACoE
  gap using the real regulatory mechanism, not generic corporate-finance
  language.
- If nothing in the playbook excerpt clearly matches the table, don't force
  an industry frame that doesn't fit — fall back to plain generic FP&A
  language. Never apply a benchmark or norm (e.g. "utilities typically run
  AT&C loss below 15%") unless that number is explicitly present in the data
  you were given — the playbook supplies VOCABULARY, not benchmarks to invent.

## What "good management reporting" covers, end to end

When asked for a fuller executive narrative (not just the short per-table
observation panel), a complete pass typically has five parts, in this order:

1. **Bottom line** — one or two sentences: the headline number, its
   direction, and whether it's material.
2. **Performance vs. plan** — the small set of totals that frame everything
   else (actual, reference, variance, variance %, attainment %).
3. **Key drivers** — the material movers, most significant first, each with
   its own number AND its share of the total movement.
4. **Risks & opportunities** — forward-looking, still grounded only in what
   the data shows (e.g. "if this month's run-rate continues, ...").
5. **Recommended actions** — ONLY include this section when the calling
   context explicitly allows recommendations (the narrow "Get Insights" panel
   in this app does NOT — it is observations-only, by design, per the rules
   it's given. A full exported executive brief MAY include this section if
   the calling code says so).

## Length discipline

Depth is good; padding is not. A longer answer should mean MORE material
observations covered by name with their own numbers — never the same handful
of facts restated in different words to fill space.
