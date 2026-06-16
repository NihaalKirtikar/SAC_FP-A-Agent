# SAC FP&A Agent

A single-file Streamlit application that connects to SAP Analytics Cloud (SAC)
for FP&A workflows (OAuth connect, import, multi-action, export).

## Prerequisites
- Python 3.10+
- Access to the SAC tenant and an OAuth client (Interactive Usage)

## Setup
```bash
# 1. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS/Linux

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
copy .env.example .env        # Windows  (cp on macOS/Linux)
# then edit .env and fill in real values
```

## Run
```bash
streamlit run app.py
```
The app opens at http://localhost:8501.

## Configuration
All configuration is via a local `.env` file (see `.env.example` for the full
list of variables). The required variables must be set or the app will not
start; the optional per-capability IDs can be filled in as you confirm them.

## Important notes for collaborators
- **Secrets stay local.** `.env` is git-ignored. Never commit it. The OAuth
  client secret must only ever live in your own local `.env`.
- **Flat-file data stays local.** CSV/Excel/Parquet files and
  `saved_multi_actions.json` are git-ignored by design — data integration is
  performed on local machines only and is never pushed to the repository.
- Import / Multi-Action / Export endpoints are wired against the documented
  SAP APIs but some response keys / payload schemas are tenant-specific and
  marked `# CONFIRM` in `app.py`.
