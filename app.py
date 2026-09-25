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

import sac_insights as INS
import sac_report_export as REP
import sac_playbook as PB
import sac_dim_config as DIMCFG

# Streamlit's in-browser "Rerun" re-executes this whole file, but a plain `import`
# is a no-op once a module is already in sys.modules — so edits to these local
# modules would otherwise stay INVISIBLE until the whole streamlit process is
# restarted (not just "Rerun"), which is a confusing trap. Force a fresh reload on
# every run so Rerun always reflects the latest code on disk, same as app.py itself.
import importlib
try:
    importlib.reload(INS)
    importlib.reload(REP)
    importlib.reload(PB)
    importlib.reload(DIMCFG)
except Exception:
    pass  # never let a reload hiccup break the app — fall back to what's already bound

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
# CAPABILITY — MASTER DATA UPDATE (Data Import Service: dimension members)
# ============================================================================
# SAC's Data Import Service addresses master data through PUBLIC DIMENSIONS
# (not under the model). We discover the publicDimensionID, then create a job.
def fetch_public_dimensions(c):
    """List public dimensions from the Data Import Service.
    Returns (dims, raw) with dims = [{'id','name'}]."""
    for path in ("/api/v1/dataimport/publicDimensions",
                 "/api/v1/dataimport/publicDimensions/"):
        r = c.get(path)
        if r.status_code == 200:
            data = _safe_json(r)
            items = (data.get("publicDimensions") or data.get("value")
                     or data.get("dimensions")
                     or (data if isinstance(data, list) else []))
            out = []
            for d in items:
                if isinstance(d, dict):
                    did = (d.get("publicDimensionID") or d.get("id")
                           or d.get("dimensionId") or d.get("dimensionID"))
                    nm = d.get("name") or d.get("description") or did
                    if did:
                        out.append({"id": str(did), "name": str(nm)})
            if out:
                return out, data
    return [], {}


def resolve_public_dimension(c, dim_name):
    """Map a dimension name/id to its publicDimensionID. Returns (pubDimId, dims)."""
    dims, _ = fetch_public_dimensions(c)
    dl = (dim_name or "").strip().lower()
    for d in dims:                                   # exact match on id or name
        if d["id"].lower() == dl or d["name"].lower() == dl:
            return d["id"], dims
    for d in dims:                                   # fuzzy contains
        if dl and (dl in d["id"].lower() or dl in d["name"].lower()
                   or d["name"].lower() in dl):
            return d["id"], dims
    return None, dims


_PUBDIM_JOB_PATHS = [
    "/api/v1/dataimport/publicDimensions/{pid}/masterData",
    "/api/v1/dataimport/publicDimensions/{pid}",
]   # CONFIRM endpoint


def create_public_dim_job(c, pub_dim_id):
    """Create a master-data import job for a public dimension.
    Returns (job_id, info)."""
    last = {}
    for tmpl in _PUBDIM_JOB_PATHS:
        path = tmpl.format(pid=pub_dim_id)
        r = c.post(path)
        raw = _safe_json(r)
        last = {"path": path, "http": r.status_code, "body": r.text[:600]}
        if r.status_code in (200, 201, 202):
            job_id = raw.get("jobID") or raw.get("jobId")          # CONFIRM key
            if job_id:
                return job_id, last
    return None, last


def probe_masterdata_api(c, dim_name):
    """Read-only diagnosis of the master-data write path: list public dimensions,
    resolve the id, fetch its import metadata, and try creating a (staging-only)
    job on each candidate path. Returns findings for display."""
    out = {}
    r = c.get("/api/v1/dataimport/publicDimensions")
    out["1_publicDimensions_HTTP"] = r.status_code
    out["1_publicDimensions_body"] = (r.text or "")[:1500]
    dims, _ = fetch_public_dimensions(c)
    out["2_parsed_dimensions"] = dims
    pid, _ = resolve_public_dimension(c, dim_name)
    out["3_resolved_publicDimensionID"] = pid
    if pid:
        rm = c.get(f"/api/v1/dataimport/publicDimensions/{pid}/metadata")
        out["4_metadata_HTTP"] = rm.status_code
        out["4_metadata_body"] = (rm.text or "")[:1500]
        attempts = []
        for tmpl in _PUBDIM_JOB_PATHS:
            path = tmpl.format(pid=pid)
            rr = c.post(path)
            attempts.append({"endpoint": path, "HTTP": rr.status_code,
                             "body": (rr.text or "")[:300]})
        out["5_create_job_attempts"] = attempts
    return out


def cap_master_data():
    st.subheader("Master Data Update")
    st.caption("Create or update the **members of one dimension** from a flat file "
               "(CSV) via the SAC Data Import Service — IDs, descriptions, "
               "hierarchies and properties. This does not write fact data.")
    mid = effective_model_id()
    if not need(mid):
        st.error("PREREQUISITE MISSING — choose a model on the **Models** tab "
                 "(or set SAC_MODEL_ID in .env)."); return

    # ---- choose the dimension ---------------------------------------------
    if st.button("📥 Load this model's dimensions"):
        c = client()
        try:
            cols, meta = fetch_model_columns(c, mid)
            st.session_state["md_dims"] = cols
            st.session_state["md_dims_raw"] = meta
        except Exception as e:
            st.session_state["md_dims"] = []
            st.session_state["md_dims_raw"] = {"error": str(e)}

    if st.session_state.get("md_dims") == []:
        st.warning("Couldn't read this model's dimensions automatically — you can "
                   "still type the dimension ID manually below.")
        with st.expander("Raw model metadata"):
            st.write(st.session_state.get("md_dims_raw"))

    dims = st.session_state.get("md_dims") or []
    MANUAL = "✏️  type the dimension ID manually"
    options = (dims + [MANUAL]) if dims else [MANUAL]
    pick = st.selectbox("Dimension to update", options, key="md_dim_pick")
    dim = (st.text_input("Dimension ID", key="md_dim_text").strip()
           if pick == MANUAL else pick)
    if not dim:
        st.info("Pick a dimension above (or 📥 load them), then upload your file.")
        return

    # ---- upload + preview --------------------------------------------------
    up = st.file_uploader("Upload master-data CSV", type=["csv"], key="md_file")
    if not up:
        st.info("File headers must match the dimension's member field IDs — at "
                "minimum the member **ID** column, plus optional **Description**, a "
                "parent/**hierarchy** column, and any property columns.")
        return

    df = pd.read_csv(up, dtype=str).fillna("")
    st.caption(f"{len(df):,} rows × {len(df.columns)} columns detected.")
    st.dataframe(df, use_container_width=True, height=380)

    cols = list(df.columns)
    id_col = st.selectbox("Which column holds the member ID?", cols, key="md_id_col")

    with st.expander("➕ Set a constant value on every row (optional, e.g. a hierarchy)"):
        cc_name = st.text_input("Column name", key="md_cc_name").strip()
        cc_val = st.text_input("Value for every row", key="md_cc_val")

    st.markdown("---")
    if st.button("Validate & Update Master Data", type="primary"):
        c = client()
        if id_col not in df.columns:
            st.error("Pick the member ID column first."); return
        send_df = df.copy()
        if cc_name:
            send_df[cc_name] = cc_val
        rows = send_df.to_dict(orient="records")
        prog = JobProgress(f"Updating master data for '{dim}' …")
        try:
            out = push_master_data(c, dim, rows, prog)
            if out["ok"]:
                st.success("Master data updated.")
            if out.get("detail") is not None:
                with st.expander("Details / invalid rows"):
                    st.write(out["detail"])
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
/* Dark-theme-PRIMARY palette (GN Productivity Hub / gn-playbook-ui companion
   prompt), applied as the base surface everywhere — not as isolated dark
   accents on an otherwise light page. Matches .streamlit/config.toml's
   [theme] block; this CSS only adds what config.toml can't express (the
   hero gradient/shine-sweep, shadow-to-glow, sidebar nav states, card
   borders). Font: Segoe UI/Arial — this is an internal-only tool; swap to
   the Accenture brand font stack (via the accenture-pptx skill) if this
   ever becomes client-facing. */
:root{
  --acn-purple:#A100FF; --acn-violet:#7500C0; --acn-magenta:#E6007E;
  --acn-bg:#1A0030; --acn-surface:#2D0050; --acn-surface-2:#3D006E;
  --acn-ink:#F1E9F8; --acn-muted:#B9AED0; --acn-accent-ink:#EBD9FF;
}
html, body, .stApp, [class*="css"] { font-family:"Segoe UI", Arial, sans-serif; }
.stApp{
  background:
    radial-gradient(1100px 480px at 100% -8%, rgba(230,0,126,0.16), transparent 60%),
    radial-gradient(900px 480px at -8% 0%, rgba(161,0,255,0.18), transparent 55%),
    var(--acn-bg);
}
[data-testid="stHeader"] { background:transparent; }
.block-container { padding-top:2rem; max-width:1300px; }
h1,h2,h3 { color:var(--acn-ink); font-weight:800; letter-spacing:-0.015em; }

/* gentle transitions on interactive elements */
button, a, [role="radiogroup"] label, [data-testid="stExpander"],
[data-testid="stMetric"], input, textarea, [data-baseweb="select"]>div {
  transition: all .18s cubic-bezier(.2,.7,.3,1) !important;
}

/* ---------- Hero (animated gradient + shine sweep) — unchanged; white text
   on a purple/magenta gradient already reads correctly on a dark page. ---------- */
