"""
Real vs. Nominal Exchange Rate Explorer (IMF data)
==================================================

A Streamlit app that compares the nominal and real exchange rate between any
two IMF countries (or the Euro area) using monthly data from the IMF SDMX API.

Definitions
-----------
    S(t)          = units of Country 2 currency per 1 unit of Country 1 currency
                  = (Country 2 currency per USD) / (Country 1 currency per USD)
    P1(t), P2(t)  = monthly CPI index of Country 1 and Country 2
    RER(t)        = S(t) * P1(t) / P2(t)
    RER*          = mean of RER(t) over valid months in the selected sample
    Real Index(t) = 100 * RER(t) / RER*

Run with:  streamlit run app.py
"""

from __future__ import annotations

import re
import time

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from plotly.subplots import make_subplots

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

IMF_BASE = "https://api.imf.org/external/sdmx/3.0"       # primary API
IMF_BASE_21 = "https://api.imf.org/external/sdmx/2.1"    # fallback (same host)

CPI_FLOW = "CPI"   # IMF.STA dataflow: Consumer Price Index
ER_FLOW = "ER"     # IMF.STA dataflow: Exchange Rates
CPI_KEY = "{iso}.CPI._T.IX.M"        # all-items CPI, index, monthly
ER_KEY = "{iso}.XDC_USD.PA_RT.M"     # local currency per USD, period average, monthly

EURO_AREA = "G163"                    # IMF code for the Euro area
EARLIEST = pd.Timestamp("1976-01-01")  # we only use data from Jan 1976 onward

TIMEOUT = 45          # seconds per HTTP request
RETRIES = 3           # attempts per URL for transient errors
CACHE_TTL = 6 * 3600  # cache API answers for 6 hours

# Used only if the live country list cannot be downloaded.
FALLBACK_COUNTRIES = {
    "ARG": "Argentina", "AUS": "Australia", "AUT": "Austria", "BEL": "Belgium",
    "BRA": "Brazil", "CAN": "Canada", "CHE": "Switzerland", "CHL": "Chile",
    "CHN": "China", "COL": "Colombia", "CZE": "Czech Republic",
    "DEU": "Germany", "DNK": "Denmark", "EGY": "Egypt", "ESP": "Spain",
    "FIN": "Finland", "FRA": "France", "GBR": "United Kingdom",
    "GRC": "Greece", "HUN": "Hungary", "IDN": "Indonesia", "IND": "India",
    "IRL": "Ireland", "ISR": "Israel", "ITA": "Italy", "JPN": "Japan",
    "KEN": "Kenya", "KOR": "Korea, Republic of", "MEX": "Mexico",
    "MYS": "Malaysia", "NGA": "Nigeria", "NLD": "Netherlands",
    "NOR": "Norway", "NZL": "New Zealand", "PAK": "Pakistan", "PER": "Peru",
    "PHL": "Philippines", "POL": "Poland", "PRT": "Portugal",
    "RUS": "Russian Federation", "SAU": "Saudi Arabia", "SGP": "Singapore",
    "SWE": "Sweden", "THA": "Thailand", "TUR": "Türkiye", "USA": "United States",
    "VNM": "Vietnam", "ZAF": "South Africa",
}


class IMFError(Exception):
    """Raised when the IMF API cannot be reached or returns unusable output."""


# ---------------------------------------------------------------------------
# Low-level API helpers
# ---------------------------------------------------------------------------

def _get_json(url: str, headers: dict[str, str]) -> tuple[int, dict | None]:
    """
    GET a URL and return (status_code, parsed_json_or_None).

    Transient problems (timeouts, 429, 5xx) are retried with a short back-off
    and raise IMFError if they persist.  Other non-200 codes (404, 400, 406 ...)
    are returned to the caller so it can try another URL.
    """
    last_problem = "unknown error"
    for attempt in range(RETRIES):
        try:
            resp = requests.get(url, headers=headers, timeout=TIMEOUT)
        except requests.RequestException as exc:
            last_problem = f"network error ({exc.__class__.__name__})"
            time.sleep(1.5 * (attempt + 1))
            continue

        if resp.status_code == 200:
            try:
                return 200, resp.json()
            except ValueError as exc:
                raise IMFError("The IMF API returned a response that is not valid JSON.") from exc
        if resp.status_code in (429, 500, 502, 503, 504):
            last_problem = f"HTTP {resp.status_code}"
            time.sleep(1.5 * (attempt + 1))
            continue
        return resp.status_code, None

    raise IMFError(f"IMF API unavailable ({last_problem}) for {url}")


