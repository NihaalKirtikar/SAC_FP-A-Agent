# SAC FP&A Agent — Setup, Diagnostics & Capabilities

**Document date:** 2026-06-24
**Scope:** Summary of all work, findings, and decisions for the SAC FP&A Agent
(a single-file Streamlit app that connects to SAP Analytics Cloud for FP&A
workflows). No secret values are recorded in this document.

> ⚠️ This file lives in the **shared OneDrive/SharePoint folder**, so it syncs to
> everyone who has access to the folder. It contains operational notes only — no
> passwords, client secrets, or tokens.

---

## 1. What the app is

- **Type:** Single-file Streamlit app (`app.py`), Python.
- **Connects to:** SAP Analytics Cloud tenant `accenture-p-test` (region `br10`).
- **Auth model:** OAuth 2.0 **Authorization Code** flow ("Interactive Usage")
  via SAML SSO — a user signs in; the app then exchanges the code for a token.
- **Repo:** local git repo, branch `main`. `.env` (real secrets) is **git-ignored**
  by design and must never be committed.

---

## 2. Local setup performed (this machine only — no colleague impact)

Everything below was created **outside** the shared folder, under
`C:\Users\<you>\AppData\Local\SAC_FPNA_Agent\`, so none of it syncs to colleagues:

| Item | Path | Purpose |
|---|---|---|
| Python virtual env | `…\AppData\Local\SAC_FPNA_Agent\.venv` | Isolated deps (streamlit, pandas, requests, python-dotenv) |
| App launcher | `…\AppData\Local\SAC_FPNA_Agent\Launch SAC FP&A Agent.cmd` | Starts the app on **port 8502** using the venv |
| Diagnostics launcher | `…\AppData\Local\SAC_FPNA_Agent\Run Diagnostics.cmd` | Runs the login diagnostic |
| Diagnostic script | `…\AppData\Local\SAC_FPNA_Agent\diagnose.py` | Read-only OAuth/config check |
| App icon (copy) | `…\AppData\Local\SAC_FPNA_Agent\sac_agent.ico` | Shortcut icon |

**Desktop shortcuts created** (on your OneDrive Desktop, personal — not shared):
- **`SAC FP&A Agent`** → launches the app.
- **`SAC FP&A Agent - Diagnostics`** → runs the login diagnostic.

> Why the venv is outside the shared folder: a `.venv` placed *inside* the folder
> would be git-ignored but **still OneDrive-synced** (hundreds of MB of
> machine-specific binaries pushed to colleagues). Keeping it in local `AppData`
> avoids that entirely.

---

## 3. Port configuration (resolved)

- The app **must run on the port in `SAC_REDIRECT_URI`**, because after sign-in
  SAP redirects the browser back to that exact address.
- **Authoritative value:** `SAC_REDIRECT_URI = http://localhost:8502`.
- Therefore the app, the shared `Launch SAC FP&A Agent.bat`, and the local
  launcher are all aligned on **8502**. (An earlier exploration mistakenly aligned
  to 8501 based on the README; this was corrected — 8502 is correct and the shared
  `.bat` was restored to its original.)

---

## 4. Login diagnosis — current blocker

A read-only diagnostic (`diagnose.py`, also runnable from the Diagnostics desktop
button) checks the whole technical layer. **Latest result:**

| Check | Result |
|---|---|
| `.env` config complete (6 required keys) | ✅ PASS |
| Network reachability to SAP tenant | ✅ PASS |
| App running on the redirect port (8502) | ✅ PASS |
| `client_id` + `redirect_uri` registered (authorize endpoint) | ✅ PASS — HTTP 302 to IdP |
| `client_id` + `client_secret` accepted (token endpoint) | ❌ **FAIL — HTTP 401 `invalid_client`** |
| OAuth state cache writable | ✅ PASS |

### Root cause
The `client_id` is **valid** (the authorize step succeeded), but client
authentication at the **token endpoint** fails. Because authentication is checked
before anything else, this points specifically at the **`SAC_CLIENT_SECRET` being
wrong, expired, or rotated** (or the OAuth client being disabled).

**Effect on login:** you can sign in at the SAP page, but the app's token exchange
returns `401` and shows *"Token exchange failed: 401"* — so the connection never
completes. This affects **everyone** using the shared `.env`, consistent with a
rotated/expired secret.