.acn-hero{
  position:relative; overflow:hidden;
  background:linear-gradient(120deg,#A100FF 0%,#7500C0 45%,#E6007E 100%);
  background-size:200% 200%; animation:acnShift 12s ease infinite;
  border-radius:20px; padding:26px 32px; margin:0 0 14px 0;
  display:flex; align-items:center; gap:20px;
  box-shadow:0 18px 40px rgba(161,0,255,0.35);
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

/* ---------- Sidebar — aligned to the SAME dark-primary surface (a deeper
   anchor, --acn-bg-ish, so it reads as part of one continuous dark surface
   rather than a separately-styled dark accent on a light page). ---------- */
[data-testid="stSidebar"]{ background:linear-gradient(180deg,#22003D 0%,#150026 100%); }
[data-testid="stSidebar"] *{ color:var(--acn-ink) !important; }
.acn-side-title{ font-size:20px; font-weight:900; color:#fff !important; line-height:1.1; }
.acn-side-sub{ font-size:11.5px; color:var(--acn-muted) !important; margin:2px 0 8px 0; }
[data-testid="stSidebar"] [role="radiogroup"] label{
  padding:8px 12px; border-radius:11px; margin-bottom:4px;
}
[data-testid="stSidebar"] [role="radiogroup"] label:hover{
  background:rgba(161,0,255,0.28); transform:translateX(3px);
}
[data-testid="stSidebar"] [role="radiogroup"] label:has(input:checked){
  background:linear-gradient(90deg,rgba(230,0,126,.32),rgba(161,0,255,.30));
  box-shadow:inset 3px 0 0 #E6007E;
}

/* ---------- Buttons ---------- */
.stButton>button{
  border-radius:11px; border:1px solid var(--acn-surface-2); font-weight:600;
  background:var(--acn-surface); color:var(--acn-ink);
}
.stButton>button:hover{
  border-color:var(--acn-purple); color:var(--acn-accent-ink);
  transform:translateY(-1px); box-shadow:0 6px 16px rgba(161,0,255,.35);
}
.stButton>button[kind="primary"], [data-testid="stLinkButton"] a, .acn-login-btn{
  background:linear-gradient(120deg,#A100FF,#7500C0) !important; color:#fff !important;
  border:0 !important; border-radius:11px !important; font-weight:700 !important;
  padding:.55rem 1.2rem !important; box-shadow:0 8px 20px rgba(161,0,255,0.40) !important;
  text-decoration:none;
}
.stButton>button[kind="primary"]:hover, [data-testid="stLinkButton"] a:hover, .acn-login-btn:hover{
  transform:translateY(-2px); filter:brightness(1.1);
  box-shadow:0 12px 26px rgba(161,0,255,0.55) !important;
}
.acn-login-btn{ display:inline-block; padding:.6rem 1.4rem; }

/* ---------- Inputs (dark surface + focus ring) ---------- */
.stTextInput input, .stNumberInput input, .stTextArea textarea,
[data-baseweb="select"]>div { border-radius:11px !important; }
.stTextInput input:focus, .stNumberInput input:focus, .stTextArea textarea:focus{
  box-shadow:0 0 0 3px rgba(161,0,255,.30) !important; border-color:var(--acn-purple) !important;
}

/* ---------- Base text colour (app-wide default; safe — every rule that must
   stay a DIFFERENT colour, e.g. sidebar/badges/primary buttons, already uses
   !important or a higher-specificity selector and still wins over this).
   Without this, plain text/labels/captions had NO explicit colour and simply
   inherited whatever Streamlit's own light/dark auto-detection produced —
   which is exactly what caused invisible white-on-white / dark-on-dark text
   whenever it disagreed with a background this CSS forces elsewhere. ---------- */
.stApp *{ color:var(--acn-ink); }

/* ---------- Cards / alerts / tables / metrics — dark surface, purple-GLOW
   shadows (rgba(161,0,255,…)) instead of the old black-based shadows
   (rgba(0,0,0,…)-style, which read as dirty/muddy on a dark background). ---------- */
.stAlert{ border-radius:14px; border:1px solid var(--acn-surface-2); background:var(--acn-surface); }
[data-testid="stExpander"]{ border-radius:14px; border:1px solid var(--acn-surface-2); background:var(--acn-surface); color:var(--acn-ink); }
[data-testid="stExpander"]:hover{ box-shadow:0 8px 22px rgba(161,0,255,.28); }
[data-testid="stDataFrame"], [data-testid="stTable"]{
  border-radius:12px; overflow:hidden; box-shadow:0 4px 16px rgba(161,0,255,.22);
}
[data-testid="stMetric"]{
  background:var(--acn-surface); color:var(--acn-ink); border:1px solid var(--acn-surface-2); border-radius:16px; padding:14px 16px;
  box-shadow:0 6px 18px rgba(161,0,255,.20);
}
[data-testid="stMetric"]:hover{ transform:translateY(-2px); box-shadow:0 12px 26px rgba(161,0,255,.38); }

/* role / persona badge */
.acn-badge{
  display:inline-block; padding:5px 14px; border-radius:999px; font-weight:700;
  font-size:12.5px; color:#fff !important; background:linear-gradient(120deg,#A100FF,#E6007E);
  box-shadow:0 4px 12px rgba(161,0,255,.40);
}

/* ---------- Chart images — matplotlib PNGs (live view + PPTX/PDF exports)
   stay LIGHT/white-background on purpose: board/CEO decks are conventionally
   light for printing/projection, and re-theming charts dark would make
   exports inconsistent with the printed deliverable. A rounded, glow-bordered
   "light card" frame makes this read as an intentional panel on the dark
   surface, not a mismatched glitch. ---------- */
[data-testid="stImage"] img{
  border-radius:14px; background:#fff; padding:10px;
  border:1px solid var(--acn-surface-2); box-shadow:0 8px 24px rgba(161,0,255,.28);
}

/* inline `code` chips (e.g. model-ID badges) — Streamlit's own default can
   stay a fixed light-gray regardless of theme; pin it to the dark surface. */
.stMarkdown code, [data-testid="stCaptionContainer"] code{
  background:var(--acn-surface-2) !important; color:var(--acn-accent-ink) !important;
  border-radius:6px; padding:.15em .4em;
}

/* custom scrollbars */
::-webkit-scrollbar{ width:10px; height:10px; }
::-webkit-scrollbar-thumb{ background:linear-gradient(#A100FF,#7500C0); border-radius:10px; }
::-webkit-scrollbar-track{ background:var(--acn-bg); }
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


# ============================================================================
# CAPABILITY — DIMENSION MEMBERS (read existing members via Data Export)
# ============================================================================
def _master_data_bases(provider, entity):
    """Candidate master-data (per-dimension) export paths."""
    return [
        f"/api/v1/dataexport/providers/sac/{provider}/{entity}",
        f"/api/v1/dataexport/providers/{provider}/{entity}",
    ]   # CONFIRM endpoint


def fetch_export_entitysets(c, provider):
    """List the OData entity sets (dimensions + FactData) the provider exposes,
    read from the Data Export service document."""
    for path in (f"/api/v1/dataexport/providers/sac/{provider}",
                 f"/api/v1/dataexport/providers/{provider}"):       # CONFIRM
        r = c.get(path)
        if r.status_code == 200:
            data = _safe_json(r)
            if isinstance(data, dict) and isinstance(data.get("value"), list):
                names = [it.get("name") or it.get("url")
                         for it in data["value"] if isinstance(it, dict)]
                names = [n for n in names if n]
                if names:
                    return names, path
    return [], None


def read_master_data(c, provider, entity, max_rows=100000, query=""):
    """Read a dimension's members via the Data Export API (OData + nextLink
    paging), mirroring read_fact_data. Returns (rows, fail_resp)."""
    last = None
    for base in _master_data_bases(provider, entity):
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


def _entitysets_from_metadata(xml):
    """Pull EntitySet names out of an OData EDMX ($metadata) document."""
    import re
    return re.findall(r'EntitySet\s+Name="([^"]+)"', xml or "")


def probe_export_structure(c, provider):
    """Discover the real Data Export entity-set names (service document +
    $metadata) so a dimension can be mapped to its export entity set.
    Returns (entityset_names, probe_rows)."""
    ents, rows = [], []
    for path in (f"/api/v1/dataexport/providers/sac/{provider}",
                 f"/api/v1/dataexport/providers/sac/{provider}/$metadata",
                 f"/api/v1/dataexport/providers/{provider}",
                 "/api/v1/dataexport/administration/providers"):
        try:
            r = c.get(path)
            txt = r.text or ""
            rows.append({"endpoint": path, "HTTP": r.status_code, "snippet": txt[:200]})
            if r.status_code == 200:
                data = _safe_json(r)
                if isinstance(data, dict) and isinstance(data.get("value"), list):
                    for it in data["value"]:
                        if isinstance(it, dict):
                            nm = it.get("name") or it.get("url")
                            if nm:
                                ents.append(str(nm))
                if "<EntitySet" in txt:
                    ents += _entitysets_from_metadata(txt)
        except Exception as e:
            rows.append({"endpoint": path, "HTTP": "ERR", "snippet": str(e)[:200]})
    seen = set()
    ents = [e for e in ents if not (e in seen or seen.add(e))]
    return ents, rows


def _order_member_cols(cols, dim_name=""):
    """Order columns as: member ID first, Description second, then the rest."""
    cols = list(cols)
    low = {c: str(c).strip().lower() for c in cols}
    dn = (dim_name or "").strip().lower()
    id_col = (next((c for c in cols if low[c] == "id"), None)
              or (next((c for c in cols if low[c] == dn), None) if dn else None)
              or next((c for c in cols if low[c].endswith("id")
                       or "memberid" in low[c].replace("_", "")), None)
              or (cols[0] if cols else None))
    desc_col = (next((c for c in cols if low[c] == "description"), None)
                or next((c for c in cols if "description" in low[c]), None)
                or next((c for c in cols if "desc" in low[c]), None))
    ordered = list(dict.fromkeys([c for c in (id_col, desc_col) if c is not None]))
    for c in cols:
        if c not in ordered:
            ordered.append(c)
    return ordered


def push_master_data(c, dim_name, rows, prog):
    """Update master data for a dimension via the DIS public-dimensions API:
    resolve public dimension -> create job -> upload -> validate -> run -> poll.
    Emits progress via `prog` (which it finishes). Returns {'ok', 'detail'}."""
    prog.step("Resolving the public dimension…", emoji="🔎")
    pub_id, dims = resolve_public_dimension(c, dim_name)
    prog.log(f"public dimensions available: {[d['id'] for d in dims][:30]}")
    if not pub_id:
        prog.finish(False, f"No public dimension matches '{dim_name}'.")
        return {"ok": False, "detail": {"reason": "no matching public dimension",
                                        "publicDimensions": dims}}
    prog.log(f"publicDimensionID: {pub_id}")

    prog.step("Creating master-data job…", emoji="🆕")
    job_id, info = create_public_dim_job(c, pub_id)
    prog.log(f"create job → {info}")
    if not job_id:
        prog.finish(False, "Couldn't create a master-data job (see log).")
        return {"ok": False, "detail": info}
    prog.log(f"Job ID: {job_id}")

    prog.step(f"Uploading {len(rows):,} member(s)…", emoji="⬆️")
    r_up = c.post(f"/api/v1/dataimport/jobs/{job_id}", json={"Data": rows})   # CONFIRM wrapper
    prog.log(f"upload → HTTP {r_up.status_code}: {r_up.text[:600]}")
    if r_up.status_code not in (200, 201, 202):
        prog.finish(False, f"Upload rejected (HTTP {r_up.status_code}).")
        return {"ok": False, "detail": _safe_json(r_up) or r_up.text[:600]}

    prog.step("Validating…", emoji="🔍")
    r_val = c.post(f"/api/v1/dataimport/jobs/{job_id}/validate")
    prog.log(f"validate → HTTP {r_val.status_code}: {r_val.text[:300]}")

    prog.step("Submitting run…", emoji="🚀")
    r_run = c.post(f"/api/v1/dataimport/jobs/{job_id}/run")
    prog.log(f"run → HTTP {r_run.status_code}: {r_run.text[:300]}")
    if r_run.status_code not in (200, 201, 202):
        prog.finish(False, f"SAP did not start the update (HTTP {r_run.status_code}).")
        return {"ok": False, "detail": r_run.text[:600]}

    res = poll_job(c, f"/api/v1/dataimport/jobs/{job_id}/status", progress=prog)
    ok = res["status"] == "done"
    detail = res.get("detail")
    if not ok:
        rr = c.get(f"/api/v1/dataimport/jobs/{job_id}/invalidRows")
        if rr.status_code == 200:
            detail = {"status": res["status"], "invalidRows": _safe_json(rr)}
    prog.finish(ok, "Master data updated successfully" if ok
                else f"Update {res['status']} — see log")
    return {"ok": ok, "detail": detail}


def cap_dim_members():
    st.subheader("Dimension Members")
    st.caption("Pick a dimension to view all of its members and properties (read-only).")
    mid = effective_model_id()
    if not need(mid):
        st.error("PREREQUISITE MISSING — choose a model on the **Models** tab "
                 "(or set SAC_MODEL_ID in .env)."); return

    # 1) The dimensions the model uses (from the model's metadata). Cached per
    #    model, auto-loaded the first time the tab is opened, and refreshable.
    if st.session_state.get("dm_dims_for") != mid or st.button("🔄 Reload dimensions"):
        with st.spinner("Reading the model's dimensions…"):
            c = client()
            try:
                cols, meta = fetch_model_columns(c, mid)
            except Exception as e:
                cols, meta = [], {"error": str(e)}
        st.session_state["dm_dims"] = cols
        st.session_state["dm_dims_meta"] = meta
        st.session_state["dm_dims_for"] = mid

    dims = st.session_state.get("dm_dims") or []
    if dims:
        st.markdown(f"**{len(dims)} dimension(s) in this model:** " + ", ".join(dims))
    else:
        st.warning("Couldn't auto-detect this model's dimensions — you can still type a "
                   "dimension / entity-set name below.")
        with st.expander("Raw model metadata (share this if the list looks wrong)"):
            st.write(st.session_state.get("dm_dims_meta"))

    # 2) Dropdown to choose a dimension (with a manual fallback).
    MANUAL = "✏️  type a dimension / entity-set name"
    options = (dims + [MANUAL]) if dims else [MANUAL]
    pick = st.selectbox("Select a dimension", options, key="dm_pick")
    dim = (st.text_input("Dimension / entity-set name", key="dm_manual").strip()
           if pick == MANUAL else pick)

    provider = (effective_model_id() or CFG.export_provider_id or "").strip()

    # 3) Read & show all members + properties for the chosen dimension. If the
    #    direct read fails, discover the real export entity-set names and retry
    #    against a matching one (the master-data name can differ from the id).
    if st.button("📥 View members & properties", type="primary"):
        if not dim:
            st.error("Select or type a dimension first.")
        elif not provider:
            st.error("No export provider — set SAC_EXPORT_PROVIDER_ID or select a model.")
        else:
            c = client()
            ents, probe = [], []
            with st.spinner(f"Reading members of '{dim}'…"):
                rows, fail = read_master_data(c, provider, dim)
                used = dim
                if rows is None:
                    ents, probe = probe_export_structure(c, provider)
                    match = next((e for e in ents
                                  if e.lower() == dim.lower()
                                  or dim.lower() in e.lower()
                                  or e.lower() in dim.lower()), None)
                    if match:
                        rows2, _ = read_master_data(c, provider, match)
                        if rows2 is not None:
                            rows, used = rows2, match
            if rows is not None:
                st.session_state["dm_result"] = {"ok": True, "dim": used,
                                                 "sel": dim, "rows": rows}
            else:
                st.session_state["dm_result"] = {
                    "ok": False, "dim": dim,
                    "http": getattr(fail, "status_code", "?"),
                    "body": (fail.text[:800] if fail is not None else ""),
                    "entitysets": ents, "probe": probe}

    res = st.session_state.get("dm_result")
    if not res:
        return
    if res["ok"]:
        rows = res["rows"]
        if not rows:
            st.info(f"'{res['dim']}' returned 0 members.")
        else:
            df = pd.DataFrame(rows)
            df = df[_order_member_cols(df.columns, res["dim"])]
            id_col = df.columns[0]
            st.success(f"**{len(df):,} member(s)** in '{res['dim']}' — "
                       f"{len(df.columns)} propert(ies) per member.")
            st.download_button("⬇️ Download as CSV", df.to_csv(index=False).encode("utf-8"),
                               file_name=f"{res['dim']}_members.csv", mime="text/csv")

            st.markdown("### ✏️ Edit & update members")
            st.caption(f"Edit the **Description** and property cells below (the member "
                       f"**{id_col}** is locked), then push the changes back to SAC. "
                       "Only **changed rows** are sent, via the Data Import master-data job.")
            edited = st.data_editor(df, use_container_width=True, height=460,
                                    disabled=[id_col], num_rows="fixed", key="dm_editor")
            if st.button("💾 Update master data in SAC", type="primary", key="dm_update_btn"):
                a = df.astype(object).where(pd.notna(df), "")
                b = edited.astype(object).where(pd.notna(edited), "")
                changed = edited[(a.values != b.values).any(axis=1)]
                if len(changed) == 0:
                    st.warning("No changes detected — edit a Description or property cell first.")
                else:
                    sel = res.get("sel") or res["dim"]
                    prog = JobProgress(f"Updating {len(changed):,} member(s) of '{sel}' …")
                    try:
                        out = push_master_data(
                            client(), sel, changed.to_dict(orient="records"), prog)
                        if out["ok"]:
                            st.success("Update complete — click **📥 View members & "
                                       "properties** again to reload the updated data.")
                        if out.get("detail") is not None:
                            with st.expander("Final server response"):
                                st.write(out["detail"])
                    except Exception as e:
                        prog.finish(False, "Unexpected error — see log")
                        st.exception(e)

            with st.expander("🔧 Diagnose the update API (read-only) — open if updates fail"):
                st.caption("Probes the Data Import master-data endpoints and shows the raw "
                           "responses, so we can pin the exact path / fields your tenant "
                           "uses. (Create-job attempts only allocate a staging area — they "
                           "don't import anything.)")
                if st.button("Run update-API diagnostics", key="dm_probe_btn"):
                    with st.spinner("Probing the Data Import master-data endpoints…"):
                        st.session_state["dm_probe"] = probe_masterdata_api(
                            client(), res.get("sel") or res.get("dim"))
                if st.session_state.get("dm_probe"):
                    st.json(st.session_state["dm_probe"])
    else:
        st.error(f"Couldn't read '{res['dim']}' (HTTP {res['http']}). SAC's Data Export "
                 "master-data entity-set name likely differs from the dimension id, or "
                 "this model isn't master-data export-enabled.")
        if res.get("entitysets"):
            st.markdown("**Export entity sets your tenant exposes** — pick the matching "
                        "one via the ✏️ option above and paste it:")
            st.write(res["entitysets"])
        if res.get("probe"):
            st.markdown("**What each discovery endpoint returned:**")
            st.dataframe(pd.DataFrame(res["probe"]), use_container_width=True, hide_index=True)
        with st.expander("Raw response from the read attempt"):
            st.code(res.get("body") or "(empty)")
        st.caption("Send me this and I'll wire the exact master-data endpoint your tenant uses.")


# ============================================================================
# CAPABILITY — STORIES (list tenant stories + the models they use)
# ============================================================================
def fetch_stories(c):
    """List stories via the SAC Tenant API (?include=models). Returns
    (items, path_used, fail_resp)."""
    last = None
    for path in ("/api/v1/stories?include=models",
                 "/api/v1/stories?$format=json&include=models",
                 "/api/v1/Resources?$format=json&$expand=models"):   # CONFIRM
        r = c.get(path)
        last = r
        if r.status_code == 200:
            data = _safe_json(r)
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = (data.get("stories") or data.get("value")
                         or data.get("Resources") or data.get("resources") or [])
            else:
                items = []
            return items, path, None
    return None, None, last


def _story_matches(story, mid, mname):
    hay = " ".join(f"{m.get('id','')} {m.get('description','')}"
                   for m in (story.get("models") or [])).lower()
    return (bool(mid) and mid.lower() in hay) or (bool(mname) and mname.lower() in hay)


def _filtered_stories(items, mid, mname, show_all):
    """Return (stories_to_show, total, fell_back_to_all) — scoped to the selected
    model unless show_all; falls back to all only if nothing matched."""
    stories = [s for s in items if isinstance(s, dict)]
    total = len(stories)
    if show_all:
        return stories, total, False
    matched = [s for s in stories if _story_matches(s, mid, mname)]
    if not matched and total:
        return stories, total, True
    return matched, total, False


def _story_url(s):
    u = s.get("openURL") or ""
    return (CFG.base_url + u) if u.startswith("/") else u


def _render_story_list(shown_stories, total, fellback, show_all):
    if fellback:
        st.warning(f"None of the {total} tenant story(ies) matched this model by id/name "
                   "(the model-id format inside stories can differ). Showing all — check "
                   "the **Model(s)** column, or tick **Show all** above.")
    if not shown_stories:
        st.info("No stories for the selected model. Tick **Show all** above to list every "
                "story on the tenant."); return

    rows = []
    for s in shown_stories:
        mods = s.get("models") or []
        rows.append({
            "Story": s.get("name") or s.get("id") or "",
            "Model(s)": ", ".join(str(m.get("description") or m.get("id")) for m in mods),
            "Description": s.get("description", ""),
            "Changed": s.get("changed", ""),
            "Changed by": s.get("changedBy", ""),
            "Story id": s.get("id", ""),
            "Link": _story_url(s),
        })
    df = pd.DataFrame(rows)
    scope = "all tenant stories" if show_all else "using this model"
    st.success(f"**{len(rows)}** of {total} story(ies) shown ({scope}).")
    st.dataframe(df, use_container_width=True, height=420,
                 column_config={"Link": st.column_config.LinkColumn("Open")})

    sel_map = {f"{s.get('name') or s.get('id')}  ·  {s.get('id','')}": s
               for s in shown_stories}
    label = st.selectbox("Select / open a story (also drives the Explore tab)",
                         list(sel_map.keys()), key="story_open_pick")
    sel = sel_map[label]
    st.session_state["story_sel_id"] = sel.get("id")
    url = _story_url(sel)
    c1, c2 = st.columns(2)
    with c1:
        if url:
            st.link_button("🔗 Open in a new browser tab", url, use_container_width=True)
    with c2:
        embed = st.toggle("📺 Open inside the agent (embed below)", key="story_embed",
                          help="Renders the story in a panel below. Needs the SAC tenant "
                               "to allow embedding from this app's origin.")
    if embed and url:
        import streamlit.components.v1 as components
        embed_url = url + ("&" if "?" in url else "?") + "mode=embed"
        st.caption("If the panel below is blank, the SAC tenant is blocking embedding — an "
                   "admin must add this app's origin (http://localhost:8502) under "
                   "**System → Administration → App Integration → Trusted Origins** "
                   "(content-security-policy frame-ancestors).")
        components.iframe(embed_url, height=800, scrolling=True)
    st.download_button("⬇️ Download as CSV", df.to_csv(index=False).encode("utf-8"),
                       file_name="stories.csv", mime="text/csv")


def _render_story_explorer(shown_stories):
    if not shown_stories:
        st.info("No stories for the selected model to explore. In the **📚 Story list** "
                "tab, select a story (or tick **Show all**)."); return
    sel_id = st.session_state.get("story_sel_id")
    s = next((x for x in shown_stories if x.get("id") == sel_id), shown_stories[0])
    st.caption("Exploring the story selected in the **📚 Story list** tab — change the "
               "selection there to explore a different story.")
    mods = s.get("models") or []

    # ---- Brief introduction -----------------------------------------------
    st.markdown(f"### 📖 {s.get('name', '(unnamed story)')}")
    if s.get("description"):
        st.write(s["description"])
    bits = []
    if s.get("createdBy"):
        bits.append(f"created by **{s['createdBy']}**")
    if s.get("changedBy"):
        bits.append(f"last changed by **{s['changedBy']}**")
    if s.get("changed"):
        bits.append(f"on {s['changed']}")
    if bits:
        st.caption(" · ".join(bits))
    if mods:
        st.markdown("**Models used:** " + ", ".join(
            f"`{m.get('description') or m.get('id')}`"
            + (" _(planning)_" if m.get("isPlanning") else "") for m in mods))
    url = s.get("openURL") or ""
    if url:
        st.link_button("🔗 Open the full story in a new tab",
                       (CFG.base_url + url) if url.startswith("/") else url)

    st.info("SAC's API doesn't expose a story's exact widgets/filters, so the tools below "
            "reconstruct the story's **data** from its underlying model — build tables, "
            "filter, and run data actions without opening the story.")

    # Data source: default to THIS story's model(s); allow the active model or manual.
    cand = []
    for m in mods:
        for v in (m.get("id"), m.get("description")):
            if v and str(v) not in cand:
                cand.append(str(v))
    _act = effective_model_id()
    if _act and _act not in cand:
        cand.append(_act)
    MANUAL = "✏️  type a provider id"
    p_options = (cand + [MANUAL]) if cand else [MANUAL]
    psel = st.selectbox("Data source for this story (model / export provider)",
                        p_options, key="explore_prov_sel",
                        help="Defaults to this story's model. If the read fails, the "
                             "story's model id can differ from the export-provider id — "
                             "pick another candidate, or type it.")
    provider = (st.text_input("Provider id", key="explore_provider_manual").strip()
                if psel == MANUAL else psel)

    # ---- Read the story data ----------------------------------------------
    st.markdown("#### 📄 Story data (tables & filters)")
    if st.button("📄 Read story data", type="primary", key="explore_load"):
        if not provider:
            st.error("Pick or type a provider id.")
        else:
            c = client()
            with st.spinner("Reading the story's model data via Data Export…"):
                erows, fail = read_fact_data(c, provider)
            if erows is None:
                provs, ppath = fetch_export_providers(c)
                st.session_state["explore_rows"] = None
                st.session_state["explore_fail"] = {
                    "http": getattr(fail, "status_code", "?"),
                    "body": fail.text[:600] if fail is not None else "",
                    "providers": provs, "providers_path": ppath}
            else:
                st.session_state["explore_rows"] = erows
                st.session_state["explore_fail"] = None
    erows = st.session_state.get("explore_rows")
    efail = st.session_state.get("explore_fail")
    if efail:
        st.error(f"Couldn't read data (HTTP {efail['http']}). The story's model id may "
                 "differ from the export-provider id, or the model isn't export-enabled.")
        if efail.get("providers") is not None:
            st.caption(f"Valid export providers (from `{efail.get('providers_path')}`) — "
                       "pick one in the data-source box above:")
            st.write(efail["providers"])
        with st.expander("Raw response"):
            st.code(efail.get("body") or "(empty)")
    elif erows is not None:
        if not erows:
            st.info("The model returned 0 rows.")
        else:
            df = pd.DataFrame(erows)
            text_cols = [col for col in df.columns if not pd.api.types.is_numeric_dtype(df[col])]
            num_cols = [col for col in df.columns if pd.api.types.is_numeric_dtype(df[col])]
            fdf = df
            fcols = st.multiselect("Filter by dimension(s)", text_cols, key="explore_fcols")
            for fc in fcols:
                vals = sorted(df[fc].astype(str).unique())[:5000]
                chosen = st.multiselect(f"Values for {fc}", vals, key=f"explore_fv_{fc}")
                if chosen:
                    fdf = fdf[fdf[fc].astype(str).isin(chosen)]
            gb = st.multiselect("Group by (rows)", text_cols, key="explore_gb")
            meas = st.multiselect("Measures (sum)", num_cols,
                                  default=num_cols[:1], key="explore_meas")
            if gb and meas:
                out = fdf.groupby(gb)[meas].sum().reset_index()
                st.dataframe(out, use_container_width=True, height=380)
            else:
                out = fdf.head(500)
                st.caption(f"{len(fdf):,} rows after filters (showing up to 500). Pick "
                           "**Group by** + **Measures** to pivot into a table.")
                st.dataframe(out, use_container_width=True, height=380)
            st.download_button("⬇️ Download table", out.to_csv(index=False).encode("utf-8"),
                               file_name="story_table.csv", mime="text/csv", key="explore_dl")
    else:
        st.caption("Click **Load story data** to build tables and apply filters.")

    # ---- Data actions -----------------------------------------------------
    st.markdown("#### ⚙️ Data actions")
    st.caption("Data actions run as SAC Multi Actions. Load those exposed to the API, "
               "then run one (parameters use defaults).")
    if st.button("Load data actions", key="explore_da_load"):
        c = client()
        acts, _ = fetch_multi_actions(c, effective_model_id())
        st.session_state["explore_acts"] = acts
    acts = st.session_state.get("explore_acts")
    if acts:
        amap = {f"{a['name']}  ·  {a['id']}": a["id"] for a in acts}
        apick = st.selectbox("Data action", list(amap.keys()), key="explore_da_pick")
        if st.button("🚀 Run data action", type="primary", key="explore_da_run"):
            c = client()
            prog = JobProgress(f"Running '{apick}'…")
            try:
                res = run_multi_action(c, amap[apick], [], progress=prog)
                ok = res.get("status") == "done"
                prog.finish(ok, "Data action completed" if ok
                            else f"{res.get('status')} — see log")
                with st.expander("Result"):
                    st.write(res.get("detail"))
            except Exception as e:
                prog.finish(False, "Error — see log"); st.exception(e)
    elif acts == []:
        st.info("No multi-actions were exposed to the API for this model.")
    else:
        st.caption("Click **Load data actions** to list them.")


def cap_stories():
    st.subheader("Stories")
    st.caption("List the tenant's SAC stories and the models they use, and explore a "
               "story's data without opening it.")
    mid = (effective_model_id() or "").strip()
    mname = (effective_model_name() or "").strip()

    top1, top2 = st.columns([1, 2])
    with top1:
        do_fetch = st.button("📥 Fetch stories", type="primary")
    with top2:
        show_all = st.checkbox("Show all stories on the tenant (not just this model)",
                               key="story_show_all")
    if do_fetch:
        c = client()
        with st.spinner("Fetching stories…"):
            items, path, fail = fetch_stories(c)
        st.session_state["story_items"] = items
        st.session_state["story_path"] = path
        st.session_state["story_fail"] = (None if items is not None else
            {"http": getattr(fail, "status_code", "?"),
             "body": (fail.text[:800] if fail is not None else "")})

    items = st.session_state.get("story_items")
    fail = st.session_state.get("story_fail")
    if items is None and fail is None:
        st.info("Click **Fetch stories** to load them."); return
    if items is None:
        st.error(f"Couldn't list stories (HTTP {fail['http']}). The most common cause is "
                 "that the OAuth client is missing the **\"Story Listing\"** access type — "
                 "it must be enabled on the client in SAC -> App Integration. Also confirm "
                 "your signed-in user is allowed to see stories.")
        with st.expander("Raw response"):
            st.code(fail.get("body") or "(empty)")
        return

    shown_stories, total, fellback = _filtered_stories(items, mid, mname, show_all)
    tab_list, tab_explore = st.tabs(["📚 Story list", "🔎 Explore a story"])
    with tab_list:
        _render_story_list(shown_stories, total, fellback, show_all)
    with tab_explore:
        _render_story_explorer(shown_stories)


# ============================================================================
# CAPABILITY — INSIGHTS & REPORTING (rules-first management-reporting builder)
#
# Architecture ("Rules before AI" — see sac_insights.py docstring):
#   * The user builds one or more TABLES: pick row/column dimensions, measures
#     + aggregation type, an optional Budget/Forecast/Prior-Year comparison,
#     and a presentation style. Every number, every Favourable/Unfavourable
#     call, and the chart type are then computed/decided by fixed rules.
#   * AI (Gemini) is used in exactly one place per table — the optional
#     "✨ Get Insights" button — and only ever sees that table's own visible
#     values. It observes; it never computes, benchmarks, or recommends.
#   * Any built table can be exported, individually or together, to PPTX/PDF.
# ============================================================================
def _ins_new_table(df: pd.DataFrame, dims: list[str], tid: int) -> dict:
    time_guess = INS.guess_time_dimension(df, dims) if dims else None
    row_guess = [time_guess] if time_guess else ([dims[0]] if dims else [])
    return {
        # "title": "" means "not customized yet" — cap_insights() resolves this to
        # a position-based "Table N" label every render, so tables always number
        # 1, 2, 3... by their CURRENT position even after earlier ones are removed
        # (the internal "id" below is a separate, permanently-unique counter used
        # only for widget keys / session-state dict keys, never shown to the user).
        "id": tid, "title": "", "row_dims": row_guess, "col_dims": [],
        "time_dim": time_guess, "measure_cols": [], "agg": {},
        "calc_on": False, "calc_n": 2, "calc_terms": [None, None], "calc_ops": ["÷"],
        "calc_label": "Ratio (%)", "calc_pct": True,
        "reference_kind": "None", "reference_version": None, "measure_type": "cost",
        "presentation": INS.PRESENTATIONS[0], "filters": {},
    }


# Keys added to the table-spec dict AFTER _ins_new_table() first shipped (the
# calc_* rename from ratio_on/ratio_num/... being the most recent). A spec
# object already sitting in st.session_state from before that change is
# missing these — Streamlit's own file-watcher can hot-reload a NEWER app.py
# into an OLDER, still-running session whose session_state was never cleared,
# so "the user restarted" doesn't reliably guarantee every spec dict is
# current. Calling this before any spec["calc_..."] read makes an old dict
# self-heal instead of KeyError-ing (verified: this exact crash happened live
# once, "KeyError: 'calc_on'", on a table created before this key existed).
_INS_SPEC_KEY_DEFAULTS = {
    "calc_on": False, "calc_n": 2, "calc_terms": [None, None], "calc_ops": ["÷"],
    "calc_label": "Ratio (%)", "calc_pct": True,
}


def _ins_ensure_spec_defaults(spec: dict) -> None:
    for k, v in _INS_SPEC_KEY_DEFAULTS.items():
        spec.setdefault(k, v)


def _ins_try_build(df: pd.DataFrame, spec: dict, *, version_col: str, actual_version: str,
                   title: str) -> bool:
    """Validate + build one table spec. Shows st.error and returns False on any
    failure; on success writes ins_built[tid] and clears any stale observations/
    export cache for it. Never raises to the caller."""
    _ins_ensure_spec_defaults(spec)
    tid = spec["id"]
    measures, bad = [], False
    for m in spec["measure_cols"]:
        agg = spec["agg"].get(m, INS.CHOOSE)
        if agg == INS.CHOOSE:
            st.error(f"Choose an aggregation type for **{m}**."); bad = True
        else:
            measures.append({"kind": "agg", "col": m, "agg": agg})
    if spec["calc_on"]:
        terms = spec["calc_terms"]
        if len(terms) < 2 or any(t is None for t in terms):
            st.error("Choose a measure for every term in the calculated measure."); bad = True
        else:
            measures.append({"kind": "calc", "terms": terms, "ops": spec["calc_ops"],
                             "label": spec["calc_label"] or "Calculated measure",
                             "as_pct": spec["calc_pct"]})
    if not measures and not bad:
        st.error("Add at least one measure."); bad = True
    if bad:
        return False
    build_spec = dict(spec); build_spec["measures"] = measures; build_spec["title"] = title
    try:
        result = INS.build_table(df, build_spec, version_col=version_col, actual_version=actual_version)
    except Exception as e:
        result = {"error": f"Unexpected error building this table: {e}"}
    if "error" in result:
        st.error(result["error"]); return False
    st.session_state["ins_built"][tid] = result
    st.session_state["ins_obs"].pop(tid, None)
    st.session_state.pop("ins_report", None)
    return True


def _ins_dim_rows(dim: str, *, provider: str | None) -> list[dict]:
    """Raw master-data member rows for one dimension — the SAME read (with the
    SAME fuzzy entity-set-name fallback) the Dimension Members tab uses:
    fact-data column names (e.g. 'GL_Account') don't always match the export
    API's real entity-set name, so a failed direct read triggers
    probe_export_structure() + a fuzzy match, exactly like cap_dim_members().
    Cached per (provider, dim) so it's fetched AT MOST ONCE per session even
    though both the description lookup and the hierarchy lookup below both
    need it; on ANY failure (no live connection, endpoint not available,
    dimension not found) caches and returns [] — never a hard dependency."""
    if not provider:
        return []
    cache = st.session_state.setdefault("ins_dim_rows_cache", {})
    cache_key = f"{provider}::{dim}"
    if cache_key not in cache:
        rows = None
        try:
            c = client()
            rows, _fail = read_master_data(c, provider, dim)
            if rows is None:
                ents, _probe = probe_export_structure(c, provider)
                match = next((e for e in ents
                              if e.lower() == dim.lower()
                              or dim.lower() in e.lower()
                              or e.lower() in dim.lower()), None)
                if match:
                    rows, _fail = read_master_data(c, provider, match)
        except Exception:
            rows = None
        cache[cache_key] = rows or []
    return cache[cache_key]


def _ins_dim_descriptions(dim: str, *, provider: str | None) -> dict[str, str]:
    """Best-effort member id -> description lookup for one dimension.
    FactData rows (what the Filters options come from) only carry the raw
    member id, not its description — this fills that gap on demand. Pure
    display enhancement: filtering always still works on raw ids regardless.
    Which column IS the description is resolved via sac_dim_config — a saved
    per-dimension override first, else a configurable pattern list (both
    editable in config/master_data_columns.yaml, or from this same dimension's
    ⋮ menu -> 'Show raw columns' — no code change needed for a new model's
    naming, that's the whole point of pushing this into config)."""
    rows = _ins_dim_rows(dim, provider=provider)
    result: dict[str, str] = {}
    if rows:
        cols = list(rows[0].keys())
        id_col = _order_member_cols(cols, dim)[0] if cols else None
        desc_col = DIMCFG.find_column(cols, dim, "description", exclude=id_col)
        if id_col and desc_col:
            for r in rows:
                rid, rdesc = r.get(id_col), r.get(desc_col)
                if rid is not None and rdesc:
                    result[str(rid)] = str(rdesc)
    return result


def _ins_find_parent_col(cols, dim: str, id_col) -> str | None:
    """Best-effort parent/hierarchy column detector, resolved via
    sac_dim_config (see _ins_dim_descriptions above for why) — this is
    UNVERIFIED against a real tenant payload from THIS session (needs live
    confirmation) so the config's default pattern list is intentionally
    permissive; a wrong guess is fixed in config, not in this function."""
    return DIMCFG.find_column(cols, dim, "parent", exclude=id_col)


def _ins_dim_hierarchy_rank(dim: str, *, provider: str | None) -> dict[str, tuple[int, int]]:
    """Best-effort member id -> (sort_rank, depth), IF this dimension's
    master-data rows expose a detectable parent/hierarchy column. Returns {}
    when none is found (most dimensions, or a tenant that names it something
    this doesn't recognize yet) — callers MUST treat that as 'no hierarchy
    available' and fall back to the flat alphabetical list, never a hard
    dependency. sort_rank orders members into a parent-then-children (pre-order)
    sequence; depth is how many levels indented for display."""
    rows = _ins_dim_rows(dim, provider=provider)
    if not rows:
        return {}
    cols = list(rows[0].keys())
    id_col = _order_member_cols(cols, dim)[0] if cols else None
    parent_col = _ins_find_parent_col(cols, dim, id_col) if id_col else None
    if not parent_col:
        return {}
    parent_of: dict[str, str] = {}
    all_ids: set[str] = set()
    for r in rows:
        rid = r.get(id_col)
        if rid is None:
            continue
        rid = str(rid)
        all_ids.add(rid)
        p = r.get(parent_col)
        if p is not None and str(p) not in ("", rid):
            parent_of[rid] = str(p)
    children: dict[str, list[str]] = {}
    for rid, p in parent_of.items():
        children.setdefault(p, []).append(rid)
    roots = sorted(i for i in all_ids if i not in parent_of)

    result: dict[str, tuple[int, int]] = {}
    next_rank = [0]
    def visit(node, depth, seen):
        if node in seen:                      # guard against cyclic/bad data
            return
        seen.add(node)
        result[node] = (next_rank[0], depth)
        next_rank[0] += 1
        for c in sorted(children.get(node, [])):
            visit(c, depth + 1, seen)
    seen: set[str] = set()
    for r in roots:
        visit(r, 0, seen)
    for rid in sorted(all_ids):                # any disconnected leftovers still get a rank
        if rid not in result:
            result[rid] = (next_rank[0], 0)
            next_rank[0] += 1
    return result


def _ins_render_panel_fields(df: pd.DataFrame, spec: dict, *, vvals: list[str],
                             actual_version: str, dims: list[str], numeric_cols: list[str],
                             provider: str | None = None, industry: str | None = None
                             ) -> bool:
    """Renders the builder-panel form for ONE table spec, stacked single-column
    (this panel is narrow — a right-hand rail, not the full page width). Mutates
    spec in place. Returns True if "Build table" was clicked this run."""
    _ins_ensure_spec_defaults(spec)
    tid = spec["id"]
    # Row/Column are multiselect "wells" — like the Measures well below — so you
    # can drag in more than one field per side (a 2-level row breakdown, etc.),
    # matching a standard designer/pivot-builder panel.
    row_sel = st.multiselect(
        "Row dimension(s)", dims,
        default=[d for d in spec["row_dims"] if d in dims], key=f"row_{tid}",
        help="Leave empty for a single total (no breakdown). Pick 2+ for a nested breakdown.")
    spec["row_dims"] = row_sel

    col_opts = [d for d in dims if d not in row_sel]
    col_sel = st.multiselect(
        "Column dimension(s) (optional)", col_opts,
        default=[d for d in spec["col_dims"] if d in col_opts], key=f"col_{tid}")
    spec["col_dims"] = col_sel

    time_candidates = list(dict.fromkeys(row_sel + col_sel))  # dedupe, keep order
    time_opts = ["(none)"] + time_candidates
    time_default = spec["time_dim"] if spec["time_dim"] in time_candidates else "(none)"
    time_sel = st.selectbox("Time dimension (for line charts / LAST)", time_opts,
                            index=time_opts.index(time_default), key=f"time_{tid}",
                            help="Only needed for time-series charts or the LAST aggregation.")
    spec["time_dim"] = None if time_sel == "(none)" else time_sel

    measures_sel = st.multiselect(
        "Measures", numeric_cols,
        default=[m for m in spec["measure_cols"] if m in numeric_cols], key=f"meas_{tid}")
    spec["measure_cols"] = measures_sel
    for m in measures_sel:
        cur = spec["agg"].get(m, INS.CHOOSE)
        spec["agg"][m] = st.selectbox(
            f"Aggregation — {m}", INS.AGG_TYPES,
            index=INS.AGG_TYPES.index(cur) if cur in INS.AGG_TYPES else 0, key=f"agg_{tid}_{m}")
        # Playbook SUGGESTION only — purely informational, never sets anything.
        # The Cost/Revenue call below is still made explicitly by the user.
        suggestion = PB.suggest_category(m, industry=industry)
        if suggestion:
            st.caption(f"📖 Playbook: looks like **{suggestion['label']}** "
                      f"({suggestion['favourability']}-type)." +
                      (f" {suggestion['note']}" if suggestion.get("note") else ""))

    with st.expander("➕ Calculated measure (optional)"):
        spec["calc_on"] = st.checkbox("Add a calculated measure", value=spec["calc_on"], key=f"ccON_{tid}")
        if spec["calc_on"]:
            st.caption("Combine 2+ measures left-to-right (no algebraic precedence — "
                      "e.g. Revenue − COGS − Opex, evaluated in the order shown). "
                      "Each term is always SUMmed first, like the old Ratio(%) feature.")
            n_terms = st.selectbox("Number of terms", [2, 3, 4],
                                   index=[2, 3, 4].index(spec["calc_n"]) if spec["calc_n"] in (2, 3, 4) else 0,
                                   key=f"ccN_{tid}")
            spec["calc_n"] = n_terms
            opts = ["(choose)"] + numeric_cols
            terms_cur, ops_cur = spec["calc_terms"], spec["calc_ops"]
            terms, ops = [], []
            for i in range(n_terms):
                if i > 0:
                    cur_op = ops_cur[i - 1] if (i - 1) < len(ops_cur) else "+"
                    op = st.selectbox(f"Operator before term {i + 1}", INS.CALC_OPS,
                                      index=INS.CALC_OPS.index(cur_op) if cur_op in INS.CALC_OPS else 0,
                                      key=f"ccOp_{tid}_{i}")
                    ops.append(op)
                cur = terms_cur[i] if i < len(terms_cur) else None
                sel = st.selectbox(f"Term {i + 1}", opts,
                                   index=opts.index(cur) if cur in opts else 0, key=f"ccT_{tid}_{i}")
                terms.append(None if sel == "(choose)" else sel)
            spec["calc_terms"], spec["calc_ops"] = terms, ops
            spec["calc_label"] = st.text_input("Label", value=spec["calc_label"], key=f"cclbl_{tid}")
            spec["calc_pct"] = st.checkbox("Express as % (×100)", value=spec["calc_pct"], key=f"ccpct_{tid}",
                                           help="Typically only meaningful when the last operator is ÷.")

    _calc_ready = spec["calc_on"] and len(spec["calc_terms"]) >= 2 and all(spec["calc_terms"])
    n_effective = len(measures_sel) + (1 if _calc_ready else 0)

    if n_effective == 1:
        st.markdown("**Comparison (optional)**")
        st.caption("Declare this to get Favourable/Unfavourable + variance.")
        spec["reference_kind"] = st.selectbox(
            "Compare Actual to", INS.REFERENCE_KINDS,
            index=INS.REFERENCE_KINDS.index(spec["reference_kind"]), key=f"refk_{tid}")
        if spec["reference_kind"] != "None":
            other_vals = [v for v in vvals if v != actual_version]
            guess = INS.guess_reference_value(other_vals, spec["reference_kind"])
            cur_ref = spec["reference_version"] if spec["reference_version"] in other_vals else guess
            spec["reference_version"] = st.selectbox(
                f"{spec['reference_kind']} version", other_vals,
                index=other_vals.index(cur_ref) if cur_ref in other_vals else 0, key=f"refv_{tid}")
            mtype = st.segmented_control(
                "Measure type", INS.MEASURE_TYPES, required=True,
                default=INS.MEASURE_TYPES[0 if spec["measure_type"] == "cost" else 1],
                help="Cost/expense: lower than reference is favourable. "
                     "Revenue/income: higher than reference is favourable.",
                key=f"mtype_{tid}")
            spec["measure_type"] = "cost" if mtype.startswith("Cost") else "revenue"
        else:
            spec["reference_version"] = None
        # Presentation is independent of having a comparison — Composition (donut)
        # works standalone; Driver/Waterfall need a comparison and fall back to Auto
        # (safely, in the chart-selection rules) if one isn't set, with a note here.
        spec["presentation"] = st.selectbox(
            "Presentation", INS.PRESENTATIONS,
            index=INS.PRESENTATIONS.index(spec["presentation"]), key=f"pres_{tid}")
        if (spec["presentation"] in ("Driver / contribution analysis", "Variance waterfall")
                and spec["reference_kind"] == "None"):
            st.caption("⚠️ Driver/Waterfall need a comparison (above) to take effect — "
                      "falls back to Auto until one is set.")
    else:
        spec["reference_kind"], spec["reference_version"] = "None", None
        if n_effective > 1:
            st.caption("Multiple measures selected → this is a browse table (no "
                      "Favourable/Unfavourable comparison). Pick exactly one measure "
                      "to enable a Budget/Forecast/Prior-Year comparison.")

    with st.expander("🔎 Filters (optional)"):
        if dims:
            show_desc = spec.setdefault("filter_show_desc", {})
            show_hier = spec.setdefault("filter_show_hier", {})
            for d in dims:
                options = sorted(df[d].astype(str).unique().tolist())
                cur = [v for v in spec["filters"].get(d, []) if v in options]
                fc1, fc2 = st.columns([6, 1])
                # Render the toggles FIRST (inside the ⋮ popover) and use their
                # return values immediately — reading the stored dict value
                # before the widget call would lag a full rerun behind the
                # click (fetch only on the NEXT rerun).
                with fc2.popover("", icon=":material/more_vert:", help=f"{d} options"):
                    want_desc = st.toggle(
                        "Show ID + Description", value=show_desc.get(d, False),
                        key=f"filtdesc_{tid}_{d}",
                        help="Look up each member's description via SAC master data "
                             "(one-time live lookup per dimension, then cached).")
                    want_hier = st.toggle(
                        "Show as hierarchy (indented)", value=show_hier.get(d, False),
                        key=f"filthier_{tid}_{d}",
                        help="Indent members under their parent, IF this dimension "
                             "exposes hierarchy data via SAC's export API. No effect "
                             "(flat list stays) if it doesn't.")
                    if provider and st.toggle(
                            "Show raw columns (debug)", value=False, key=f"filtdbg_{tid}_{d}",
                            help="See exactly what SAC's export API returns for this "
                                 "dimension — use this if Description/hierarchy come up "
                                 "empty and you want to know why."):
                        dbg_rows = _ins_dim_rows(d, provider=provider)
                        if dbg_rows:
                            dbg_cols = list(dbg_rows[0].keys())
                            st.caption(f"{len(dbg_rows)} member row(s) · columns: "
                                      f"{', '.join(dbg_cols)}")
                            st.json(dbg_rows[0])
                            st.caption("If Description/hierarchy picked the wrong column "
                                      "above, confirm the right one here — saved once, "
                                      "reused for every model/session, no code change:")
                            auto = "(auto-detect)"
                            dopts = [auto] + dbg_cols
                            dcur = DIMCFG.override_column(d, "description") or auto
                            dsel = st.selectbox("This dimension's Description column", dopts,
                                                index=dopts.index(dcur) if dcur in dopts else 0,
                                                key=f"dimcfg_desc_{tid}_{d}")
                            pcur = DIMCFG.override_column(d, "parent") or auto
                            psel = st.selectbox("This dimension's Parent/hierarchy column", dopts,
                                                index=dopts.index(pcur) if pcur in dopts else 0,
                                                key=f"dimcfg_parent_{tid}_{d}")
                            if st.button("Save column mapping for this dimension",
                                        key=f"dimcfg_save_{tid}_{d}"):
                                ok1 = DIMCFG.set_override(d, "description",
                                                          None if dsel == auto else dsel)
                                ok2 = DIMCFG.set_override(d, "parent",
                                                          None if psel == auto else psel)
                                if ok1 and ok2:
                                    st.success(f"Saved — every table's '{d}' filter will use "
                                              "this from now on. Toggle Description/hierarchy "
                                              "off and on again to see it applied.")
                                else:
                                    st.error("Couldn't write config/master_data_columns.yaml "
                                             "— check the app process can write to that folder.")
                        else:
                            st.caption("No rows came back for this dimension name — "
                                      "the export API read failed or found nothing.")
                show_desc[d] = want_desc
                show_hier[d] = want_hier
                desc_map: dict[str, str] = {}
                hier_rank: dict[str, tuple[int, int]] = {}
                if (want_desc or want_hier) and provider:
                    with st.spinner(f"Loading {d} member data…"):
                        if want_desc:
                            desc_map = _ins_dim_descriptions(d, provider=provider)
                        if want_hier:
                            hier_rank = _ins_dim_hierarchy_rank(d, provider=provider)
                if want_hier and hier_rank:
                    options = sorted(options, key=lambda v: hier_rank.get(v, (10**9, 0)))  # noqa: B023
                elif want_hier and provider:
                    st.caption(f"No hierarchy data found for '{d}' — showing flat list.")
                label = lambda v: (                                          # noqa: B023
                    ("　" * hier_rank[v][1] + ("└ " if hier_rank[v][1] else "")
                     if want_hier and v in hier_rank else "")
                    + (f"{v} — {desc_map[v]}" if v in desc_map else v))
                spec["filters"][d] = fc1.multiselect(
                    d, options, default=cur, key=f"filt_{tid}_{d}", format_func=label)
                if want_desc and provider and not desc_map:
                    st.caption(f"No description found for '{d}' — showing raw values.")
        else:
            st.caption("No other dimensions available to filter by.")
        if not provider:
            st.caption("Descriptions/hierarchy need a live model connection — unavailable in demo mode.")

    return st.button("Build table", icon=":material/build:", key=f"build_{tid}",
                     type="primary", width="stretch")


@st.dialog("Table detail", width="large")
def _ins_fullscreen_dialog(result: dict, tid: int) -> None:
    meta, disp = result["meta"], result["display_df"]
    st.markdown(f"#### {meta['title']}")
    _ins_render_result_body(result, tid, key_suffix="_fs")


def _ins_render_result_body(result: dict, tid: int, *, key_suffix: str = "",
                            industry: str | None = None) -> None:
    """The KPI/chart/table/Get-Insights body shared by the main pane and the
    full-screen dialog. key_suffix keeps widget keys unique between the two.
    industry defaults from session_state so the @st.dialog call site (which
    can't easily receive cap_insights()'s local variable) still gets it."""
    industry = industry or st.session_state.get("ins_industry")
    meta, disp = result["meta"], result["display_df"]
    if meta.get("has_reference"):
        tot_a = float(disp["actual"].sum()); tot_r = float(disp["reference"].sum())
        var = tot_a - tot_r
        var_pct = (var / abs(tot_r) * 100.0) if tot_r else None
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Actual", f"{tot_a:,.0f}")
        k2.metric(meta["reference_kind"], f"{tot_r:,.0f}")
        # delta_color is a FIXED mapping from measure type (not from this variance's
        # sign): cost -> "inverse" so a decrease (underspend, favourable) shows green;
        # revenue -> "normal" so an increase (favourable) shows green.
        k3.metric("Variance", f"{var:,.0f}", delta=f"{var:,.0f}",
                 delta_color=("inverse" if meta.get("measure_type") == "cost" else "normal"))
        k4.metric("Var %", "n/a" if var_pct is None else f"{var_pct:,.1f}%")

    chart_png = REP.render_chart_png(result)
    if chart_png:
        st.image(chart_png, width="stretch")
    elif meta["chart_kind"] == "tile" and "actual" in disp.columns:
        st.metric(meta["measures"][0], f"{float(disp['actual'].iloc[0]):,.0f}")
    st.caption(f"Chart: `{meta['chart_kind']}` (rules-selected, not AI-picked) · "
              f"{len(disp)} row(s) shown.")
    st.dataframe(disp, width="stretch", hide_index=True)

    if st.button("Get Insights", icon=":material/auto_awesome:", key=f"obsbtn_{tid}{key_suffix}",
                 help="The ONLY AI step — observes this table's own visible values only "
                      "(grounded by a small, relevant playbook excerpt, if any matches)."):
        facts = INS.build_view_facts(result)
        pb_context = PB.relevant_context(meta["measures"], industry=industry)
        with st.spinner("Reading the visible table…"):
            md, source = INS.generate_observations(facts, playbook_context=pb_context)
        st.session_state["ins_obs"][tid] = (md, source)
    saved_obs = st.session_state["ins_obs"].get(tid)
    if saved_obs:
        md, source = saved_obs
        badge = "🟢 Gemini" if source == "gemini" else "⚪ Template"
        st.caption(f"AI observations: {badge} — this is the only AI-generated content on this page.")
        st.markdown(md)


def cap_insights():
    st.subheader("📈 Insights & Reporting")
    st.caption("Build one or more tables — pick the rows, columns, measures and (optionally) "
              "a Budget/Forecast/Prior-Year comparison in the builder panel. Fixed rules then "
              "pick the chart type and the Favourable/Unfavourable calls — never AI. AI is used "
              "only if you click **Get Insights** on a built table, and only sees that table's "
              "own values.")

    demo = st.toggle("🧪 Use sample data (demo — no SAC connection needed)",
                     value=not is_authenticated(),
                     help="A synthetic dataset (cost centres × months × Actual/Budget/"
                          "Forecast/Prior-Year) so you can try every feature before a live "
                          "model is wired.")
    df, model_name, provider = None, "SAC model", None
    if demo:
        df, model_name = INS.sample_dataframe(), "DEMO (sample data)"
    elif not is_authenticated():
        st.warning("Connect to SAC first (Connect tab), or switch on demo above."); return
    else:
        active_model_id = (effective_model_id() or CFG.export_provider_id or "").strip()
        provider_in = st.text_input(
            "Export provider (defaults to the active model)", value=active_model_id,
            help="Pre-filled with the active model id; change only if the export "
                 "provider differs from the model.")
        if active_model_id:
            st.caption(f"Active model: **{effective_model_name() or '—'}** · `{active_model_id}`")
        if st.button("📥 Load model data", type="primary"):
            provider = (provider_in or active_model_id).strip()   # fall back to active model
            if not provider:
                st.error("No active model — select one on the **Models** tab, or type a "
                         "provider / model id above."); return
            st.session_state.pop("ins_rows", None)
            with st.spinner(f"Reading model data via Data Export ({provider})…"):
                rows, fail = read_fact_data(client(), provider)
            if rows is not None:
                st.session_state["ins_rows"] = rows
                st.success(f"Loaded {len(rows):,} rows.")
            else:
                st.error(f"Couldn't read data (HTTP {getattr(fail, 'status_code', '?')}). "
                         "The model may not be export-enabled, or its export-provider id "
                         "differs from the model id — check the **Versions** tab diagnostics.")
        _rows = st.session_state.get("ins_rows")
        df = pd.DataFrame(_rows) if _rows else None
        model_name = effective_model_name() or "SAC model"
        provider = (provider_in or active_model_id).strip() or None

    if df is None or df.empty:
        st.info("Load a model above (or switch on demo) to begin."); return

    cols = list(df.columns)
    vguess = _detect_version_col(df)
    vc1, vc2 = st.columns(2)
    version_col = vc1.selectbox("Version / Category column", cols,
                                index=cols.index(vguess) if vguess in cols else 0, key="ins_vcol")
    vvals = sorted(df[version_col].astype(str).unique())
    a_guess = INS.guess_version_value(vvals, "actual", "act")
    actual_version = vc2.selectbox("Actual version", vvals,
                                   index=vvals.index(a_guess) if a_guess in vvals else 0, key="ins_averf")
    industry = st.selectbox(
        "Industry / model type (playbook)", PB.INDUSTRIES, key="ins_industry",
        help="Grounds measure-category suggestions and the Get Insights narrative in the "
             "right vocabulary/formulas (e.g. AT&C loss, ACS-ACoE gap for utilities/Discom). "
             "Purely a suggestion source — never overrides what you declare in the builder.")
    st.caption("Changing the version/actual fields above won't retroactively update "
              "already-built tables — click **Build table** again on each to refresh them.")

    dims = INS.dimension_columns(df, version_col)
    numeric_cols = INS.numeric_columns(df)
    if not numeric_cols:
        st.warning("No numeric measure column found in this model's data."); return
    if not dims:
        st.warning("No dimension column available to build tables with."); return

    st.session_state.setdefault("ins_tables", [])
    st.session_state.setdefault("ins_built", {})
    st.session_state.setdefault("ins_obs", {})
    st.session_state.setdefault("ins_next_id", 1)
    st.session_state.setdefault("ins_active_id", None)
    st.session_state.setdefault("ins_panel_collapsed", False)

    tables = st.session_state["ins_tables"]

    # ---- 1) Table selector strip — click a table to make it active; the builder
    # panel below always reflects whichever table was clicked most recently. ----
    st.markdown("##### 1️⃣ Pick or add a table")
    strip = st.columns(len(tables) + 1)
    for i, t in enumerate(tables):
        is_active = (t["id"] == st.session_state["ins_active_id"])
        # Label by CURRENT POSITION (1, 2, 3...), not the permanently-unique
        # internal id — so numbering always starts at Table 1 and stays gapless
        # even after earlier tables were removed. Only overridden once the user
        # actually types a custom title (see the panel header below).
        if strip[i].button(t["title"] or f"Table {i + 1}", key=f"sel_{t['id']}",
                           type=("primary" if is_active else "secondary"),
                           width="stretch"):
            st.session_state["ins_active_id"] = t["id"]
            st.session_state["ins_panel_collapsed"] = False
            st.rerun()
    if strip[-1].button("Add table", icon=":material/add:", key="ins_add", width="stretch"):
        tid = st.session_state["ins_next_id"]; st.session_state["ins_next_id"] += 1
        st.session_state["ins_tables"].append(_ins_new_table(df, dims, tid))
        st.session_state["ins_active_id"] = tid
        st.session_state["ins_panel_collapsed"] = False
        st.rerun()

    if not tables:
        st.info("👆 Click **Add table** to choose the rows, columns and measures for your "
                "first table — a builder panel will open on the right for it.")
        return

    if st.session_state["ins_active_id"] not in [t["id"] for t in tables]:
        st.session_state["ins_active_id"] = tables[0]["id"]
    active_id = st.session_state["ins_active_id"]
    active_spec = next(t for t in tables if t["id"] == active_id)

    if st.button("Reset all tables", icon=":material/refresh:", key="ins_reset_all"):
        st.session_state["ins_tables"] = []
        st.session_state["ins_built"] = {}
        st.session_state["ins_obs"] = {}
        st.session_state["ins_active_id"] = None
        st.session_state["ins_next_id"] = 1     # so the next table starts back at 1
        st.session_state.pop("ins_report", None)
        st.rerun()

    # ---- 2) Two-pane layout: results/chart on the left, the ACTIVE table's ----
    # builder panel on the right (collapsible to reclaim width for the chart). ----
    st.markdown("##### 2️⃣ Configure the selected table (builder panel, right) "
               "→ 3️⃣ review it (left, expandable)")
    panel_collapsed = st.session_state["ins_panel_collapsed"]
    if panel_collapsed:
        if st.button("Show builder panel", icon=":material/right_panel_open:", key="ins_expand_panel"):
            st.session_state["ins_panel_collapsed"] = False
            st.rerun()
        main_col, panel_col = st.container(), None
    else:
        main_col, panel_col = st.columns([3, 2], gap="large")

    # Position-based auto title (see _ins_new_table) — recomputed every render so
    # it stays correct even after earlier tables are removed and this one shifts.
    position = tables.index(active_spec) + 1
    auto_title = f"Table {position}"
    effective_title = active_spec["title"] or auto_title

    if panel_col is not None:
        with panel_col:
            with st.container(border=True):
                ph1, ph2, ph3 = st.columns([3, 1, 1])
                typed_title = ph1.text_input(
                    "Table title", value=effective_title, key=f"ttl_{active_id}",
                    label_visibility="collapsed")
                # Only treat it as "customized" (and stop auto-renumbering) once the
                # user actually types something other than the current auto label.
                active_spec["title"] = "" if typed_title == auto_title else typed_title
                effective_title = typed_title
                if ph2.button(":material/right_panel_close:", key="ins_collapse_panel",
                             width="stretch", help="Collapse the builder panel"):
                    st.session_state["ins_panel_collapsed"] = True
                    st.rerun()
                if ph3.button(":material/delete:", key=f"rm_{active_id}", width="stretch",
                             help="Remove this table"):
                    st.session_state["ins_tables"] = [t for t in tables if t["id"] != active_id]
                    st.session_state["ins_built"].pop(active_id, None)
                    st.session_state["ins_obs"].pop(active_id, None)
                    st.session_state["ins_active_id"] = None
                    st.rerun()
                st.caption(":material/build: Builder panel — applies to the table selected above")
                build_clicked = _ins_render_panel_fields(
                    df, active_spec, vvals=vvals, actual_version=actual_version,
                    dims=dims, numeric_cols=numeric_cols, provider=provider, industry=industry)
                if build_clicked:
                    _ins_try_build(df, active_spec, version_col=version_col,
                                   actual_version=actual_version, title=effective_title)

    with main_col:
        result = st.session_state["ins_built"].get(active_id)
        if not result:
            st.caption("Configure this table in the builder panel and click Build table "
                      "to see it here." if not panel_collapsed else
                      "This table hasn't been built yet — click Show builder panel above.")
        else:
            top_l, top_r = st.columns([5, 1])
            top_l.markdown(f"#### {effective_title}")
            if top_r.button("Full screen", icon=":material/fullscreen:", key=f"fs_{active_id}",
                            width="stretch"):
                _ins_fullscreen_dialog(result, active_id)
            _ins_render_result_body(result, active_id, industry=industry)

    # ---- 4) Final step: bundle every BUILT table into one exportable report. ----
    built_ids = [t["id"] for t in tables if t["id"] in st.session_state["ins_built"]]
    st.markdown("##### 🏁 4️⃣ Final step — generate the management report")
    if not built_ids:
        st.info("Build at least one table above, then generate the report here.")
        return

    st.caption(f"{len(built_ids)} of {len(tables)} table(s) are built and will be included.")
    fmt = st.segmented_control("Format", ["PPTX", "PDF"], default="PPTX", required=True,
                               key="ins_fmt")
    if st.button("📤 Build report", key="ins_build_report", type="primary"):
        try:
            import datetime as _dt
            tables_payload = []
            for tid in built_ids:
                result = st.session_state["ins_built"][tid]
                tables_payload.append({
                    "result": result,
                    "chart_png": REP.render_chart_png(result),
                    "observations_md": (st.session_state["ins_obs"].get(tid) or (None, None))[0],
                })
            report = {
                "title": "SAC FP&A Agent — Management Reporting",
                "subtitle": f"{len(tables_payload)} table(s) · {model_name}",
                "lineage": (f"Model: {model_name} · Actual version: {actual_version} · "
                            f"Generated: {_dt.datetime.now():%Y-%m-%d %H:%M}"),
                "tables": tables_payload,
                "disclaimer": ("Figures are computed deterministically from SAC model data per "
                              "user-declared rules. Any AI-generated observations are marked and "
                              "should be verified against source data."),
            }
            if fmt == "PPTX":
                data = REP.build_pptx(report)
                mime = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
                ext = "pptx"
            else:
                data = REP.build_pdf(report); mime = "application/pdf"; ext = "pdf"
            st.session_state["ins_report"] = (fmt, data, ext, mime)
        except Exception as e:
            st.error(f"Export failed: {e}")
    rep = st.session_state.get("ins_report")
    if rep:
        _fmt, _data, _ext, _mime = rep
        st.download_button(f"⬇️ Download {_fmt}", data=_data,
                           file_name=f"SAC_Management_Report.{_ext}", mime=_mime, key="ins_dl")


ALL_PAGES = {
    "Connect": ("🔌", cap_connect),
    "Models": ("📦", cap_models),
    "Dimension Members": ("👥", cap_dim_members),
    "Stories": ("📖", cap_stories),
    "Ingest Actuals": ("⬆️", cap_actuals),
    "Master Data": ("🧬", cap_master_data),
    "Version Initialization": ("🔁", cap_version_init),
    "Run Data Action": ("⚙️", cap_other_das),
    "Driver Inputs": ("🎚️", cap_drivers),
    "Versions": ("🗂️", cap_versions),
    "Insights & Reporting": ("📈", cap_insights),
}

# Presentation personas — which tabs each role sees. SAC still enforces the
# real permissions via the signed-in user's token; this only tailors the UI.
ROLE_ACCESS = {
    "Power User": list(ALL_PAGES.keys()),
    "Planner": ["Connect", "Models", "Ingest Actuals", "Driver Inputs",
                "Versions", "Insights & Reporting"],
    "Management": ["Connect", "Versions", "Insights & Reporting"],
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

    if choice not in ("Connect", "Insights & Reporting") and not is_authenticated():
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