_PERIOD_RE = re.compile(r"^(\d{4})-?M?(\d{1,2})$")  # "1976-M01" or "1976-01"


def _parse_period(text: str) -> pd.Timestamp | None:
    """Convert an IMF monthly period code to the first day of that month."""
    match = _PERIOD_RE.match(str(text).strip())
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        return None
    return pd.Timestamp(year=year, month=month, day=1)


def _parse_sdmx_json(payload: dict) -> pd.Series:
    """
    Turn an SDMX-JSON data message (v1.0 or v2.0 layout) into a monthly Series.

    With a fully specified key there is exactly one series; if several come
    back we keep the one with the most observations.
    """
    root = payload.get("data", payload)
    datasets = root.get("dataSets") or []
    if not datasets:
        return pd.Series(dtype=float)

    structures = root.get("structures") or []
    structure = structures[0] if structures else root.get("structure", {})
    obs_dims = (structure or {}).get("dimensions", {}).get("observation", [])
    time_dim = next((d for d in obs_dims if d.get("id") == "TIME_PERIOD"),
                    obs_dims[0] if obs_dims else None)
    if time_dim is None:
        return pd.Series(dtype=float)
    periods = [v.get("id") or v.get("value") for v in time_dim.get("values", [])]

    best: dict = {}
    for series in (datasets[0].get("series") or {}).values():
        obs = series.get("observations") or {}
        if len(obs) > len(best):
            best = obs

    records: dict[pd.Timestamp, float] = {}
    for idx, vals in best.items():
        try:
            ts = _parse_period(periods[int(idx)])
            raw = vals[0] if isinstance(vals, list) else vals
            value = float(raw)
        except (IndexError, ValueError, TypeError):
            continue  # skip malformed or missing observations
        if ts is not None:
            records[ts] = value

    out = pd.Series(records, dtype=float).sort_index()
    return out[out.index >= EARLIEST]


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def fetch_series(flow: str, key: str) -> pd.Series:
    """
    Download one monthly series from the IMF.

    Returns an empty Series when the IMF has no data for the key.
    Raises IMFError when the API itself cannot be reached (not cached, so the
    next run retries).
    """
    candidates = [
        # SDMX 3.0: /data/{context}/{agency}/{resource}/{version}/{key}
        (f"{IMF_BASE}/data/dataflow/IMF.STA/{flow}/+/{key}",
         {"Accept": "application/vnd.sdmx.data+json;version=2.0.0"}),
        # SDMX 2.1 fallback on the same host
        (f"{IMF_BASE_21}/data/IMF.STA,{flow}/{key}",
         {"Accept": "application/vnd.sdmx.data+json;version=1.0.0"}),
    ]
    saw_no_data = False
    errors: list[str] = []

    for url, headers in candidates:
        try:
            status, payload = _get_json(url, headers)
        except IMFError as exc:
            errors.append(str(exc))
            continue
        if status == 200 and payload is not None:
            series = _parse_sdmx_json(payload)
            if not series.empty:
                return series
            saw_no_data = True
        elif status == 404:
            saw_no_data = True
        else:
            errors.append(f"HTTP {status} from {url}")

    if saw_no_data or not errors:
        return pd.Series(dtype=float)
    raise IMFError("; ".join(errors))


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def fetch_country_list() -> tuple[dict[str, str], bool]:
    """
    Return ({code: name}, is_live).  Falls back to a built-in list on failure.
    The Euro area (G163) is always included.
    """
    countries: dict[str, str] = {}
    live = False
    try:
        status, payload = _get_json(
            f"{IMF_BASE}/structure/codelist/IMF/CL_COUNTRY/+",
            {"Accept": "application/vnd.sdmx.structure+json;version=2.0.0"},
        )
        if status == 200 and payload:
            lists = payload.get("data", {}).get("codelists", [])
            for code in (lists[0].get("codes", []) if lists else []):
                cid = str(code.get("id", ""))
                name = code.get("name") or (code.get("names") or {}).get("en") or cid
                if re.fullmatch(r"[A-Z]{3}", cid):  # keep ISO3 countries only
                    countries[cid] = name
            live = len(countries) > 20
    except IMFError:
        pass

    if not live:
        countries = dict(FALLBACK_COUNTRIES)
    countries[EURO_AREA] = "Euro area"
    return dict(sorted(countries.items(), key=lambda kv: kv[1])), live


# ---------------------------------------------------------------------------
# Data loading and calculations
# ---------------------------------------------------------------------------