### Fix (pending — requires admin action)
Refresh the OAuth client secret in SAC (see §6), update `SAC_CLIENT_SECRET` in
`.env`, then re-run the Diagnostics button — **Section 5 should flip to PASS**.

---

## 5. Authentication / login account notes

- Sign-in currently uses a **colleague's account** because it is a local
  password user (no corporate SSO redirect). The user's own corporate ID requires
  SSO, which doesn't complete cleanly in the localhost flow.
- **Important distinction:** the **OAuth Client ID/Secret authenticate the *app***;
  **your user ID/password authenticate *you*** at the identity provider. A new
  client secret fixes the app layer; it does **not** change whether your own ID
  needs SSO, and it does not grant your user account SAC access.
- **Recommended (compliant) target state:** your **own** SAC account, provisioned
  and entitled in `accenture-p-test`, so you sign in as yourself. Using a shared
  colleague account is generally against security policy and named-user licensing.

---

## 6. Steps to obtain a new OAuth Client ID + Secret

Performed in SAC (needs the System Administration role — likely the owner,
kumar/arun):

1. Sign in to `https://accenture-p-test.br10.analytics.cloud.sap` as admin.
2. Main Menu (☰) → **System** → **Administration** → **App Integration** tab.
3. Confirm **Authorization URL** / **Token URL** match `.env`
   (`…authentication.br10.hana.ondemand.com/oauth/authorize` and `/token`).
4. Under **OAuth Clients** → **Add a New OAuth Client**:
   - **Name:** `SAC FP&A Agent`
   - **Authorization Grant:** **Authorization Code** (not Client Credentials)
   - **Redirect URI:** `http://localhost:8502` (must be exact)
   - **Secret:** auto-generated
5. **Copy the Client ID and Secret immediately** — the secret is shown only once.
6. (If the existing client allows regenerating its secret, do that instead — the
   redirect URI is already registered and only the secret changes.)
7. Hand both values over; update `SAC_CLIENT_SECRET` (and `SAC_CLIENT_ID` if it
   changed) in `.env`, quoting the secret if it contains spaces/`#`.

**Gotchas:** wrong grant type, redirect URI not exactly `http://localhost:8502`,
or a truncated/space-padded secret will all reproduce `invalid_client`.

---

## 7. Sharing approach

- **Chosen method:** native **OneDrive/SharePoint** share link (right-click the
  folder → Share → Copy link). **No GitHub required** — the folder is already on
  SharePoint, so sharing is built in and has zero impact on existing colleagues.
- **GitHub** would only be for version history/external collaborators, and must be
  a **private** repo done from a **separate clone** (not from inside the shared
  folder, to avoid `.git`/OneDrive sync conflicts).
- **Security caveat:** a folder share link also exposes the **live `.env`
  secret** to whoever opens it. Only share with trusted teammates who already use
  this folder; do not circulate the link externally.

---

## 8. Capability inventory (what the app can do in SAC)

All capabilities act with the **signed-in user's SAC permissions**. There are now
**9 capabilities** (tabs):

| # | Capability | What it does in SAC | SAC API | R/W |
|---|---|---|---|---|
| 1 | 🔌 Connect | OAuth sign-in / token refresh / disconnect | `/oauth/authorize`, `/oauth/token` | — |
| 2 | 📦 Models | Discover & select the active model | `GET /dataimport/models` | Read |
| 3 | ⬆️ Ingest Actuals | Load a flat file (CSV) → map → write fact data | `dataimport …/factData`, `/jobs`, `/run`, `/status` | Write |
| 4 | 🧬 **Master Data** *(new)* | Load a flat file → create/update a **dimension's members** | `dataimport …/masterData/{dim}`, `/jobs`, `/run`, `/status` | Write |
| 5 | 🔁 Version Initialization | Run a pre-built Multi Action to initialize a version | `multiActions/{id}/executions` | Write/Exec |
| 6 | ⚙️ Run Data Action | Generic Multi Action runner (needs "Allow External API") | `multiActions/{id}/executions` | Write/Exec |
| 7 | 🎚️ Driver Inputs | Enter a bounded set of driver values (≤200 rows) → write | `dataimport …/factData` | Write |
| 8 | 🗂️ Versions | Read model live; list versions; aggregate a measure | `dataexport …/FactData` (OData) | Read |
| 9 | 📊 KPIs | Read model figures live; display KPIs | `dataexport …/FactData` | Read |

