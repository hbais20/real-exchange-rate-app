# Real vs. Nominal Exchange Rate Explorer (IMF data)

A Streamlit app that compares the **nominal** and **real** exchange rate between
any two countries (or the Euro area) using monthly data from the IMF, with
interactive Plotly charts and a CSV export.

## What the app does

1. Lets you pick **Country 1** and **Country 2** (IMF countries plus the Euro area, code `G163`).
2. Lets you pick a **start month** and **end month** for the sample.
3. Downloads monthly CPI and exchange-rate data from the IMF (from January 1976 onward, where available).
4. Computes the nominal rate, the real exchange rate, the sample-average benchmark **RER\***, and a **Real Index** that equals 100 at RER\*.
5. Plots **Real**, **Nominal**, or **Both** (two separate y-axes).
6. Exports every observation and calculation in the sample to CSV.

RER\* is recomputed every time you change the start or end month.

## Formulas

| Symbol | Definition |
|---|---|
| `S(t)` | Units of Country 2 currency per 1 unit of Country 1 currency |
| `P1(t)`, `P2(t)` | Monthly CPI index (all items) of Country 1 and Country 2 |
| `RER(t)` | `S(t) × P1(t) / P2(t)` |
| `RER*` | Average of `RER(t)` over **valid months** in the selected sample |
| `Real Index(t)` | `100 × RER(t) / RER*` |

The IMF publishes exchange rates as *local currency units per USD*. With
`E1` and `E2` the per-USD rates of Country 1 and Country 2:

```
S(t) = E2(t) / E1(t)
```

For the United States, `E = 1` by definition (USD is the numeraire).

A **valid month** is one where `S`, `P1` and `P2` are all available. Only valid
months enter RER\*, the Real Index and the plotted lines. The nominal rate
S(t) is shown in actual currency units and is **not** normalized.

**Reading the Real Index:** above 100 means Country 1's currency is stronger in
real terms than its sample average (Country 1 goods are relatively expensive in
Country 2 currency terms); below 100 means weaker.

## CSV columns

`date, S, P1, P2, RER, RER_star, Real_Index, valid`

One row per month from the start to the end month, including months with
missing data (left blank, `valid = False`).

## Installation

Requires **Python 3.12**.

```bash
python3.12 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Running locally

```bash
streamlit run app.py
```

Your browser will open at <http://localhost:8501>.

## Data source

International Monetary Fund (IMF) SDMX API: <https://api.imf.org/external/sdmx/3.0>

- Monthly CPI: dataflow `IMF.STA:CPI`, key `{ISO3}.CPI._T.IX.M`
- Monthly exchange rate: dataflow `IMF.STA:ER`, key `{ISO3}.XDC_USD.PA_RT.M`
- CPI discovery (fallback): wildcard keys `{ISO3}.CPI.*.*.M` and `{ISO3}.*.*.IX.M` on the same dataflow
- Country list: codelist `IMF:CL_COUNTRY` (the Euro area is added as `G163`)

Responses are cached for 6 hours (country list: 24 hours) using `st.cache_data`.
If the 3.0 endpoint fails, the app falls back to the IMF's SDMX 2.1 endpoint on the same host.

## Euro area CPI may be unavailable

The IMF does not always publish an all-items CPI index for the Euro area
(`G163.CPI._T.IX.M` can come back as an empty dataset). The app handles this
automatically:

1. It first requests the exact key `{ISO3}.CPI._T.IX.M`.
2. If that returns nothing, it **discovers** what CPI series exist using
   SDMX wildcards: first `{ISO3}.CPI.*.*.M`, then `{ISO3}.*.*.IX.M`
   (any index type, e.g. a harmonised index).
3. From the discovered series it uses a **monthly index** series (transformation
   `IX`), preferring all-items (`_T`) and then the longest history. Growth-rate
   series are never used.
4. If a substitute series is used, the app shows a warning naming the exact
   series. If no usable series exists, the app shows a clear message and you can
   choose individual euro-area countries (e.g. Germany, France) instead.

The same fallback applies to every country's CPI, and a note is shown in the app
whenever the Euro area is selected. A **debug expander** shows the first 500
characters of each raw IMF response so you can see exactly what was returned.

## Handling missing data

- Failed API calls show an error message naming the series that could not be loaded; failures are not cached, so a rerun retries.
- Countries with no IMF data for a series get a clear message identifying which series is missing (for CPI, only after the wildcard discovery above also finds nothing).
- If the two countries' data do not overlap, the app says so and shows each series' coverage.
- Gaps inside the sample are reported (number of missing months) and left out of the calculations.
- If the IMF country list cannot be downloaded, a built-in list of major economies is used.

## Troubleshooting

- If the dropdowns show only major economies, the live country list could not be fetched.
- If the IMF changes dataflow names or endpoints, edit the constants at the top of `app.py` (`IMF_BASE`, `CPI_FLOW`, `ER_FLOW`, `CPI_KEY`, `ER_KEY`).