def load_series(flow: str, key: str) -> tuple[pd.Series | None, str | None]:
    """Fetch a series; return (series, None) or (None, user-facing message)."""
    try:
        s = fetch_series(flow, key)
    except IMFError as exc:
        return None, f"Could not reach the IMF API: {exc}"
    if s.empty:
        return None, "the IMF has no data for this series"
    return s, None


def build_dataset(iso1: str, iso2: str, names: dict[str, str]):
    """
    Download CPI and exchange rates for both countries and combine them.

    Returns (DataFrame | None, problems, coverage) where DataFrame has columns
    S, P1, P2, RER on a monthly index.
    """
    problems: list[str] = []
    coverage: list[dict] = []
    loaded: dict[str, pd.Series] = {}

    jobs = []
    for tag, iso in (("1", iso1), ("2", iso2)):
        jobs.append((f"P{tag}", f"CPI - {names[iso]}", CPI_FLOW, CPI_KEY.format(iso=iso)))
        # The USD is the numeraire, so USA's rate per USD is 1 by definition.
        if iso != "USA":
            jobs.append((f"E{tag}", f"Exchange rate - {names[iso]}", ER_FLOW, ER_KEY.format(iso=iso)))

    for tag, label, flow, key in jobs:
        series, problem = load_series(flow, key)
        if series is None:
            problems.append(f"**{label}**: {problem}.")
        else:
            loaded[tag] = series
            coverage.append({"Series": label, "First month": series.index.min().strftime("%Y-%m"),
                             "Last month": series.index.max().strftime("%Y-%m"),
                             "Observations": len(series)})

    if problems:
        return None, problems, coverage

    # Local currency per USD (USA = 1).
    er = {}
    for tag, iso in (("1", iso1), ("2", iso2)):
        if iso == "USA":
            other = loaded["E2" if tag == "1" else "E1"]
            er[tag] = pd.Series(1.0, index=other.index)
        else:
            er[tag] = loaded[f"E{tag}"]
        er[tag] = er[tag].where(er[tag] > 0)  # non-positive rates are invalid

    df = pd.DataFrame({"E1": er["1"], "E2": er["2"], "P1": loaded["P1"], "P2": loaded["P2"]})
    df = df.sort_index()
    df["S"] = df["E2"] / df["E1"]               # Country 2 units per Country 1 unit
    df["RER"] = df["S"] * df["P1"] / df["P2"]
    return df[["S", "P1", "P2", "RER"]], [], coverage


def compute_sample(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp):
    """Slice the sample, compute RER* and the Real Index. Returns (sample, rer_star)."""
    months = pd.date_range(start, end, freq="MS")
    sample = df.reindex(months).copy()
    sample.index.name = "date"
    valid = sample[["S", "P1", "P2", "RER"]].notna().all(axis=1)
    sample["valid"] = valid
    rer_star = sample.loc[valid, "RER"].mean() if valid.any() else float("nan")
    sample["RER_star"] = rer_star
    sample["Real_Index"] = 100 * sample["RER"].where(valid) / rer_star
    return sample, rer_star


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------

def make_chart(sample: pd.DataFrame, mode: str, label1: str, label2: str) -> go.Figure:
    """Build the Plotly figure for Real, Nominal or Both."""
    real_name = "Real Index (RER* = 100)"
    nom_name = f"S(t): {label2} per 1 {label1}"
    real_color, nom_color = "#1f77b4", "#d62728"

    real = go.Scatter(x=sample.index, y=sample["Real_Index"], name=real_name,
                      mode="lines", line=dict(color=real_color, width=2))
    nominal = go.Scatter(x=sample.index, y=sample["S"].where(sample["valid"]), name=nom_name,
                         mode="lines", line=dict(color=nom_color, width=2))

    if mode == "Both":
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        fig.add_trace(real, secondary_y=False)
        fig.add_trace(nominal, secondary_y=True)
        fig.update_yaxes(title_text=real_name, secondary_y=False, title_font_color=real_color)
        fig.update_yaxes(title_text=nom_name, secondary_y=True, title_font_color=nom_color,
                         showgrid=False)
    else:
        fig = go.Figure(real if mode == "Real" else nominal)
        if mode == "Real":
            fig.add_hline(y=100, line_dash="dot", line_color="gray",
                          annotation_text="RER* = 100")
            fig.update_yaxes(title_text=real_name)
        else:
            fig.update_yaxes(title_text=nom_name)

    fig.update_layout(hovermode="x unified", height=520, margin=dict(t=30, b=40),
                      legend=dict(orientation="h", y=1.08, x=0), xaxis_title="Month")
    return fig


# ---------------------------------------------------------------------------
# Streamlit interface
# ---------------------------------------------------------------------------