### Role personas (UI presentation only — SAC enforces real security)
- **Power User** → all tabs (including Master Data).
- **Planner** → Connect, Models, Ingest Actuals, Driver Inputs, Versions, KPIs.
- **Management** → Connect, Versions, KPIs.
- *Master Data is Power-User-only by default; it can be added to other personas on request.*

### Dimension reads — system vs generic
- The Data Export read (`/FactData`, no `$select`/`$filter`) returns the **entire
  fact dataset**: **system dimensions** (Version/Category, Date), **Account**, and
  **all generic/user-defined dimensions** (Cost Center, Product, Entity, …) plus
  measures. It is **not** limited to the Version dimension.
- **Fact data vs master data:** the app reads generic dimension members **as they
  appear in fact rows**, and lists dimension IDs from model metadata — but it does
  **not** read full standalone master data (all members/hierarchies of a dimension)
  via the `/MasterData/{dimension}` export endpoint (not wired).

---

## 9. Master Data Update — analysis & what was added

**Question:** can we update master data for a dimension via flat file or via SAP
Datasphere?

| Path | Status | Notes |
|---|---|---|
| **Flat file → master data** | ✅ **Added** (new "Master Data" tab) | Uses the Data Import Service master-data job, reusing the existing upload/run/poll engine. |
| **SAP Datasphere connection** | ❌ Not built | The app has no Datasphere integration. Options: (a) extract from Datasphere's own APIs and push into SAC DIS (the app as middleware — a larger build), or (b) configure a native **SAC↔Datasphere import connection inside SAC** (recommended; managed in SAC, outside this app). |

### About the new "Master Data" tab
- **Inputs:** active model → pick/enter a **dimension ID** → upload CSV (headers
  should match the dimension's member fields: ID + optional Description / hierarchy
  / properties) → optional constant column → **Validate & Update**.
- **Flow:** create master-data job → upload members → run → poll status → report
  rejected rows (same UX as Ingest Actuals).
- **Status:** wired and **boot-tested** (the app compiles and starts cleanly). The
  master-data endpoint path is **tenant-specific** and marked `# CONFIRM` — the tab
  tries the known endpoint variants and reports diagnostics if none answer.
- **Not yet verified end-to-end** against the tenant, because login is blocked by
  the invalid client secret (§4). Once the secret is fixed, the tab can be tested
  and the exact endpoint/wrapper confirmed.

---

## 10. Known caveats / `# CONFIRM` items

- Import / Multi-Action / Export / **Master-Data** endpoint paths and payload
  wrappers are partly tenant-specific and marked `# CONFIRM` in `app.py`. They are
  wired defensively (multiple candidate paths) but should be validated against
  `accenture-p-test`.
- Optional `.env` IDs are currently **empty**:
  `SAC_MODEL_ID`, `SAC_EXPORT_PROVIDER_ID`, `MA_VERSION_INIT_ID`.
  - Empty `MA_VERSION_INIT_ID` → **Version Initialization** is disabled until set.
  - Empty `SAC_EXPORT_PROVIDER_ID` → Versions/KPIs default to the model id.
- Data Export must be **enabled** on the model for Versions/KPIs.
- Multi Actions must be pre-built in SAC with **"Allow External API"** enabled.

---

## 11. Outstanding actions / next steps

- [ ] **Refresh the OAuth client secret** in SAC and update `SAC_CLIENT_SECRET`
      in `.env` (§6) — this unblocks all login/connect.
- [ ] Re-run the **Diagnostics** desktop button → confirm Section 5 = PASS.
- [ ] Complete a real sign-in and verify the connection.
- [ ] **(Compliance)** Provision your **own** SAC account so you don't rely on a
      shared login.
- [ ] **Confirm the Master-Data endpoint** for this tenant (run the new tab once
      login works; share the log if no endpoint answers).
- [ ] Decide whether Datasphere-sourced master data is needed; if so, choose the
      native SAC import connection vs. an app-side connector.
- [ ] (Optional) Create the OneDrive share link for colleagues (§7).

---

## 12. Boundaries (what the app does NOT do)

- No story/dashboard building; no model or dimension **creation** (it updates
  existing dimension **members**, not the dimension definition itself).
- Export tabs are **read-only** (no write-back).
- Does not bypass SAC security — a capability fails server-side if the signed-in
  user lacks the role.
- No SAP Datasphere connectivity (see §9).
