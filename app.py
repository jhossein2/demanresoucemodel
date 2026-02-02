import streamlit as st
import pandas as pd

# Import your model as a black box
from model.workforce_planning_v5_1 import (
    CONFIG,
    load_forecast,
    apply_forecast_scale,
    run_simulation,
    create_monthly_summary
)

st.set_page_config(
    page_title="Workforce Planning Studio",
    layout="wide"
)

st.title("📞 Workforce Planning Studio")
st.caption("Executive view — peak-hour risk, 2026–2032")

# ---------------------------
# EXECUTIVE MODE NOTICE
# ---------------------------
st.info(
    "This is a decision-support model. "
    "Assumptions are locked; month selection filters results only."
)

# ---------------------------
# LOAD & RUN MODEL (ONCE)
# ---------------------------
@st.cache_data
def run_model():
    forecasts = load_forecast(CONFIG['forecast_file'], CONFIG['service'])
    forecasts = apply_forecast_scale(forecasts, CONFIG['forecast_scale'])
    bucket_rows, _ = run_simulation(forecasts, CONFIG)
    df = pd.DataFrame(bucket_rows)
    monthly_df = create_monthly_summary(df)
    return df, monthly_df

bucket_df, monthly_df = run_model()

# ---------------------------
# EXECUTIVE SUMMARY
# ---------------------------
st.subheader("🚨 Executive Summary")

first_breach = monthly_df[monthly_df['zone'].isin(
    ["SLA Breach", "Floor Breach"]
)]

if len(first_breach) > 0:
    fb = first_breach.iloc[0]
    st.error(
        f"First breach: **{fb['month_name']} {fb['year']}** "
        f"({fb['zone']} – {fb['worst_bucket']})"
    )
else:
    st.success("No SLA or compliance breach in forecast horizon.")

kpi_cols = st.columns(4)
kpi_cols[0].metric("Worst SLA", f"{monthly_df['min_sla'].min():.0%}")
kpi_cols[1].metric("Peak Occupancy", f"{monthly_df['max_occupancy'].max():.0%}")
kpi_cols[2].metric("Min Chairs Available", int(monthly_df['n_available'].min()))
kpi_cols[3].metric("Peak FTE Required", int(monthly_df['fte_required_peak'].max()))

# ---------------------------
# MONTH SELECTION
# ---------------------------
st.subheader("📅 Monthly Detail")

years = sorted(monthly_df['year'].unique())
year = st.selectbox("Year", years, index=0)

months = monthly_df[monthly_df['year'] == year][['month', 'month_name']]
month_label = st.selectbox(
    "Month",
    months['month_name'].tolist()
)

selected_month = months[months['month_name'] == month_label]['month'].iloc[0]

# ---------------------------
# FILTER DATA
# ---------------------------
month_summary = monthly_df[
    (monthly_df['year'] == year) &
    (monthly_df['month'] == selected_month)
].iloc[0]

month_buckets = bucket_df[
    (bucket_df['year'] == year) &
    (bucket_df['month'] == selected_month)
]

st.markdown(
    f"**Showing details for {month_label} {year} "
    f"(worst bucket: {month_summary['worst_bucket']})**"
)

# ---------------------------
# MONTHLY KPIs
# ---------------------------
cols = st.columns(4)
cols[0].metric("Min SLA", f"{month_summary['min_sla']:.0%}")
cols[1].metric("Max Occupancy", f"{month_summary['max_occupancy']:.0%}")
cols[2].metric("Chairs Required", int(month_summary['n_required']))
cols[3].metric("Chairs Available", int(month_summary['n_available']))

# ---------------------------
# BUCKET TABLE
# ---------------------------
st.subheader("📊 Bucket Breakdown")

display_cols = [
    'bucket',
    'calls_per_hour',
    'erlangs',
    'n_required',
    'n_available',
    'n_deployed',
    'achievable_sla',
    'agent_occupancy',
    'binding_shift',
    'zone'
]

st.dataframe(
    month_buckets[display_cols]
        .sort_values('bucket'),
    use_container_width=True
)