def month_label(ts: pd.Timestamp) -> str:
    return ts.strftime("%b %Y")


def main() -> None:
    st.set_page_config(page_title="Real vs Nominal Exchange Rates", page_icon="💱", layout="wide")
    st.title("💱 Real vs. Nominal Exchange Rates")
    st.caption("Monthly data from the IMF (CPI and exchange rates). "
               "RER(t) = S(t) × P1(t) / P2(t), where S(t) is Country 2 currency per 1 unit of Country 1 currency.")

    # ---- Country selectors -------------------------------------------------
    countries, live = fetch_country_list()
    if not live:
        st.warning("Could not download the IMF country list, so a built-in list of "
                   "major economies is shown instead.")
    codes = list(countries)
    label_of = lambda c: f"{countries[c]} ({c})"
    default1 = codes.index("USA") if "USA" in codes else 0
    default2 = codes.index(EURO_AREA) if EURO_AREA in codes else 0

    c1, c2 = st.columns(2)
    iso1 = c1.selectbox("Country 1 (base currency)", codes, index=default1, format_func=label_of)
    iso2 = c2.selectbox("Country 2", codes, index=default2, format_func=label_of)

    if iso1 == iso2:
        st.info("Please choose two different countries.")
        st.stop()

    # ---- Download and combine data ----------------------------------------
    with st.spinner("Fetching data from the IMF..."):
        df, problems, coverage = build_dataset(iso1, iso2, countries)

    if df is None:
        st.error("Data could not be loaded for this pair:")
        for p in problems:
            st.markdown(f"- {p}")
        if coverage:
            with st.expander("Series that did load"):
                st.dataframe(pd.DataFrame(coverage), hide_index=True)
        st.stop()

    valid_months = df.dropna().index
    if valid_months.empty:
        st.error("Both countries have data, but their available months do not overlap, "
                 "so the real exchange rate cannot be computed.")
        st.dataframe(pd.DataFrame(coverage), hide_index=True)
        st.stop()

    # ---- Sample period -----------------------------------------------------
    options = list(pd.date_range(valid_months.min(), valid_months.max(), freq="MS"))
    s_col, e_col, m_col = st.columns([1, 1, 1.2])
    start = s_col.selectbox("Start month", options, index=0, format_func=month_label,
                            key=f"start_{iso1}_{iso2}")
    end = e_col.selectbox("End month", options, index=len(options) - 1, format_func=month_label,
                          key=f"end_{iso1}_{iso2}")
    mode = m_col.radio("Series to plot", ["Real", "Nominal", "Both"], horizontal=True, index=2)

    if start > end:
        st.error("The start month must not be after the end month.")
        st.stop()

    # ---- Calculations (re-run every time the dates change) ----------------
    sample, rer_star = compute_sample(df, start, end)
    n_valid = int(sample["valid"].sum())
    if n_valid == 0:
        st.error("There are no months with complete data in the selected period.")
        st.stop()

    n_missing = len(sample) - n_valid
    if n_missing:
        st.warning(f"{n_missing} of {len(sample)} months in this sample are missing data "
                   "and are left out of RER* and the charts.")

    last_valid = sample[sample["valid"]].iloc[-1]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("RER* (sample mean of RER)", f"{rer_star:,.4f}")
    m2.metric("Valid months", f"{n_valid}")
    m3.metric(f"Latest S ({month_label(sample[sample['valid']].index[-1])})", f"{last_valid['S']:,.4f}")
    m4.metric("Latest Real Index", f"{last_valid['Real_Index']:,.1f}")

    # ---- Chart ------------------------------------------------------------
    st.plotly_chart(make_chart(sample, mode, iso1, iso2), use_container_width=True)
    st.caption("A Real Index above 100 means Country 1's currency is stronger in real terms than "
               "its sample average (Country 1 goods are relatively expensive); below 100 means weaker.")

    # ---- CSV download -----------------------------------------------------
    out = sample.reset_index()[["date", "S", "P1", "P2", "RER", "RER_star", "Real_Index", "valid"]]
    out["date"] = out["date"].dt.strftime("%Y-%m-%d")
    st.download_button(
        "⬇️ Download CSV",
        data=out.to_csv(index=False).encode("utf-8"),
        file_name=f"rer_{iso1}_{iso2}_{start:%Y%m}_{end:%Y%m}.csv",
        mime="text/csv",
    )

    with st.expander("Data coverage and preview"):
        st.dataframe(pd.DataFrame(coverage), hide_index=True)
        st.dataframe(out, hide_index=True)


if __name__ == "__main__":
    main()
