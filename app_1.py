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
import time
import secrets
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
def build_login_url():
    state = secrets.token_urlsafe(24)
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
    for k in ("access_token", "refresh_token", "token_expiry", "oauth_state"):
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
# POLLER — generic async job poller
# ============================================================================
def poll_job(c, status_path, timeout_s=900, interval_s=5, ui=None):
    start = time.time()
    while time.time() - start < timeout_s:
        try:
            data = c.get(status_path).json()
        except Exception as e:
            return {"status": "poll_error", "detail": str(e)}
        status = str(data.get("status", "")).upper()
        if ui:
            ui.write(f"Status: {status or 'UNKNOWN'} "
                     f"({int(time.time()-start)}s elapsed)")
        if status in ("COMPLETED", "DONE", "SUCCESS"):
            return {"status": "done", "detail": data}
        if status in ("FAILED", "ERROR"):
            return {"status": "failed", "detail": data}
        time.sleep(interval_s)
    return {"status": "timeout", "detail": {}}


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
        if qp.get("state") != st.session_state.get("oauth_state"):
            st.error("State mismatch — re-start the login (stale link or rerun).")
            st.query_params.clear(); return
        try:
            _store_token(exchange_code_for_token(qp["code"]))
            st.query_params.clear(); st.rerun()
        except requests.HTTPError as e:
            st.error(f"Token exchange failed: {e.response.status_code} — {e.response.text}")
            st.query_params.clear(); return

    st.link_button("Log in to SAC", build_login_url())
    st.caption("Opens the SAC login. A browser sign-in is required — "
               "the agent runs as you, not as an unattended service.")


# ============================================================================
# CAPABILITY 2 — INGEST ACTUALS (Data Import Service)
# ============================================================================
def cap_actuals():
    st.subheader("Ingest Actuals")
    if not need(CFG.model_id):
        st.error("PREREQUISITE MISSING — set SAC_MODEL_ID in .env."); return

    up = st.file_uploader("Upload actuals CSV", type=["csv"])
    if not up:
        st.info("Version and Date columns are mandatory and must be existing "
                "members in the model."); return

    df = pd.read_csv(up)
    st.write("Preview:", df.head())
    st.caption(f"{len(df)} rows detected.")

    if st.button("Validate & Import", type="primary"):
        c = client()
        rows = df.to_dict(orient="records")
        with st.status("Importing…", expanded=True) as box:
            try:
                job = c.post(f"/api/v1/dataimport/models/{CFG.model_id}/factData").json()
                job_id = job.get("jobID") or job.get("jobId")          # CONFIRM
                box.write(f"Job created: {job_id}")
                c.post(f"/api/v1/dataimport/jobs/{job_id}",
                       json={"Data": rows})                            # CONFIRM wrapper
                c.post(f"/api/v1/dataimport/jobs/{job_id}/validate")
                bad = c.get(f"/api/v1/dataimport/jobs/{job_id}/invalidRows").json()
                if bad.get("invalidRows"):
                    box.update(label="Validation failed", state="error")
                    st.error("Rejected rows — fix and re-upload:")
                    st.write(bad); return
                c.post(f"/api/v1/dataimport/jobs/{job_id}/run")
                res = poll_job(c, f"/api/v1/dataimport/jobs/{job_id}/status", ui=box)
                box.update(label=f"Import {res['status']}",
                           state="complete" if res["status"] == "done" else "error")
                st.write(res["detail"])
            except Exception as e:
                box.update(label="Error", state="error"); st.exception(e)


# ============================================================================
# CAPABILITY 3 — VERSION INITIALIZATION (Multi Action)
# ============================================================================
def run_multi_action(c, ma_id, parameters, ui=None):
    body = {"parameters": parameters} if parameters else {"parameters": []}  # CONFIRM schema; body must not be empty
    r = c.post(f"/api/v1/multiActions/{ma_id}/executions", json=body)
    if r.status_code not in (200, 201, 202):
        return {"status": "trigger_failed", "detail": {"code": r.status_code, "body": r.text}}
    execution_id = r.json().get("executionId")                              # CONFIRM key
    return poll_job(c, f"/api/v1/multiActions/{ma_id}/executions/{execution_id}", ui=ui)


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
        with st.status("Running version init…", expanded=True) as box:
            res = run_multi_action(c, CFG.ma_version_init_id, params, ui=box)
            box.update(label=f"Version init {res['status']}",
                       state="complete" if res["status"] == "done" else "error")
            st.write(res["detail"])


