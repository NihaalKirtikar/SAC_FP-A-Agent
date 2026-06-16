"""
SAC FP&A AGENT — single-file Streamlit application.
Paste into Visual Studio as app.py, then:  streamlit run app.py

------------------------------------------------------------------------------
ONE-TIME SETUP
------------------------------------------------------------------------------
1) pip install streamlit requests pandas python-dotenv
2) Create a file named  .env  in the same folder (add it to .gitignore):

   SAC_AUTH_URL=https://accenture-p-test.authentication.br10.hana.ondemand.com/oauth/authorize
   SAC_TOKEN_URL=https://accenture-p-test.authentication.br10.hana.ondemand.com/oauth/token
   SAC_CLIENT_ID=<fresh client id>
   SAC_CLIENT_SECRET=<fresh secret — regenerated after the earlier leak>
   SAC_REDIRECT_URI=http://localhost:8501
   SAC_BASE_URL=https://accenture-p-test.br10.analytics.cloud.sap

   # Per-capability IDs — fill these as you confirm them. App runs without
   # them; each capability shows a banner until its IDs are present.
   SAC_MODEL_ID=
   SAC_EXPORT_PROVIDER_ID=
   MA_VERSION_INIT_ID=

NEVER put the secret anywhere except .env. If it leaks, delete the OAuth
client and create a new one.

------------------------------------------------------------------------------
WHAT IS REAL vs WHAT YOU MUST CONFIRM
------------------------------------------------------------------------------
- Auth (Connect tab) is complete and runnable now.
- Import / Multi-Action / Export calls are wired with the documented endpoints,
  but the exact response keys (jobID vs jobId, executionId), payload wrappers,
  and the multi-action parameter schema MUST be checked against your tenant's
  SAP Business Accelerator Hub entries. Such spots are marked  # CONFIRM.
- Capability 5 is a bounded driver-input form, NOT a live grid (by design).
"""

import os
import json
import time
import secrets
import tempfile
from urllib.parse import urlencode

import requests
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

# ============================================================================
# CONFIG
# ============================================================================
class Config:
    def __init__(self):
        # Required for the app to start (auth layer).
        self.auth_url = self._req("SAC_AUTH_URL")
        self.token_url = self._req("SAC_TOKEN_URL")
        self.client_id = self._req("SAC_CLIENT_ID")
        self.client_secret = self._req("SAC_CLIENT_SECRET")
        self.redirect_uri = self._req("SAC_REDIRECT_URI")
        self.base_url = self._req("SAC_BASE_URL").rstrip("/")
        # Optional per-capability IDs (capabilities self-guard if blank).
        self.model_id = os.environ.get("SAC_MODEL_ID", "").strip()
        self.export_provider_id = os.environ.get("SAC_EXPORT_PROVIDER_ID", "").strip()
        self.ma_version_init_id = os.environ.get("MA_VERSION_INIT_ID", "").strip()

    @staticmethod
    def _req(key):
        v = os.environ.get(key, "").strip()
        if not v:
            raise RuntimeError(f"Missing required config '{key}' in .env")
        return v


try:
    CFG = Config()
    CONFIG_ERROR = None
except RuntimeError as e:
    CFG = None
    CONFIG_ERROR = str(e)

ACCENTURE_PURPLE = "#A100FF"

# ============================================================================
# AUTH — OAuth 2.0 Authorization Code (interactive)
# ============================================================================
# OAuth "state" tokens are persisted to disk (not just memory) so they survive
# an app restart between clicking "Log in" and returning from SAP — otherwise a
# restart would cause a spurious "state mismatch".
_STATE_FILE = os.path.join(tempfile.gettempdir(), "sac_fpa_oauth_states.json")
_STATE_TTL = 900  # seconds a login link stays valid


def _load_oauth_states():
    try:
        with open(_STATE_FILE) as f:
            data = json.loads(f.read())
    except Exception:
        data = {}
    now = time.time()
    return {s: t for s, t in data.items() if now - t < _STATE_TTL}


def _remember_oauth_state(state):
    data = _load_oauth_states()
    data[state] = time.time()
    try:
        with open(_STATE_FILE, "w") as f:
            f.write(json.dumps(data))
    except Exception:
        pass


def _consume_oauth_state(state):
    """True if we issued this state (and it's not expired); then remove it."""
    data = _load_oauth_states()
    ok = state in data
    if ok:
        data.pop(state, None)
        try:
            with open(_STATE_FILE, "w") as f:
                f.write(json.dumps(data))
        except Exception:
            pass
    return ok


def build_login_url():
    state = secrets.token_urlsafe(24)
    _remember_oauth_state(state)
    st.session_state["oauth_state"] = state
    params = {
        "response_type": "code",
        "client_id": CFG.client_id,
        "redirect_uri": CFG.redirect_uri,
        "state": state,
        # No scope: Interactive Usage clients inherit the user's permissions.
    }
    return f"{CFG.auth_url}?{urlencode(params)}"