# ============================================================================
# CAPABILITY 4 — OTHER DATA ACTIONS (generic runner)
# ============================================================================
def cap_other_das():
    st.subheader("Run Data Action")
    # Registry — extend by adding MA_<NAME>_ID to .env and an entry here.
    registry = {k: v for k, v in {
        "Version Initialization": CFG.ma_version_init_id,
        # "Allocate Overheads": os.environ.get("MA_ALLOCATE_ID", ""),
    }.items() if v}
    if not registry:
        st.error("No Multi Actions configured. Add MA_*_ID values to .env."); return

    name = st.selectbox("Data action", list(registry.keys()))
    raw = st.text_area("Parameters as JSON list (optional)",
                       value='[]',
                       help='e.g. [{"name":"Region","value":"APAC"}]')
    if st.button("Run", type="primary"):
        import json
        try:
            params = json.loads(raw) if raw.strip() else []
        except json.JSONDecodeError:
            st.error("Parameters must be valid JSON."); return
        c = client()
        with st.status(f"Running {name}…", expanded=True) as box:
            res = run_multi_action(c, registry[name], params, ui=box)
            box.update(label=f"{name}: {res['status']}",
                       state="complete" if res["status"] == "done" else "error")
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
    if not need(CFG.model_id):
        st.error("PREREQUISITE MISSING — set SAC_MODEL_ID in .env."); return

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
        with st.status("Importing driver values…", expanded=True) as box:
            try:
                job = c.post(f"/api/v1/dataimport/models/{CFG.model_id}/factData").json()
                job_id = job.get("jobID") or job.get("jobId")              # CONFIRM
                c.post(f"/api/v1/dataimport/jobs/{job_id}", json={"Data": rows})
                c.post(f"/api/v1/dataimport/jobs/{job_id}/validate")
                c.post(f"/api/v1/dataimport/jobs/{job_id}/run")
                res = poll_job(c, f"/api/v1/dataimport/jobs/{job_id}/status", ui=box)
                box.update(label=f"Drivers {res['status']}",
                           state="complete" if res["status"] == "done" else "error")
                st.write(res["detail"])
                st.caption("Now run the apply/disaggregation data action (tab: Run Data Action).")
            except Exception as e:
                box.update(label="Error", state="error"); st.exception(e)


# ============================================================================
# CAPABILITY 6 — KPIs (Data Export read; display, don't re-derive)
# ============================================================================
def cap_kpis():
    st.subheader("KPIs")
    if not need(CFG.export_provider_id):
        st.error("PREREQUISITE MISSING — set SAC_EXPORT_PROVIDER_ID in .env."); return

    select = st.text_input("$select (comma-separated columns)", value="")
    odata_filter = st.text_input("$filter (OData)", value="")
    if st.button("Read model data", type="primary"):
        c = client()
        base = f"/api/v1/dataexport/providers/sac/{CFG.export_provider_id}/FactData"
        q = []
        if select: q.append(f"$select={select}")
        if odata_filter: q.append(f"$filter={odata_filter}")
        rows, skip = [], 0
        with st.status("Reading…", expanded=True) as box:
            try:
                while True:
                    page_q = q + [f"$skip={skip}", "$pagesize=20000"]
                    page = c.get(base + "?" + "&".join(page_q)).json()
                    vals = page.get("value", [])
                    rows += vals
                    box.write(f"{len(rows)} rows read…")
                    if len(vals) < 20000:
                        break
                    skip += 20000
                box.update(label="Read complete", state="complete")
            except Exception as e:
                box.update(label="Error", state="error"); st.exception(e); return
        df = pd.DataFrame(rows)
        st.write(df.head(50))
        st.caption("Display model-calculated KPIs as-is. Compute ONLY presentation "
                   "ratios that don't already exist in the model, and label them "
                   "as derived — never re-derive a model figure here.")
        # Example derived ratio (only if both columns are read measures):
        # df["Variance%"] = (df["Actual"] - df["Budget"]) / df["Budget"] * 100


# ============================================================================
# APP SHELL
# ============================================================================
def main():
    st.set_page_config(page_title="SAC FP&A Agent", page_icon="📊", layout="wide")
    st.markdown(
        f"<style>"
        f"h1,h2,h3{{color:{ACCENTURE_PURPLE};}}"
        f".stButton>button[kind=primary]{{background:{ACCENTURE_PURPLE};border:0;}}"
        f"</style>", unsafe_allow_html=True)

    st.sidebar.title("SAC FP&A Agent")
    if CONFIG_ERROR:
        st.error(CONFIG_ERROR)
        st.stop()

    conn = "🟢 Connected" if is_authenticated() else "⚪ Not connected"
    st.sidebar.caption(conn)

    pages = {
        "1 · Connect": cap_connect,
        "2 · Ingest Actuals": cap_actuals,
        "3 · Version Initialization": cap_version_init,
        "4 · Run Data Action": cap_other_das,
        "5 · Driver Inputs": cap_drivers,
        "6 · KPIs": cap_kpis,
    }
    choice = st.sidebar.radio("Capability", list(pages.keys()))

    # All capabilities except Connect require an authenticated session.
    if choice != "1 · Connect" and not is_authenticated():
        st.warning("Connect to SAC first (tab 1)."); return
    pages[choice]()


if __name__ == "__main__":
    main()