def exchange_code_for_token(code):
    r = requests.post(
        CFG.token_url,
        data={"grant_type": "authorization_code", "code": code,
              "redirect_uri": CFG.redirect_uri},
        auth=(CFG.client_id, CFG.client_secret),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def refresh_access_token(refresh_token):
    r = requests.post(
        CFG.token_url,
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        auth=(CFG.client_id, CFG.client_secret),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _store_token(tok):
    st.session_state["access_token"] = tok["access_token"]
    if "refresh_token" in tok:
        st.session_state["refresh_token"] = tok["refresh_token"]
    st.session_state["token_expiry"] = time.time() + int(tok.get("expires_in", 3600)) - 60


def is_authenticated():
    return bool(st.session_state.get("access_token"))


def get_valid_token():
    if not is_authenticated():
        return None
    if time.time() >= st.session_state.get("token_expiry", 0):
        rt = st.session_state.get("refresh_token")
        if not rt:
            logout(); return None
        try:
            _store_token(refresh_access_token(rt))
        except requests.HTTPError:
            logout(); return None
    return st.session_state["access_token"]


def logout():
    # Local sign-out: drop the in-session tokens and any cached app state so a
    # fresh login starts clean. (Does not revoke the token server-side.)
    for k in ("access_token", "refresh_token", "token_expiry", "oauth_state",
              "model_id", "model_name", "models_cache", "models_raw"):
        st.session_state.pop(k, None)


# ============================================================================
# CLIENT — session wrapper, bearer header, CSRF for write calls
# ============================================================================
class SacClient:
    def __init__(self, token, base_url):
        self.base = base_url.rstrip("/")
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"
        self._csrf = None

    def _fetch_csrf(self):
        r = self.s.get(f"{self.base}/api/v1/dataimport/models",
                       headers={"x-csrf-token": "fetch"}, timeout=30)
        self._csrf = r.headers.get("x-csrf-token")
        return self._csrf

    def get(self, path, **kw):
        return self.s.get(f"{self.base}{path}", timeout=60, **kw)

    def post(self, path, **kw):
        if not self._csrf:
            self._fetch_csrf()
        headers = kw.pop("headers", {})
        if self._csrf:
            headers["x-csrf-token"] = self._csrf
        return self.s.post(f"{self.base}{path}", headers=headers, timeout=120, **kw)


def client():
    return SacClient(get_valid_token(), CFG.base_url)


# ============================================================================
# PROGRESS + POLLER — friendly live status with a real-time technical log
# ============================================================================
# SAP returns job/execution state under different keys and with many spellings.
DONE_STATES = {"COMPLETED", "DONE", "SUCCESS", "SUCCEEDED", "FINISHED", "OK"}
FAIL_STATES = {"FAILED", "ERROR", "ERRORED", "ABORTED", "CANCELLED", "CANCELED",
               "COMPLETED_WITH_FAILURES", "COMPLETED_WITH_ERRORS", "REJECTED"}

# Plain-language label for each raw status string we might see.
_STATUS_WORDS = {
    "READY_FOR_TRANSFER": "Job ready — preparing to transfer data",
    "READY_FOR_DATA": "Job ready — preparing to transfer data",
    "QUEUED": "Queued in SAP, waiting to start",
    "PENDING": "Queued in SAP, waiting to start",
    "VALIDATING": "Validating your rows in SAP",
    "VALIDATED": "Validation passed",
    "IN_PROGRESS": "Importing data into the model",
    "RUNNING": "Importing data into the model",
    "PROCESSING": "Importing data into the model",
    "COMPLETED": "Completed successfully",
    "DONE": "Completed successfully",
    "SUCCESS": "Completed successfully",
    "SUCCEEDED": "Completed successfully",
    "FAILED": "Failed",
    "ERROR": "Failed",
    "COMPLETED_WITH_FAILURES": "Completed, but some rows were rejected",
}


def _extract_status(data):
    """SAP uses different keys per service — check the common ones."""
    if not isinstance(data, dict):
        return ""
    for key in ("status", "factDataJobStatus", "jobStatus", "state",
                "executionStatus", "importStatus", "validationStatus"):   # CONFIRM
        val = data.get(key)
        if val:
            return str(val)
    return ""


def _friendly(raw):
    if not raw:
        return "Working… (waiting for SAP to report a status)"
    return _STATUS_WORDS.get(raw.upper(), f"Status: {raw}")


class JobProgress:
    """A friendly, live status panel with a collapsible real-time log.

    .step()   updates the big human-readable line (and logs it)
    .log()    appends a timestamped line to the technical log only
    .finish() sets the final success/error state
    """
    def __init__(self, title="Working…"):
        self.box = st.status(title, expanded=True)
        self.headline = self.box.empty()
        self.sub = self.box.empty()
        self.lines = []
        self._exp = st.expander("🔎 Technical log (live) — open to see details")
        self._area = self._exp.empty()
        self.start = time.time()

    def step(self, message, emoji="⏳", sub=None):
        self.headline.markdown(f"### {emoji}  {message}")
        if sub is not None:
            self.sub.caption(sub)
        self.log(message)

    def log(self, message):
        self.lines.append(f"`{time.strftime('%H:%M:%S')}`  {message}")
        self._area.markdown("\n\n".join(self.lines[-400:]))

    def finish(self, ok, message):
        emoji = "✅" if ok else "❌"
        self.headline.markdown(f"### {emoji}  {message}")
        self.sub.caption(f"Total time: {int(time.time() - self.start)}s")
        self.log(message)
        self.box.update(label=message, state="complete" if ok else "error")


def poll_job(c, status_path, timeout_s=900, interval_s=4, progress=None):
    start = time.time()
    data, prev = {}, object()
    while time.time() - start < timeout_s:
        try:
            data = c.get(status_path).json()
        except Exception as e:
            if progress:
                progress.log(f"⚠️ Poll request errored: {e}")
            return {"status": "poll_error", "detail": str(e)}
        raw = _extract_status(data)
        elapsed = int(time.time() - start)
        if progress:
            if raw != prev:                    # state changed -> headline + full payload
                progress.step(_friendly(raw), sub=f"{elapsed}s elapsed")
                progress.log(f"[{elapsed}s] server response: {data}")
                prev = raw
            else:                              # unchanged -> light heartbeat
                progress.log(f"[{elapsed}s] still '{raw or '(no status field)'}'…")
        norm = raw.upper()
        if norm in DONE_STATES:
            return {"status": "done", "detail": data}
        if norm in FAIL_STATES:
            return {"status": "failed", "detail": data}
        time.sleep(interval_s)
    return {"status": "timeout", "detail": data}


def _safe_json(resp):
    try:
        return resp.json()
    except Exception:
        return {}


def report_rejected_rows(prog, up, resp, failed_n):
    """Explain an upload where SAP rejected rows (the job then stalls forever)."""
    n = failed_n or up.get("totalNumberRowsInCurrentRequest") or "all"
    prog.finish(False, f"SAP rejected {n} row(s) — 0 imported, so the job stays "
                       f"'waiting for data'. Fix the data and re-import.")
    st.error(
        "**All/most rows were rejected at upload**, which is why the import never "
        "finishes. Most common causes:\n\n"
        "• CSV **column headers must exactly match the model's dimension / account "
        "IDs** (case-sensitive), plus the mandatory **Version** and **Date** "
        "members.\n"
        "• A value isn't an existing **member** of its dimension.\n"
        "• A measure column (e.g. a name with spaces) doesn't match a model measure.")
    rejected = up.get("failedRows") or []
    rows_out = []
    for item in rejected[:100]:
        if not isinstance(item, dict):
            continue
        reason = (item.get("messages") or item.get("message") or item.get("reason")
                  or item.get("errorMessage") or item.get("errors") or "")
        if isinstance(reason, (list, tuple)):
            reason = " | ".join(str(x) for x in reason)
        rec = {"why_rejected": str(reason)}
        if isinstance(item.get("row"), dict):
            rec.update(item["row"])
        rows_out.append(rec)
    if rows_out:
        st.markdown("**Why each row was rejected** (first 100):")
        st.dataframe(pd.DataFrame(rows_out), use_container_width=True)
    with st.expander("Full upload response from SAP"):
        st.write(up if up else resp.text)


def need(*vals):
    """True if every required config value is present."""
    return all(v for v in vals)


# ============================================================================
# CAPABILITY 1 — CONNECT
# ============================================================================
def cap_connect():
    st.subheader("Connect to SAP Analytics Cloud")
    if is_authenticated():
        st.success("Connected. The agent is acting with your SAC permissions.")
        get_valid_token()  # triggers refresh if near expiry
        exp = st.session_state.get("token_expiry", 0)
        st.caption("Token valid until " +
                   time.strftime("%H:%M:%S", time.localtime(exp)))
        if st.button("Disconnect"):
            logout(); st.rerun()
        return

    qp = st.query_params
    if "code" in qp:
        returned_state = qp.get("state")
        state_ok = bool(returned_state) and (
            returned_state == st.session_state.get("oauth_state")
            or _consume_oauth_state(returned_state))
        if state_ok:
            try:
                _store_token(exchange_code_for_token(qp["code"]))
                st.query_params.clear(); st.rerun()
            except requests.HTTPError as e:
                st.session_state["login_hint"] = (
                    f"Token exchange failed: {e.response.status_code} — "
                    f"{e.response.text}")
                st.query_params.clear(); st.rerun()
        else:
            st.session_state["login_hint"] = (
                "Your login link expired or didn't match — usually an app restart "
                "or a stale link. Just click **Log in to SAC** again below.")
            st.query_params.clear(); st.rerun()

    hint = st.session_state.pop("login_hint", None)
    if hint:
        st.warning(hint)

    # Same-tab navigation (target="_self") so the OAuth redirect returns here,
    # not into a new tab — st.link_button would force target="_blank".
    st.markdown(
        f'<a href="{build_login_url()}" target="_self" class="acn-login-btn">'
        f'🔐 Log in to SAC</a>',
        unsafe_allow_html=True)
    st.caption("Opens the SAC sign-in **in this same tab** and brings you back "
               "here once you're authenticated.")


# ============================================================================
# CAPABILITY — MODEL ACCESS (discover & select a model from SAC)
# ============================================================================
def _parse_models(raw):
    """Normalise the /dataimport/models payload to a list of {'id','name'}."""
    if isinstance(raw, dict):
        items = raw.get("models") or raw.get("value") or []        # CONFIRM wrapper
    elif isinstance(raw, list):
        items = raw
    else:
        items = []
    out = []
    for m in items:
        if not isinstance(m, dict):
            continue
        mid = m.get("modelID") or m.get("modelId") or m.get("id")  # CONFIRM key
        name = (m.get("modelName") or m.get("modelDescription")
                or m.get("name") or m.get("description") or mid)
        if mid:
            out.append({"id": mid, "name": name})
    return out


def effective_model_id():
    """A model picked in the UI (session) overrides SAC_MODEL_ID from .env."""
    return st.session_state.get("model_id") or CFG.model_id


def effective_model_name():
    """Friendly name of the active model — shown identically on every tab."""
    name = st.session_state.get("model_name")
    if name:
        return name
    mid = effective_model_id()
    if not mid:
        return ""
    for m in (st.session_state.get("models_cache") or []):
        if m.get("id") == mid:
            return m.get("name") or mid
    return mid


def _parse_model_columns(meta):
    """Pull the list of column / dimension IDs the model's import expects."""
    names = []
    if isinstance(meta, dict):
        fact = meta.get("factData") or meta.get("factdata") or meta
        cols = (fact.get("columns") or fact.get("Columns")) if isinstance(fact, dict) else None
        cols = cols or meta.get("columns")
        if isinstance(cols, list):
            for col in cols:
                if isinstance(col, dict):
                    nm = (col.get("columnName") or col.get("name")
                          or col.get("id") or col.get("dimension"))
                    if nm:
                        names.append(str(nm))
                elif isinstance(col, str):
                    names.append(col)
        if not names:                          # fall back to a dimensions list
            dims = meta.get("dimensions")
            if isinstance(dims, list):
                for d in dims:
                    nm = (d.get("id") or d.get("name")) if isinstance(d, dict) else d
                    if nm:
                        names.append(str(nm))
    seen, out = set(), []                       # de-dupe, keep order
    for n in names:
        if n not in seen:
            seen.add(n); out.append(n)
    return out


def fetch_model_columns(c, mid):
    """(columns, raw_metadata) for a model's fact-data import structure."""
    raw = {}
    for path in (f"/api/v1/dataimport/models/{mid}/metadata",
                 f"/api/v1/dataimport/models/{mid}"):            # CONFIRM endpoint
        r = c.get(path)
        if r.status_code == 200:
            raw = _safe_json(r) or {}
            cols = _parse_model_columns(raw)
            if cols:
                return cols, raw
    return [], raw


def _parse_multi_actions(raw, mid=None):
    """Normalise a multi-actions listing to [{'id','name','models'}]."""
    if isinstance(raw, dict):
        items = (raw.get("multiActions") or raw.get("value")
                 or raw.get("results") or raw.get("dataActions") or [])
    elif isinstance(raw, list):
        items = raw
    else:
        items = []
    out = []
    for a in items:
        if not isinstance(a, dict):
            continue
        aid = (a.get("multiActionId") or a.get("multiActionID")
               or a.get("id") or a.get("dataActionId"))             # CONFIRM key
        nm = (a.get("name") or a.get("multiActionName")
              or a.get("description") or aid)
        models = (a.get("models") or a.get("modelIds")
                  or a.get("modelID") or a.get("modelId") or [])
        if isinstance(models, str):
            models = [models]
        if aid:
            out.append({"id": aid, "name": str(nm), "models": list(models)})
    if mid:                       # best-effort filter to the active model
        scoped = [a for a in out
                  if a["models"] and any(mid == str(m) or mid in str(m)
                                         for m in a["models"])]
        return scoped or out      # if linkage isn't exposed, return everything
    return out


def fetch_multi_actions(c, mid=None):
    """(actions, raw) — multi/data actions exposed to the API."""
    raw = {}
    for path in ("/api/v1/multiActions",
                 "/api/v1/multiActions/",
                 "/api/v1/dataActions"):                            # CONFIRM endpoint
        r = c.get(path)
        if r.status_code == 200:
            raw = _safe_json(r) or {}
            acts = _parse_multi_actions(raw, mid)
            if acts:
                return acts, raw
    return [], raw


def probe_action_endpoints(c, mid):
    """Try candidate endpoints and report what each returns, to discover which
    (if any) lists actions in this tenant."""
    candidates = ["/api/v1/multiActions", "/api/v1/multiActions/",
                  "/api/v1/dataActions"]
    if mid:
        candidates += [f"/api/v1/dataimport/models/{mid}/dataActions",
                       f"/api/v1/models/{mid}/multiActions"]
    rows, seen = [], set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        try:
            r = c.get(path)
            parsed = len(_parse_multi_actions(_safe_json(r))) if r.status_code == 200 else 0
            rows.append({"endpoint": path, "HTTP": r.status_code,
                         "actions_parsed": parsed, "snippet": r.text[:140]})
        except Exception as e:
            rows.append({"endpoint": path, "HTTP": "ERR",
                         "actions_parsed": 0, "snippet": str(e)[:140]})
    return rows


_MA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "saved_multi_actions.json")


def _load_saved_mas():
    try:
        with open(_MA_FILE) as f:
            data = json.loads(f.read())
        return [m for m in data if isinstance(m, dict) and m.get("id")]
    except Exception:
        return []


def _save_mas(lst):
    try:
        with open(_MA_FILE, "w") as f:
            f.write(json.dumps(lst, indent=2))
    except Exception:
        pass


def cap_models():
    st.subheader("Model Access")
    st.caption("Discover the SAC models your signed-in account can access and "
               "set the active one. Used by Ingest Actuals and Driver Inputs — "
               "no .env editing needed.")

    if st.button("Load / refresh my models", type="primary"):
        c = client()
        with st.status("Reading models from SAC…", expanded=True) as box:
            try:
                r = c.get("/api/v1/dataimport/models")
                r.raise_for_status()
                st.session_state["models_raw"] = r.json()
                st.session_state["models_cache"] = _parse_models(
                    st.session_state["models_raw"])
                box.update(
                    label=f"Found {len(st.session_state['models_cache'])} model(s).",
                    state="complete")
            except requests.HTTPError as e:
                box.update(label="Request failed", state="error")
                st.error(f"{e.response.status_code} — {e.response.text}"); return
            except Exception as e:
                box.update(label="Error", state="error"); st.exception(e); return

    models = st.session_state.get("models_cache")
    if models:
        kw = st.text_input(
            "🔎 Search models (name or ID)", key="model_search",
            placeholder="type a keyword, e.g. DISCOM, Plan, Actuals…").strip()
        if kw:
            k = kw.lower()
            filtered = [m for m in models
                        if k in str(m["name"]).lower() or k in str(m["id"]).lower()]
            st.caption(f"{len(filtered)} of {len(models)} models match “{kw}”.")
        else:
            filtered = models
            st.caption(f"{len(models)} model(s) available.")

        if not filtered:
            st.warning("No models match that keyword — clear the search to see all.")
        else:
            # String options so the dropdown's own type-ahead matches on names too.
            labels = [f"{m['name']}  ·  {m['id']}" for m in filtered]
            by_label = {lab: m for lab, m in zip(labels, filtered)}
            cur_id = st.session_state.get("model_id")
            cur_label = next((lab for lab, m in by_label.items()
                              if m["id"] == cur_id), labels[0])
            sel_label = st.selectbox("Select your model", options=labels,
                                     index=labels.index(cur_label))
            chosen = by_label[sel_label]
            if st.button("Use this model", type="primary"):
                st.session_state["model_id"] = chosen["id"]
                st.session_state["model_name"] = chosen["name"]
                st.success(f"Active model set: {chosen['name']} ({chosen['id']})")

        st.dataframe(pd.DataFrame(filtered), use_container_width=True,
                     hide_index=True)
    elif models == []:
        st.warning("Connected, but no models were parsed from the response. "
                   "Raw payload is below — share it and I'll map the keys.")
        with st.expander("Raw /api/v1/dataimport/models response"):
            st.write(st.session_state.get("models_raw"))
    else:
        st.info("Click **Load / refresh my models** to fetch the list from SAC.")

    with st.expander("Or enter a Model ID manually"):
        manual = st.text_input("Model ID", value=st.session_state.get("model_id", ""))
        if st.button("Set manual model ID"):
            mid_in = manual.strip()
            st.session_state["model_id"] = mid_in
            st.session_state["model_name"] = next(
                (m["name"] for m in (st.session_state.get("models_cache") or [])
                 if m["id"] == mid_in), mid_in)
            st.success(f"Active model set: {mid_in or '(cleared)'}")

    active = effective_model_id()
    st.caption(f"**Active model:** {active}" if active
               else "**Active model:** none selected")


# ============================================================================
# CAPABILITY 2 — INGEST ACTUALS (Data Import Service)
# ============================================================================
def cap_actuals():
    st.subheader("Ingest Actuals")
    mid = effective_model_id()
    if not need(mid):
        st.error("PREREQUISITE MISSING — choose a model on the **Models** tab "
                 "(or set SAC_MODEL_ID in .env)."); return

    up = st.file_uploader("Upload actuals CSV", type=["csv"])
    if not up:
        st.info("Version and Date columns are mandatory and must be existing "
                "members in the model."); return

    df = pd.read_csv(up)
    st.caption(f"{len(df):,} rows × {len(df.columns)} columns detected.")
    st.markdown("**Full data preview** — scroll rows (↕) and columns (↔), "
                "sort by clicking a header, or use the toolbar (search / "
                "download / fullscreen) that appears on hover.")
    st.dataframe(df, use_container_width=True, height=430)

    # ---- Column mapping: flat-file columns -> model dimensions/measures ----
    st.markdown("---")
    st.markdown("### 🔗 Map columns to the model")
    st.caption("SAP rejects rows whose headers don't match the model's dimension / "
               "measure IDs. Load the model's columns, then map each to a column "
               "from your file. (Optional if your headers already match exactly.)")
    csv_cols = list(df.columns)
    if st.button("📥 Load model columns"):
        c = client()
        try:
            cols, meta = fetch_model_columns(c, mid)
            st.session_state["model_columns"] = cols
            st.session_state["model_meta_raw"] = meta
        except Exception as e:
            st.session_state["model_columns"] = []
            st.session_state["model_meta_raw"] = {"error": str(e)}
        st.session_state["model_columns_for"] = mid

    model_cols = st.session_state.get("model_columns")
    if model_cols is not None and st.session_state.get("model_columns_for") != mid:
        st.info("Model changed since columns were loaded — click "
                "**📥 Load model columns** to refresh.")
        model_cols = None

    mapping = {}                               # target model column -> source CSV column
    NONE = "— not mapped —"
    if model_cols:
        st.caption("For each **model column**, pick the matching **file column**. "
                   "A file column you've already used disappears from the other "
                   "lists so it can't be mapped twice.")
        # what each target currently uses (from prior reruns), to exclude elsewhere
        current = {}
        for tgt in model_cols:
            v = st.session_state.get(f"map_{tgt}")
            if v and v != NONE and v in csv_cols:
                current[tgt] = v
        used = set(current.values())

        for tgt in model_cols:
            key = f"map_{tgt}"
            mine = current.get(tgt)
            # offer: unused file columns, plus this target's own current pick
            available = [c for c in csv_cols if c not in used or c == mine]
            opts = [NONE] + available
            if key not in st.session_state:        # first render: auto-match by name
                guess = next((c for c in csv_cols
                              if c.lower() == str(tgt).lower() and c not in used), None)
                st.session_state[key] = guess or NONE
            if st.session_state[key] not in opts:  # stale value -> reset
                st.session_state[key] = NONE
            sel = st.selectbox(f"Model column: **{tgt}**", options=opts, key=key)
            if sel != NONE:
                mapping[tgt] = sel

        leftover = [c for c in csv_cols if c not in set(mapping.values())]
        st.caption("Mapped: " + (", ".join(f"{t} ← {s}"
                   for t, s in mapping.items()) or "none yet"))
        if leftover:
            st.caption("Unused file columns: " + ", ".join(leftover))
    elif model_cols == []:
        st.warning("Couldn't read the model's columns automatically — raw metadata "
                   "below (share it and I'll tune the parser). You can still import "
                   "if your file headers already match the model.")
        with st.expander("Raw model metadata"):
            st.write(st.session_state.get("model_meta_raw"))
    else:
        st.info("Click **📥 Load model columns** to set up mapping.")
    # Optional: force every row into a specific version (write to that version).
    target_cols = list(mapping.keys()) if mapping else list(df.columns)
    with st.expander("🎯 Post to a specific version (optional)"):
        fvcol = st.selectbox("Which column is the Version?",
                             ["— none —"] + target_cols, key="ingest_ver_col")
        fvval = st.text_input("Version to write into (e.g. public.Budget)",
                              key="ingest_ver_val")
    st.markdown("---")

    if st.button("Validate & Import", type="primary"):
        c = client()
        send_df = (pd.DataFrame({t: df[s] for t, s in mapping.items()})
                   if mapping else df).copy()
        if fvcol != "— none —" and fvval.strip():
            send_df[fvcol] = fvval.strip()
        rows = send_df.to_dict(orient="records")
        prog = JobProgress("Importing actuals into SAP…")
        try:
            prog.step("Creating import job…", emoji="🆕")
            job = c.post(f"/api/v1/dataimport/models/{mid}/factData").json()
            job_id = job.get("jobID") or job.get("jobId")              # CONFIRM
            prog.log(f"factData response: {job}")
            prog.log(f"Job ID: {job_id}")

            prog.step(f"Uploading {len(rows):,} rows…", emoji="⬆️")
            r_up = c.post(f"/api/v1/dataimport/jobs/{job_id}",
                          json={"Data": rows})                          # CONFIRM wrapper
            up = _safe_json(r_up)
            prog.log(f"upload → HTTP {r_up.status_code}: {r_up.text[:600]}")
            failed_n = up.get("failedNumberRows", 0)
            upserted_n = up.get("upsertedNumberRows")
            if r_up.status_code not in (200, 201, 202) or failed_n or upserted_n == 0:
                report_rejected_rows(prog, up, r_up, failed_n)
                return
            prog.log(f"{upserted_n} rows accepted, {failed_n} failed")

            prog.step("Submitting import run…", emoji="🚀")
            r_run = c.post(f"/api/v1/dataimport/jobs/{job_id}/run")
            prog.log(f"run → HTTP {r_run.status_code}: {r_run.text[:300]}")
            if r_run.status_code not in (200, 201, 202):
                prog.finish(False, f"SAP did not start the import (HTTP {r_run.status_code}).")
                st.error(f"The import run was rejected. Response:\n\n{r_run.text[:600]}")
                return

            res = poll_job(c, f"/api/v1/dataimport/jobs/{job_id}/status", progress=prog)
            ok = res["status"] == "done"
            prog.finish(ok, "Import completed successfully" if ok
                        else f"Import {res['status']} — see log below")
            with st.expander("Final server response"):
                st.write(res["detail"])
        except Exception as e:
            prog.finish(False, "Unexpected error — see log")
            st.exception(e)


# ============================================================================
# CAPABILITY 3 — VERSION INITIALIZATION (Multi Action)
# ============================================================================
def run_multi_action(c, ma_id, parameters, progress=None):
    body = {"parameters": parameters} if parameters else {"parameters": []}  # CONFIRM schema; body must not be empty
    if progress:
        progress.step("Triggering multi-action…", emoji="🚀")
    r = c.post(f"/api/v1/multiActions/{ma_id}/executions", json=body)
    if progress:
        progress.log(f"trigger → HTTP {r.status_code}: {r.text[:300]}")
    if r.status_code not in (200, 201, 202):
        return {"status": "trigger_failed", "detail": {"code": r.status_code, "body": r.text}}
    execution_id = r.json().get("executionId")                              # CONFIRM key
    if progress:
        progress.log(f"executionId: {execution_id}")
    return poll_job(c, f"/api/v1/multiActions/{ma_id}/executions/{execution_id}",
                    progress=progress)


def cap_version_init():
    st.subheader("Version Initialization")
    if not need(CFG.ma_version_init_id):
        st.error("PREREQUISITE MISSING — set MA_VERSION_INIT_ID in .env. "
                 "The data action must be wrapped in a Multi Action with "
                 "'Allow External API Access' ON."); return

    src = st.text_input("Source version", value="public.Actual")
    tgt = st.text_input("Target version", value="public.Budget")
    if st.button("Initialize Version", type="primary", disabled=not (src and tgt)):
        c = client()
        params = [                                                          # CONFIRM names match the MA definition
            {"name": "SourceVersion", "value": src},
            {"name": "TargetVersion", "value": tgt},
        ]
        prog = JobProgress("Running version initialization…")
        res = run_multi_action(c, CFG.ma_version_init_id, params, progress=prog)
        ok = res["status"] == "done"
        prog.finish(ok, "Version initialization completed" if ok
                    else f"Version init {res['status']} — see log below")
        with st.expander("Final server response"):
            st.write(res["detail"])


# ============================================================================
# CAPABILITY 4 — OTHER DATA ACTIONS (generic runner)
# ============================================================================
def cap_other_das():
    st.subheader("Run Data Action")
    st.caption("Data actions run through **Multi Actions** ('Allow External API "
               "Access' ON). Active model: "
               f"**{effective_model_name() or '—'}**")
    st.caption("This is also where **version management** runs — publish / copy / "
               "delete a version by wrapping that data action in an API-enabled "
               "Multi Action and running it here.")

    # 1) Try to auto-discover from the API; if nothing lists, probe endpoints.
    if st.button("📥 List data actions for this model"):
        c = client()
        try:
            acts, _ = fetch_multi_actions(c, effective_model_id())
            st.session_state["da_list"] = acts
            st.session_state["da_probe"] = (
                None if acts else probe_action_endpoints(c, effective_model_id()))
        except Exception as e:
            st.session_state["da_list"] = []
            st.session_state["da_probe"] = [{"endpoint": "(exception)", "HTTP": "ERR",
                                             "actions_parsed": 0, "snippet": str(e)[:140]}]

    discovered = st.session_state.get("da_list") or []
    if discovered:
        st.success(f"{len(discovered)} action(s) found via the API.")
        st.dataframe(pd.DataFrame([{"name": a["name"], "id": a["id"],
                      "models": ", ".join(map(str, a.get("models") or []))}
                      for a in discovered]), use_container_width=True, hide_index=True)
    elif st.session_state.get("da_probe"):
        st.warning("Couldn't auto-list actions. The probe below shows what each "
                   "endpoint returns in your tenant — share it and I'll wire the "
                   "right one. Meanwhile, save a Multi Action by ID below to run it "
                   "with no SAC screen.")
        st.dataframe(pd.DataFrame(st.session_state["da_probe"]),
                     use_container_width=True, hide_index=True)

    # 2) Persistent manual registry (survives restarts) — paste an ID once.
    saved = _load_saved_mas()
    with st.expander("➕ Save / manage Multi Actions by ID (run with no SAC screen)"):
        st.caption("Paste a Multi Action ID once — it's stored on disk and stays "
                   "runnable from here, even after restarts.")
        c1, c2 = st.columns(2)
        new_name = c1.text_input("Label", key="ma_new_name")
        new_id = c2.text_input("Multi Action ID", key="ma_new_id")
        if st.button("Save Multi Action"):
            if new_id.strip():
                saved = [m for m in saved if m.get("id") != new_id.strip()]
                saved.append({"name": new_name.strip() or new_id.strip(),
                              "id": new_id.strip()})
                _save_mas(saved)
                st.success(f"Saved “{new_name or new_id}”."); st.rerun()
            else:
                st.error("Enter a Multi Action ID.")
        if saved:
            st.dataframe(pd.DataFrame(saved), use_container_width=True, hide_index=True)
            rm = st.selectbox("Remove one", ["—"] + [m["name"] for m in saved],
                              key="ma_remove")
            if st.button("Remove") and rm != "—":
                _save_mas([m for m in saved if m["name"] != rm]); st.rerun()

    # 3) Run registry = discovered + saved + .env
    registry = {a["name"]: a["id"] for a in discovered}
    for m in saved:
        registry.setdefault(m["name"], m["id"])
    if CFG.ma_version_init_id:
        registry.setdefault("Version Initialization", CFG.ma_version_init_id)
    if not registry:
        st.info("No actions yet — auto-list above, save one by ID, or set MA_*_ID "
                "in .env."); return

    name = st.selectbox("Data action to run", list(registry.keys()))
    raw_params = st.text_area("Parameters as JSON list (optional)", value='[]',
                              help='e.g. [{"name":"Region","value":"APAC"}]')
    if st.button("Run", type="primary"):
        try:
            params = json.loads(raw_params) if raw_params.strip() else []
        except json.JSONDecodeError:
            st.error("Parameters must be valid JSON."); return
        c = client()
        prog = JobProgress(f"Running {name}…")
        res = run_multi_action(c, registry[name], params, progress=prog)
        ok = res["status"] == "done"
        prog.finish(ok, f"{name} completed" if ok
                    else f"{name}: {res['status']} — see log below")
        with st.expander("Final server response"):
            st.write(res["detail"])


# ============================================================================
# CAPABILITY 5 — DRIVER / ASSUMPTION INPUTS (bounded, NOT a grid)
# ============================================================================
MAX_INTERSECTION_ROWS = 200

def cap_drivers():
    st.subheader("Driver / Assumption Inputs")
    st.caption("Enter a bounded set of driver values at chosen intersections. "
               "This is not a planning grid — for large-scale entry use a native "
               "SAC story.")
    mid = effective_model_id()
    if not need(mid):
        st.error("PREREQUISITE MISSING — choose a model on the **Models** tab "
                 "(or set SAC_MODEL_ID in .env)."); return

    n = st.number_input("Number of intersections",
                        min_value=1, max_value=MAX_INTERSECTION_ROWS, value=3)
    st.info("Define the dimension columns your model requires "
            "(Version, Date, Account, + your drivers).")
    template = pd.DataFrame(
        [{"Version": "", "Date": "", "Account": "", "Value": 0.0}] * int(n))
    edited = st.data_editor(template, num_rows="fixed", use_container_width=True)

    if st.button("Submit drivers", type="primary"):
        rows = edited.to_dict(orient="records")
        c = client()
        prog = JobProgress("Importing driver values into SAP…")
        try:
            prog.step("Creating import job…", emoji="🆕")
            job = c.post(f"/api/v1/dataimport/models/{mid}/factData").json()
            job_id = job.get("jobID") or job.get("jobId")              # CONFIRM
            prog.log(f"factData response: {job}")

            prog.step(f"Uploading {len(rows):,} rows…", emoji="⬆️")
            r_up = c.post(f"/api/v1/dataimport/jobs/{job_id}", json={"Data": rows})
            up = _safe_json(r_up)
            prog.log(f"upload → HTTP {r_up.status_code}: {r_up.text[:600]}")
            failed_n = up.get("failedNumberRows", 0)
            upserted_n = up.get("upsertedNumberRows")
            if r_up.status_code not in (200, 201, 202) or failed_n or upserted_n == 0:
                report_rejected_rows(prog, up, r_up, failed_n)
                return

            prog.step("Submitting import run…", emoji="🚀")
            r_run = c.post(f"/api/v1/dataimport/jobs/{job_id}/run")
            prog.log(f"run → HTTP {r_run.status_code}: {r_run.text[:300]}")
            if r_run.status_code not in (200, 201, 202):
                prog.finish(False, f"SAP did not start the import (HTTP {r_run.status_code}).")
                st.error(f"The import run was rejected. Response:\n\n{r_run.text[:600]}")
                return

            res = poll_job(c, f"/api/v1/dataimport/jobs/{job_id}/status", progress=prog)
            ok = res["status"] == "done"
            prog.finish(ok, "Driver values imported" if ok
                        else f"Drivers {res['status']} — see log below")
            with st.expander("Final server response"):
                st.write(res["detail"])
            if ok:
                st.caption("Now run the apply/disaggregation data action (tab: Run Data Action).")
        except Exception as e:
            prog.finish(False, "Unexpected error — see log")
            st.exception(e)


# ============================================================================
# CAPABILITY 6 — KPIs (Data Export read; display, don't re-derive)
# ============================================================================
def cap_kpis():
    st.subheader("KPIs")
    st.caption("Reads model figures live via the Data Export API and shows them "
               "here — no SAC UI needed.")
    default_provider = effective_model_id() or CFG.export_provider_id
    provider = st.text_input("Export provider (defaults to the active model)",
                             value=st.session_state.get("export_provider",
                                                        default_provider or ""),
                             key="kpi_provider")
    select = st.text_input("$select (comma-separated columns, optional)", value="")
    odata_filter = st.text_input("$filter (OData, optional)", value="")
    if st.button("Read model data", type="primary"):
        if not provider.strip():
            st.error("Enter a provider / model id."); return
        st.session_state["export_provider"] = provider.strip()
        q = []
        if select.strip():
            q.append("$select=" + select.strip())
        if odata_filter.strip():
            q.append("$filter=" + odata_filter.strip())
        c = client()
        with st.spinner("Reading model data…"):
            rows, fail = read_fact_data(c, provider.strip(), query="&".join(q))
        if rows is None:
            st.session_state.pop("kpi_rows", None)
            st.error(f"Couldn't read data (HTTP {getattr(fail, 'status_code', '?')}).")
            with st.expander("Raw response"):
                st.code(fail.text[:800] if fail is not None else "(none)")
            return
        st.session_state["kpi_rows"] = rows

    rows = st.session_state.get("kpi_rows")
    if not rows:
        st.info("Add optional $select / $filter and click **Read model data**."); return
    df = pd.DataFrame(rows)
    st.caption(f"{len(df):,} rows × {len(df.columns)} columns.")
    st.dataframe(df, use_container_width=True, height=460)
    st.caption("Display model-calculated KPIs as-is. Compute ONLY presentation "
               "ratios that don't already exist in the model, and label them as "
               "derived — never re-derive a model figure here.")


# ============================================================================
# APP SHELL
# ============================================================================
THEME_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap');
:root{ --acn-purple:#A100FF; --acn-violet:#7500C0; --acn-magenta:#E6007E; --acn-ink:#2A0A4A; }
html, body, .stApp, [class*="css"] { font-family:'Inter', "Segoe UI", sans-serif; }
.stApp{
  background:
    radial-gradient(1100px 480px at 100% -8%, rgba(230,0,126,0.06), transparent 60%),
    radial-gradient(900px 480px at -8% 0%, rgba(161,0,255,0.07), transparent 55%),
    #F4F3F9;
}
[data-testid="stHeader"] { background:transparent; }
.block-container { padding-top:2rem; max-width:1300px; }
h1,h2,h3 { color:var(--acn-ink); font-weight:800; letter-spacing:-0.015em; }

/* gentle transitions on interactive elements */
button, a, [role="radiogroup"] label, [data-testid="stExpander"],
[data-testid="stMetric"], input, textarea, [data-baseweb="select"]>div {
  transition: all .18s cubic-bezier(.2,.7,.3,1) !important;
}

/* ---------- Hero (animated gradient + shine sweep) ---------- */
.acn-hero{
  position:relative; overflow:hidden;
  background:linear-gradient(120deg,#A100FF 0%,#7500C0 45%,#E6007E 100%);
  background-size:200% 200%; animation:acnShift 12s ease infinite;
  border-radius:20px; padding:26px 32px; margin:0 0 14px 0;
  display:flex; align-items:center; gap:20px;
  box-shadow:0 18px 40px rgba(117,0,192,0.30);
}
@keyframes acnShift{ 0%{background-position:0% 50%} 50%{background-position:100% 50%} 100%{background-position:0% 50%} }
.acn-hero::after{
  content:""; position:absolute; top:0; left:-60%; width:40%; height:100%;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.18),transparent);
  transform:skewX(-20deg); animation:acnShine 7s ease-in-out infinite;
}
@keyframes acnShine{ 0%,55%{left:-60%} 100%{left:140%} }
.acn-hero .mark{
  font-size:42px; font-weight:900; color:#fff; line-height:1;
  background:rgba(255,255,255,0.18); border-radius:16px;
  width:64px; height:64px; min-width:64px; display:flex;
  align-items:center; justify-content:center;
  box-shadow:inset 0 0 0 1px rgba(255,255,255,.25);
}
.acn-hero .title{ color:#fff; font-size:30px; font-weight:900; margin:0; letter-spacing:-0.02em; }
.acn-hero .sub{ color:rgba(255,255,255,0.9); font-size:14px; margin-top:4px; }

/* ---------- Sidebar ---------- */
[data-testid="stSidebar"]{ background:linear-gradient(180deg,#330C56 0%,#1B1B2F 100%); }
[data-testid="stSidebar"] *{ color:#ECE3FB !important; }
.acn-side-title{ font-size:20px; font-weight:900; color:#fff !important; line-height:1.1; }
.acn-side-sub{ font-size:11.5px; color:#B9A2E6 !important; margin:2px 0 8px 0; }
[data-testid="stSidebar"] [role="radiogroup"] label{
  padding:8px 12px; border-radius:11px; margin-bottom:4px;
}
[data-testid="stSidebar"] [role="radiogroup"] label:hover{
  background:rgba(161,0,255,0.22); transform:translateX(3px);
}
[data-testid="stSidebar"] [role="radiogroup"] label:has(input:checked){
  background:linear-gradient(90deg,rgba(230,0,126,.35),rgba(161,0,255,.22));
  box-shadow:inset 3px 0 0 #E6007E;
}

/* ---------- Buttons ---------- */
.stButton>button{
  border-radius:11px; border:1px solid #D9CBEF; font-weight:600;
  background:#fff; color:#3A1466;
}
.stButton>button:hover{
  border-color:var(--acn-purple); color:var(--acn-purple);
  transform:translateY(-1px); box-shadow:0 6px 16px rgba(161,0,255,.16);
}
.stButton>button[kind="primary"], [data-testid="stLinkButton"] a, .acn-login-btn{
  background:linear-gradient(120deg,#A100FF,#7500C0) !important; color:#fff !important;
  border:0 !important; border-radius:11px !important; font-weight:700 !important;
  padding:.55rem 1.2rem !important; box-shadow:0 8px 20px rgba(161,0,255,0.32) !important;
  text-decoration:none;
}
.stButton>button[kind="primary"]:hover, [data-testid="stLinkButton"] a:hover, .acn-login-btn:hover{
  transform:translateY(-2px); filter:brightness(1.07);
  box-shadow:0 12px 26px rgba(161,0,255,0.45) !important;
}
.acn-login-btn{ display:inline-block; padding:.6rem 1.4rem; }

/* ---------- Inputs (focus ring) ---------- */
.stTextInput input, .stNumberInput input, .stTextArea textarea,
[data-baseweb="select"]>div { border-radius:11px !important; }
.stTextInput input:focus, .stNumberInput input:focus, .stTextArea textarea:focus{
  box-shadow:0 0 0 3px rgba(161,0,255,.18) !important; border-color:var(--acn-purple) !important;
}

/* ---------- Cards / alerts / tables / metrics ---------- */
.stAlert{ border-radius:14px; border:1px solid rgba(161,0,255,.10); }
[data-testid="stExpander"]{ border-radius:14px; border:1px solid #E7DEF7; background:#fff; }
[data-testid="stExpander"]:hover{ box-shadow:0 8px 22px rgba(42,10,74,.08); }
[data-testid="stDataFrame"], [data-testid="stTable"]{
  border-radius:12px; overflow:hidden; box-shadow:0 4px 16px rgba(42,10,74,.06);
}
[data-testid="stMetric"]{
  background:#fff; border:1px solid #ECE3FB; border-radius:16px; padding:14px 16px;
  box-shadow:0 6px 18px rgba(42,10,74,.06);
}
[data-testid="stMetric"]:hover{ transform:translateY(-2px); box-shadow:0 12px 26px rgba(42,10,74,.12); }

/* role / persona badge */
.acn-badge{
  display:inline-block; padding:5px 14px; border-radius:999px; font-weight:700;
  font-size:12.5px; color:#fff !important; background:linear-gradient(120deg,#A100FF,#E6007E);
  box-shadow:0 4px 12px rgba(161,0,255,.30);
}

/* custom scrollbars */
::-webkit-scrollbar{ width:10px; height:10px; }
::-webkit-scrollbar-thumb{ background:linear-gradient(#A100FF,#7500C0); border-radius:10px; }
::-webkit-scrollbar-track{ background:transparent; }
</style>
"""

APP_HEADER_HTML = """
<div class="acn-hero">
  <div class="mark">&gt;</div>
  <div>
    <div class="title">SAC FP&amp;A Agent</div>
    <div class="sub">Financial Planning &amp; Analysis &middot; powered by SAP Analytics Cloud</div>
  </div>
</div>
"""


# ============================================================================
# CAPABILITY — VERSIONS (read versions + their numbers via Data Export)
# ============================================================================
def fetch_export_providers(c):
    for path in ("/api/v1/dataexport/administration/providers",
                 "/api/v1/dataexport/providers/sac"):           # CONFIRM endpoint
        r = c.get(path)
        if r.status_code == 200:
            return _safe_json(r), path
    return None, None


def _export_bases(provider):
    """Candidate fact-data export paths (tenants/versions vary)."""
    return [
        f"/api/v1/dataexport/providers/sac/{provider}/FactData",
        f"/api/v1/dataexport/providers/{provider}/FactData",
        f"/api/v1/dataexport/providers/sac/{provider}/factData",
    ]


def _nextlink_path(url):
    """Turn an @odata.nextLink (full URL or relative) into a path for SacClient."""
    if url.startswith("http"):
        i = url.find("/", url.find("://") + 3)
        return url[i:] if i != -1 else url
    return url if url.startswith("/") else "/" + url


def read_fact_data(c, provider, max_rows=100000, query=""):
    """Read FactData via the Data Export Service using plain OData + server-driven
    paging (@odata.nextLink). No '$pagesize' — that option isn't supported and the
    service returns HTTP 400 for it. Returns (rows, fail_resp)."""
    last = None
    for base in _export_bases(provider):
        r = c.get(base + ("?" + query if query else ""))
        last = r
        if r.status_code != 200:
            continue
        data = _safe_json(r)
        if not isinstance(data, dict):
            continue
        rows = list(data.get("value", []))
        nxt = data.get("@odata.nextLink")
        while nxt and len(rows) < max_rows:
            rr = c.get(_nextlink_path(nxt))
            last = rr
            if rr.status_code != 200:
                break
            d = _safe_json(rr)
            if not isinstance(d, dict):
                break
            rows += d.get("value", [])
            nxt = d.get("@odata.nextLink")
        return rows, None
    return None, last


def probe_export_endpoints(c, provider):
    """Report what each export path/option returns, to pinpoint the working one."""
    base = f"/api/v1/dataexport/providers/sac/{provider}/FactData"
    paths = [
        base,                                  # plain OData (expected to work)
        base + "?$top=1",
        base + "?$pagesize=1",                 # old form — likely HTTP 400
        f"/api/v1/dataexport/providers/{provider}/FactData",
        "/api/v1/dataexport/administration/providers",
    ]
    rows = []
    for path in paths:
        try:
            r = c.get(path)
            rows.append({"endpoint": path, "HTTP": r.status_code,
                         "snippet": r.text[:300]})
        except Exception as e:
            rows.append({"endpoint": path, "HTTP": "ERR", "snippet": str(e)[:300]})
    return rows


def _detect_version_col(df):
    for col in df.columns:
        if str(col).lower() in ("version", "category"):
            return col
    for col in df.columns:
        try:
            if df[col].astype(str).str.match(r"^(public|private)\.").any():
                return col
        except Exception:
            pass
    return df.columns[0] if len(df.columns) else None


def cap_versions():
    st.subheader("Versions")
    st.caption("Reads the model's data via the Data Export API to show which "
               "versions exist and their key numbers — no SAC UI needed.")
    default_provider = effective_model_id() or CFG.export_provider_id
    provider = st.text_input("Export provider (defaults to the active model)",
                             value=st.session_state.get("export_provider",
                                                        default_provider or ""))
    if st.button("📥 Load versions & numbers", type="primary"):
        st.session_state["export_provider"] = provider.strip()
        st.session_state.pop("ver_rows", None)
        st.session_state.pop("ver_diag", None)
        if not provider.strip():
            st.error("Enter a provider / model id."); return
        c = client()
        with st.spinner("Reading model data…"):
            rows, fail = read_fact_data(c, provider.strip())
        if rows is not None:
            st.session_state["ver_rows"] = rows
        else:
            provs, ppath = fetch_export_providers(c)
            st.session_state["ver_diag"] = {
                "http": getattr(fail, "status_code", "?"),
                "body": (fail.text[:800] if fail is not None else ""),
                "probe": probe_export_endpoints(c, provider.strip()),
                "providers": provs, "providers_path": ppath}

    # ---- diagnostics shown in the open (not hidden inside a status box) ----
    diag = st.session_state.get("ver_diag")
    if diag:
        st.error(f"Couldn't read data (HTTP {diag['http']}). Either this model isn't "
                 "export-enabled, or its export-provider id differs from the model id.")
        st.markdown("**Which export paths your tenant answers:**")
        st.dataframe(pd.DataFrame(diag["probe"]), use_container_width=True,
                     hide_index=True)
        if diag.get("providers") is not None:
            st.markdown(f"**Available export providers** (from "
                        f"`{diag.get('providers_path')}`) — pick an id from here and "
                        "paste it into the box above:")
            st.write(diag["providers"])
        with st.expander("Raw response from the read attempt"):
            st.code(diag.get("body") or "(empty)")
        st.caption("If it still won't read, send me this and I'll wire the exact "
                   "provider/endpoint your tenant uses.")

    rows = st.session_state.get("ver_rows")
    if not rows:
        if not diag:
            st.info("Click **Load versions & numbers** to read the model.")
        return

    df = pd.DataFrame(rows)
    cols = list(df.columns)
    vguess = _detect_version_col(df)
    vcol = st.selectbox("Version column", cols,
                        index=cols.index(vguess) if vguess in cols else 0)
    versions = sorted(df[vcol].astype(str).unique())
    st.markdown(f"**{len(versions)} version(s) in the model:**  " + ", ".join(versions))

    numeric = [c for c in cols if pd.api.types.is_numeric_dtype(df[c])]
    if numeric:
        measure = st.selectbox("Total this measure by version", numeric)
        agg = (df.groupby(vcol)[measure].sum().reset_index()
               .sort_values(measure, ascending=False))
        tiles = st.columns(min(len(agg), 4) or 1)
        for i, (_, row) in enumerate(agg.head(8).iterrows()):
            tiles[i % len(tiles)].metric(str(row[vcol]), f"{row[measure]:,.0f}")
        st.bar_chart(agg, x=vcol, y=measure)
        st.dataframe(agg, use_container_width=True, hide_index=True)
    else:
        st.caption("No numeric measure detected to total; showing a data sample.")
        st.dataframe(df.head(200), use_container_width=True, hide_index=True)


ALL_PAGES = {
    "Connect": ("🔌", cap_connect),
    "Models": ("📦", cap_models),
    "Ingest Actuals": ("⬆️", cap_actuals),
    "Version Initialization": ("🔁", cap_version_init),
    "Run Data Action": ("⚙️", cap_other_das),
    "Driver Inputs": ("🎚️", cap_drivers),
    "Versions": ("🗂️", cap_versions),
    "KPIs": ("📊", cap_kpis),
}

# Presentation personas — which tabs each role sees. SAC still enforces the
# real permissions via the signed-in user's token; this only tailors the UI.
ROLE_ACCESS = {
    "Power User": list(ALL_PAGES.keys()),
    "Planner": ["Connect", "Models", "Ingest Actuals", "Driver Inputs",
                "Versions", "KPIs"],
    "Management": ["Connect", "Versions", "KPIs"],
}


def main():
    st.set_page_config(page_title="SAC FP&A Agent", page_icon="📊", layout="wide")
    st.markdown(THEME_CSS, unsafe_allow_html=True)
    st.markdown(APP_HEADER_HTML, unsafe_allow_html=True)

    st.sidebar.markdown(
        '<div class="acn-side-title">&#10095; SAC FP&amp;A Agent</div>'
        '<div class="acn-side-sub">FP&amp;A on SAP Analytics Cloud</div>',
        unsafe_allow_html=True)
    if CONFIG_ERROR:
        st.error(CONFIG_ERROR)
        st.stop()

    conn = "🟢 Connected" if is_authenticated() else "⚪ Not connected"
    st.sidebar.caption(conn)
    if is_authenticated():
        _mn = effective_model_name()
        st.sidebar.caption(f"📦 Model: {_mn}" if _mn else "📦 Model: none selected")

    # Persona / role-based view (presentation only — SAC enforces real security).
    role = st.sidebar.selectbox("👤 View as", list(ROLE_ACCESS.keys()), key="role")
    allowed = [p for p in ALL_PAGES if p in ROLE_ACCESS[role]]
    labels = [f"{ALL_PAGES[p][0]}  {p}" for p in allowed]
    choice = allowed[labels.index(st.sidebar.radio("Capability", labels))]

    if is_authenticated():
        st.sidebar.divider()
        if st.sidebar.button("🚪 Log out", type="primary", use_container_width=True):
            logout(); st.rerun()

    if choice != "Connect" and not is_authenticated():
        st.warning("Connect to SAC first (Connect tab)."); return

    # Header strip: persona badge + the active model (same on every tab).
    if is_authenticated():
        _mn = effective_model_name()
        c1, c2 = st.columns([1, 4])
        c1.markdown(f"<span class='acn-badge'>👤 {role}</span>",
                    unsafe_allow_html=True)
        c2.markdown(
            f"📦 **Active model:** {_mn}  ·  `{effective_model_id()}`" if _mn
            else "📦 **No model selected** — pick one on the **Models** tab.")
    else:
        st.markdown(f"<span class='acn-badge'>👤 {role}</span>",
                    unsafe_allow_html=True)

    ALL_PAGES[choice][1]()


if __name__ == "__main__":
    main()
