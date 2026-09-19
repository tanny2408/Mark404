import json
import os
import tempfile
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import trackaccess as engine


# ============================================================
# Streamlit page config
# ============================================================

st.set_page_config(
    page_title="Railway Track Access Optimiser",
    page_icon="🚆",
    layout="wide",
)

REQUIRED_FILES = [
    "01_LINES.csv",
    "02_STATIONS.csv",
    "03_SECTORS.csv",
    "04_LOCATION_SUPPLY.csv",
    "05_BUFFER_LOCATION.csv",
    "06_PARAMETERS.csv",
    "07_PROJECT_DETAILS.csv",
    "08_ACTIVITY_DETAILS.csv",
]

PRIORITY_COLORS = {
    1: "#c7354d",
    2: "#e08a27",
    3: "#4a78b2",
}

APP_DIR = Path(__file__).resolve().parent
BUNDLED_DATA_DIR = APP_DIR.parent / "01_data"


# ============================================================
# Helpers
# ============================================================

def csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8")


def json_bytes(obj) -> bytes:
    return json.dumps(obj, indent=2, default=str).encode("utf-8")


def has_complete_instance(folder: Path) -> bool:
    return all((folder / name).is_file() for name in REQUIRED_FILES)


def save_uploaded_files(uploaded_files):
    """
    Save uploaded files to an isolated temporary directory.
    """
    if "uploaded_data_dir" not in st.session_state:
        st.session_state.uploaded_data_dir = tempfile.mkdtemp(
            prefix="railway_track_access_"
        )

    data_dir = Path(st.session_state.uploaded_data_dir)

    for uploaded in uploaded_files or []:
        if uploaded.name in REQUIRED_FILES:
            with open(data_dir / uploaded.name, "wb") as f:
                f.write(uploaded.getbuffer())

    missing = [
        name for name in REQUIRED_FILES
        if not (data_dir / name).is_file()
    ]

    return data_dir, missing


def load_instance_safe(data_dir):
    try:
        return engine.load_instance(str(data_dir)), None
    except BaseException as exc:
        # The optimisation engine sometimes raises SystemExit for invalid input.
        return None, exc


def solve_engine(inst, scenario, time_limit):
    """
    Adapter for the current trackaccess.py engine.
    """
    if hasattr(engine, "solve_with_growing_horizon"):
        return engine.solve_with_growing_horizon(
            inst,
            scenario,
            float(time_limit),
        )

    # Compatibility with older versions of the engine.
    if hasattr(engine, "solve_with_autoextend"):
        return engine.solve_with_autoextend(
            inst,
            scenario,
            time_limit=float(time_limit),
            verbose=False,
        )

    raise RuntimeError(
        "No supported solver entry point found in trackaccess.py. "
        "Expected solve_with_growing_horizon()."
    )


def get_week_start(inst, week):
    base = int(engine.CONFIG.get("week_base", 1))
    return (
        pd.Timestamp(inst["h0"])
        + pd.Timedelta(days=7 * (int(week) - base))
    )


def get_week_end(inst, week):
    return get_week_start(inst, week) + pd.Timedelta(days=6)


def render_metric_row(report):
    soft = report.get("soft_scores", {})

    cols = st.columns(6)

    cols[0].metric(
        "Feasible",
        "Yes" if report.get("feasible", False) else "No",
    )

    cols[1].metric(
        "Hard violations",
        len(report.get("hard_violations", [])),
    )

    cols[2].metric(
        "Overrun days",
        soft.get("overrun_days_total", 0),
    )

    cols[3].metric(
        "Excess access nights",
        soft.get("excess_access_nights_total", 0),
    )

    cols[4].metric(
        "ECLO nights",
        soft.get("eclo_nights_total", 0),
    )

    cols[5].metric(
        "Objective score",
        soft.get("objective_score", "—"),
    )


# ============================================================
# Visualisations
# ============================================================

def build_schedule_timeline(
    inst,
    sub,
    scenario,
    selected_priorities=None,
    selected_contracts=None,
):
    access = sub["access"].copy()

    if access.empty:
        fig = go.Figure()
        fig.update_layout(
            title="No scheduled access nights",
            template="plotly_white",
            height=400,
        )
        return fig

    access["week"] = access["week"].astype(int)
    access["eclo"] = access["eclo"].astype(int)

    records = []

    for row in access.itertuples():
        activity_id = row.activity_id
        activity = inst["act"][activity_id]
        contract = activity["contract"]
        priority = int(inst["tier"][contract])

        start_date = get_week_start(inst, row.week)
        end_date = start_date + pd.Timedelta(days=5)

        planned_completion = pd.Timestamp(
            inst["planned_date"][contract]
        )

        # Keep this aligned with the engine:
        # completion is based on the END of the scheduled week.
        week_end_date = get_week_end(inst, row.week)

        overrun = (
            week_end_date.date()
            > planned_completion.date()
        )

        records.append(
            {
                "activity_id": activity_id,
                "contract": contract,
                "priority": priority,
                "week": int(row.week),
                "access_night": int(row.access_night),
                "eclo": int(row.eclo),
                "start": start_date,
                "end": end_date,
                "week_end": week_end_date,
                "planned_completion": planned_completion,
                "overrun": overrun,
            }
        )

    df = pd.DataFrame(records)

    if selected_priorities:
        df = df[df["priority"].isin(selected_priorities)]

    if selected_contracts:
        df = df[df["contract"].isin(selected_contracts)]

    if df.empty:
        fig = go.Figure()
        fig.update_layout(
            title="No activities match the selected filters",
            template="plotly_white",
            height=400,
        )
        return fig

    activity_meta = (
        df[
            [
                "activity_id",
                "contract",
                "priority",
                "planned_completion",
            ]
        ]
        .drop_duplicates()
        .sort_values(
            ["priority", "contract", "activity_id"]
        )
        .reset_index(drop=True)
    )

    activity_meta["label"] = activity_meta.apply(
        lambda r: (
            f"{r['activity_id']} · "
            f"{r['contract']} · "
            f"P{r['priority']}"
        ),
        axis=1,
    )

    y_map = {
        row.activity_id: i
        for i, row in enumerate(
            activity_meta.itertuples()
        )
    }

    fig = go.Figure()

    # --------------------------------------------------------
    # Access-night blocks
    # --------------------------------------------------------

    for row in df.itertuples():
        y = y_map[row.activity_id]
        color = PRIORITY_COLORS.get(
            row.priority,
            "#4a78b2",
        )

        # ECLO = stronger dark border
        line_color = (
            "#222222"
            if row.eclo
            else color
        )

        line_width = (
            2.4
            if row.eclo
            else 1.0
        )

        fig.add_shape(
            type="rect",
            x0=row.start,
            x1=row.end,
            y0=y - 0.32,
            y1=y + 0.32,
            fillcolor=color,
            opacity=0.95,
            line=dict(
                color=line_color,
                width=line_width,
            ),
            layer="below",
        )

        # Overrun highlight
        if row.overrun:
            fig.add_shape(
                type="rect",
                x0=row.start,
                x1=row.end,
                y0=y - 0.37,
                y1=y + 0.37,
                fillcolor="rgba(0,0,0,0)",
                line=dict(
                    color="#f1b6c1",
                    width=2.4,
                ),
                layer="above",
            )

        # Invisible hover point
        fig.add_trace(
            go.Scatter(
                x=[
                    row.start
                    + (row.end - row.start) / 2
                ],
                y=[y],
                mode="markers",
                marker=dict(
                    size=20,
                    color="rgba(0,0,0,0)",
                ),
                hovertemplate=(
                    f"<b>{row.activity_id}</b><br>"
                    f"Contract: {row.contract}<br>"
                    f"Priority: P{row.priority}<br>"
                    f"Week: {row.week}<br>"
                    f"Access night: {row.access_night}<br>"
                    f"ECLO: {'Yes' if row.eclo else 'No'}<br>"
                    f"Planned completion: "
                    f"{row.planned_completion.date()}<br>"
                    f"Week end: {row.week_end.date()}<br>"
                    f"Past planned completion: "
                    f"{'Yes' if row.overrun else 'No'}"
                    "<extra></extra>"
                ),
                showlegend=False,
            )
        )

    # --------------------------------------------------------
    # Planned completion markers
    # --------------------------------------------------------

    for row in activity_meta.itertuples():
        y = y_map[row.activity_id]

        fig.add_shape(
            type="line",
            x0=row.planned_completion,
            x1=row.planned_completion,
            y0=y - 0.44,
            y1=y + 0.44,
            line=dict(
                color="black",
                width=1.5,
                dash="dash",
            ),
            layer="above",
        )

    # --------------------------------------------------------
    # Legend
    # --------------------------------------------------------

    for priority, color in PRIORITY_COLORS.items():
        fig.add_trace(
            go.Scatter(
                x=[None],
                y=[None],
                mode="markers",
                marker=dict(
                    symbol="square",
                    size=14,
                    color=color,
                ),
                name=f"Priority {priority}",
            )
        )

    fig.add_trace(
        go.Scatter(
            x=[None],
            y=[None],
            mode="markers",
            marker=dict(
                symbol="square",
                size=14,
                color="white",
                line=dict(
                    color="#222222",
                    width=2,
                ),
            ),
            name="ECLO",
        )
    )

    fig.add_trace(
        go.Scatter(
            x=[None],
            y=[None],
            mode="markers",
            marker=dict(
                symbol="square",
                size=14,
                color="white",
                line=dict(
                    color="#f1b6c1",
                    width=2,
                ),
            ),
            name="Past planned date",
        )
    )

    fig.add_trace(
        go.Scatter(
            x=[None],
            y=[None],
            mode="lines",
            line=dict(
                color="black",
                width=2,
                dash="dash",
            ),
            name="Planned completion",
        )
    )

    labels = activity_meta["label"].tolist()

    fig.update_layout(
        title=(
            f"Scenario {scenario} — "
            "Access Nights by Activity"
        ),
        template="plotly_white",
        height=max(
            700,
            28 * len(labels) + 170,
        ),
        hovermode="closest",
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            x=0,
        ),
        margin=dict(
            l=190,
            r=30,
            t=100,
            b=60,
        ),
        xaxis=dict(
            title="Calendar time",
            type="date",
            showgrid=True,
            rangeslider=dict(visible=False),
        ),
        yaxis=dict(
            tickmode="array",
            tickvals=list(range(len(labels))),
            ticktext=labels,
            autorange="reversed",
            title=None,
            showgrid=True,
        ),
    )

    return fig


def build_capacity_heatmap(inst, sub):
    """
    Capacity is measured in POSSESSIONS, not activity rows.

    One co_share_group = one possession.
    """
    occ = sub["occupancy"].copy()

    if occ.empty:
        fig = go.Figure()
        fig.update_layout(
            title="No occupancy data",
            template="plotly_white",
            height=400,
        )
        return fig

    usage = (
        occ.groupby(
            ["location_id", "week"]
        )["co_share_group"]
        .nunique()
        .reset_index(name="possessions")
    )

    usage["week"] = usage["week"].astype(int)

    def capacity_for(row):
        try:
            return int(
                engine.supply_at(
                    inst,
                    row["location_id"],
                    int(row["week"]),
                )
            )
        except Exception:
            return int(
                inst["cap"].get(
                    row["location_id"],
                    0,
                )
            )

    usage["capacity"] = usage.apply(
        capacity_for,
        axis=1,
    )

    usage["load_ratio"] = usage.apply(
        lambda r: (
            r["possessions"] / r["capacity"]
            if r["capacity"] > 0
            else (
                float(r["possessions"])
                if r["possessions"] > 0
                else 0.0
            )
        ),
        axis=1,
    )

    busiest = (
        usage.groupby("location_id")[
            "load_ratio"
        ]
        .max()
        .sort_values(ascending=False)
        .head(30)
        .index
        .tolist()
    )

    filtered = usage[
        usage["location_id"].isin(busiest)
    ]

    ratio_pivot = (
        filtered.pivot(
            index="location_id",
            columns="week",
            values="load_ratio",
        )
        .fillna(0.0)
        .reindex(busiest)
    )

    # Custom hover text lets us show the possession count
    # and capacity, not only the ratio.
    possession_pivot = (
        filtered.pivot(
            index="location_id",
            columns="week",
            values="possessions",
        )
        .fillna(0)
        .reindex(
            index=busiest,
            columns=ratio_pivot.columns,
        )
    )

    capacity_pivot = (
        filtered.pivot(
            index="location_id",
            columns="week",
            values="capacity",
        )
        .fillna(0)
        .reindex(
            index=busiest,
            columns=ratio_pivot.columns,
        )
    )

    customdata = []

    for i in range(
        len(ratio_pivot.index)
    ):
        row = []
        for j in range(
            len(ratio_pivot.columns)
        ):
            row.append(
                [
                    int(
                        possession_pivot.iloc[
                            i, j
                        ]
                    ),
                    int(
                        capacity_pivot.iloc[
                            i, j
                        ]
                    ),
                ]
            )
        customdata.append(row)

    fig = go.Figure(
        data=go.Heatmap(
            z=ratio_pivot.values,
            x=list(ratio_pivot.columns),
            y=list(ratio_pivot.index),
            customdata=customdata,
            colorscale="YlOrRd",
            zmin=0,
            colorbar=dict(
                title="Possessions / Capacity"
            ),
            hovertemplate=(
                "Location: %{y}<br>"
                "Week: %{x}<br>"
                "Possessions: %{customdata[0]}<br>"
                "Capacity: %{customdata[1]}<br>"
                "Load ratio: %{z:.2f}"
                "<extra></extra>"
            ),
        )
    )

    fig.update_layout(
        title=(
            "Capacity Heatmap "
            "(Top 30 Busiest Locations)"
        ),
        template="plotly_white",
        height=max(
            500,
            18 * len(
                ratio_pivot.index
            )
            + 160,
        ),
        xaxis_title="Week",
        yaxis_title="Location",
        margin=dict(
            l=240,
            r=30,
            t=70,
            b=60,
        ),
    )

    return fig


def build_contract_overrun_chart(inst, sub):
    results = sub["results"].copy()

    if results.empty:
        fig = go.Figure()
        fig.update_layout(
            title="No contract result data",
            template="plotly_white",
            height=400,
        )
        return fig

    results["overrun_days"] = (
        results["overrun_days"]
        .astype(int)
    )

    results["priority"] = (
        results["contract_number"]
        .map(inst["tier"])
        .astype(int)
    )

    results = results.sort_values(
        [
            "overrun_days",
            "priority",
            "contract_number",
        ],
        ascending=[
            False,
            True,
            True,
        ],
    )

    colors = [
        PRIORITY_COLORS.get(
            int(p),
            "#4a78b2",
        )
        for p in results["priority"]
    ]

    fig = go.Figure(
        data=[
            go.Bar(
                x=results[
                    "contract_number"
                ],
                y=results[
                    "overrun_days"
                ],
                marker_color=colors,
                customdata=results[
                    [
                        "priority",
                        "simulated_completion_date",
                    ]
                ].values,
                hovertemplate=(
                    "Contract: %{x}<br>"
                    "Priority: P%{customdata[0]}<br>"
                    "Overrun: %{y} days<br>"
                    "Simulated completion: "
                    "%{customdata[1]}"
                    "<extra></extra>"
                ),
            )
        ]
    )

    fig.update_layout(
        title="Contract Completion Overrun",
        template="plotly_white",
        xaxis_title="Contract",
        yaxis_title="Overrun days",
        height=450,
        margin=dict(
            l=60,
            r=30,
            t=70,
            b=60,
        ),
        showlegend=False,
    )

    return fig


# ============================================================
# Solution renderer
# ============================================================

def render_solution(
    sub,
    report,
    meta,
    inst,
    scenario,
    key_prefix,
):
    render_metric_row(report)

    c1, c2, c3, c4 = st.columns(4)

    c1.info(
        f"CP-SAT status: "
        f"**{meta.get('status', 'Unknown')}**"
    )

    c2.info(
        f"Solver time: "
        f"**{float(meta.get('seconds', 0)):.1f}s**"
    )

    c3.info(
        f"Objective bound: "
        f"**{meta.get('bound', '—')}**"
    )

    c4.info(
        f"Scenario: **{scenario}**"
    )

    hard_violations = report.get(
        "hard_violations",
        [],
    )

    if hard_violations:
        st.error(
            "The generated schedule contains "
            "hard violations according to the "
            "built-in validator."
        )

        st.dataframe(
            pd.DataFrame(
                hard_violations
            ),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.success(
            "The schedule passed the "
            "built-in validator."
        )

    # --------------------------------------------------------
    # Visual analytics
    # --------------------------------------------------------

    st.subheader("Visual Analytics")

    filter_left, filter_right = st.columns(
        [1, 2]
    )

    contract_values = sorted(
        sub["results"][
            "contract_number"
        ]
        .astype(str)
        .unique()
        .tolist()
    )

    priority_values = sorted(
        {
            int(inst["tier"][cn])
            for cn in contract_values
        }
    )

    with filter_left:
        selected_priorities = st.multiselect(
            "Priority",
            options=priority_values,
            default=priority_values,
            key=(
                f"{key_prefix}_priority_filter"
            ),
        )

    with filter_right:
        selected_contracts = st.multiselect(
            "Contracts",
            options=contract_values,
            default=[],
            help=(
                "Leave empty to show "
                "all contracts."
            ),
            key=(
                f"{key_prefix}_contract_filter"
            ),
        )

    timeline = build_schedule_timeline(
        inst=inst,
        sub=sub,
        scenario=scenario,
        selected_priorities=(
            selected_priorities
        ),
        selected_contracts=(
            selected_contracts
        ),
    )

    st.plotly_chart(
        timeline,
        use_container_width=True,
        key=f"{key_prefix}_timeline",
    )

    viz1, viz2 = st.tabs(
        [
            "Capacity Heatmap",
            "Contract Performance",
        ]
    )

    with viz1:
        st.caption(
            "Capacity is calculated from "
            "co-share possession groups, "
            "not raw activity rows."
        )

        st.plotly_chart(
            build_capacity_heatmap(
                inst,
                sub,
            ),
            use_container_width=True,
            key=(
                f"{key_prefix}_heatmap"
            ),
        )

    with viz2:
        st.plotly_chart(
            build_contract_overrun_chart(
                inst,
                sub,
            ),
            use_container_width=True,
            key=(
                f"{key_prefix}_contract_chart"
            ),
        )

    # --------------------------------------------------------
    # Raw outputs
    # --------------------------------------------------------

    st.subheader("Submission Data")

    tab_access, tab_occ, tab_results, tab_diag = st.tabs(
        [
            "Access Schedule",
            "Occupancy",
            "Contract Results",
            "Diagnostics",
        ]
    )

    with tab_access:
        st.dataframe(
            sub["access"],
            use_container_width=True,
            hide_index=True,
        )

    with tab_occ:
        st.dataframe(
            sub["occupancy"],
            use_container_width=True,
            hide_index=True,
        )

    with tab_results:
        st.dataframe(
            sub["results"],
            use_container_width=True,
            hide_index=True,
        )

    with tab_diag:
        st.markdown(
            "#### Soft Scores"
        )

        st.json(
            report.get(
                "soft_scores",
                {},
            )
        )

        hotspots = (
            report.get("detail", {})
            .get(
                "capacity_hotspots",
                [],
            )
        )

        if hotspots:
            st.markdown(
                "#### Capacity Hotspots"
            )

            st.dataframe(
                pd.DataFrame(
                    hotspots
                ),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info(
                "No capacity hotspots "
                "were reported."
            )

        with st.expander(
            "Raw solver metadata"
        ):
            st.json(meta)

    # --------------------------------------------------------
    # Downloads
    # --------------------------------------------------------

    st.subheader(
        "Download Submission"
    )

    d1, d2, d3, d4 = st.columns(4)

    d1.download_button(
        "SCHEDULE_ACCESS.csv",
        csv_bytes(sub["access"]),
        file_name=(
            "SCHEDULE_ACCESS.csv"
        ),
        mime="text/csv",
        use_container_width=True,
        key=(
            f"{key_prefix}_download_access"
        ),
    )

    d2.download_button(
        "SCHEDULE_OCCUPANCY.csv",
        csv_bytes(
            sub["occupancy"]
        ),
        file_name=(
            "SCHEDULE_OCCUPANCY.csv"
        ),
        mime="text/csv",
        use_container_width=True,
        key=(
            f"{key_prefix}_download_occ"
        ),
    )

    d3.download_button(
        "RESULTS.csv",
        csv_bytes(sub["results"]),
        file_name="RESULTS.csv",
        mime="text/csv",
        use_container_width=True,
        key=(
            f"{key_prefix}_download_results"
        ),
    )

    d4.download_button(
        "REPORT.json",
        json_bytes(report),
        file_name="REPORT.json",
        mime="application/json",
        use_container_width=True,
        key=(
            f"{key_prefix}_download_report"
        ),
    )


# ============================================================
# Main UI
# ============================================================

st.title(
    "🚆 Railway Track Access Optimiser"
)

st.caption(
    "Interactive decision-support "
    "dashboard for railway possession "
    "scheduling using OR-Tools CP-SAT."
)

with st.sidebar:
    st.header("Solver Settings")

    time_limit = st.number_input(
        "Time limit (seconds)",
        min_value=5,
        max_value=1800,
        value=180,
        step=5,
    )

    workers = st.number_input(
        "CP-SAT workers",
        min_value=1,
        max_value=64,
        value=int(
            engine.CONFIG.get(
                "workers",
                8,
            )
        ),
        step=1,
    )

    engine.CONFIG["workers"] = (
        int(workers)
    )

    extra_weeks = st.number_input(
        "Initial extra horizon (weeks)",
        min_value=0,
        max_value=int(
            engine.CONFIG.get(
                "max_extra_weeks",
                64,
            )
        ),
        value=int(
            engine.CONFIG.get(
                "extra_weeks",
                8,
            )
        ),
        step=1,
    )

    engine.CONFIG["extra_weeks"] = (
        int(extra_weeks)
    )

    st.divider()

    st.markdown(
        """
**Workflow**

1. Select bundled data or upload CSVs.
2. Choose Scenario A, B or C.
3. Run the optimiser.
4. Inspect the timeline and capacity views.
5. Download the submission files.
        """
    )


# ============================================================
# Data source
# ============================================================

st.header("1. Input Data")

bundled_available = (
    has_complete_instance(
        BUNDLED_DATA_DIR
    )
)

source_options = ["Upload CSV files"]

if bundled_available:
    source_options.insert(
        0,
        "Use bundled PS1/01_data",
    )

data_source = st.radio(
    "Choose data source",
    options=source_options,
    horizontal=True,
)

if (
    data_source
    == "Use bundled PS1/01_data"
):
    data_dir = BUNDLED_DATA_DIR
    missing = []

    st.success(
        f"Using bundled data: "
        f"`{data_dir}`"
    )

else:
    uploaded_files = st.file_uploader(
        "Upload all 8 PS1 CSV files",
        type=["csv"],
        accept_multiple_files=True,
    )

    data_dir, missing = (
        save_uploaded_files(
            uploaded_files
        )
    )

    if missing:
        st.warning(
            "Missing files: "
            + ", ".join(missing)
        )
        st.stop()

    st.success(
        "All eight input files "
        "are available."
    )


# ============================================================
# Load instance
# ============================================================

inst, load_error = load_instance_safe(
    data_dir
)

if load_error is not None:
    st.error(
        "Could not load the instance."
    )
    st.exception(load_error)
    st.stop()


# ============================================================
# Overview
# ============================================================

st.header("2. Instance Overview")

m1, m2, m3, m4 = st.columns(4)

m1.metric(
    "Activities",
    len(inst["A"]),
)

m2.metric(
    "Contracts",
    len(inst["contracts"]),
)

m3.metric(
    "Locations",
    len(inst["cap"]),
)

m4.metric(
    "Planning horizon",
    f"{inst['horizon_weeks']} weeks",
)

with st.expander(
    "View input tables"
):
    tabs = st.tabs(
        [
            "Lines",
            "Stations",
            "Sectors",
            "Location Supply",
            "Buffer Rules",
            "Contracts",
            "Activities",
        ]
    )

    with tabs[0]:
        st.dataframe(
            inst["lines"],
            use_container_width=True,
            hide_index=True,
        )

    with tabs[1]:
        st.dataframe(
            inst["stations"],
            use_container_width=True,
            hide_index=True,
        )

    with tabs[2]:
        st.dataframe(
            inst["sectors"],
            use_container_width=True,
            hide_index=True,
        )

    with tabs[3]:
        st.dataframe(
            inst["supply"],
            use_container_width=True,
            hide_index=True,
        )

    with tabs[4]:
        st.dataframe(
            inst["buffers"],
            use_container_width=True,
            hide_index=True,
        )

    with tabs[5]:
        st.dataframe(
            inst["contracts"].reset_index(),
            use_container_width=True,
            hide_index=True,
        )

    with tabs[6]:
        st.dataframe(
            inst["acts"].reset_index(),
            use_container_width=True,
            hide_index=True,
        )


# ============================================================
# Optimisation
# ============================================================

st.header("3. Optimise")

scenario = st.radio(
    "Scenario",
    options=["A", "B", "C"],
    horizontal=True,
    format_func=lambda x: {
        "A": (
            "A — Capacity hard, "
            "delay allowed"
        ),
        "B": (
            "B — Deadline hard, "
            "flexibility allowed"
        ),
        "C": (
            "C — Balanced"
        ),
    }[x],
)

scenario_descriptions = {
    "A": (
        "ECLO is disabled. Capacity is "
        "hard and priority-weighted delay "
        "is minimised."
    ),
    "B": (
        "Planned completion is hard. "
        "The optimiser can use ECLO and "
        "capacity flexibility at a cost."
    ),
    "C": (
        "Balances priority-weighted delay, "
        "excess access and ECLO."
    ),
}

st.caption(
    scenario_descriptions[scenario]
)

if st.button(
    "Run Optimiser",
    type="primary",
    use_container_width=True,
):
    with st.spinner(
        f"Optimising Scenario "
        f"{scenario}..."
    ):
        try:
            # Reload so a previous solve does
            # not mutate this instance.
            solve_inst = engine.load_instance(
                str(data_dir)
            )

            sub, meta = solve_engine(
                solve_inst,
                scenario,
                time_limit,
            )

            if sub is None:
                st.session_state[
                    "solution"
                ] = None

                st.session_state[
                    "solve_error"
                ] = meta

            else:
                report = engine.validate(
                    solve_inst,
                    sub,
                )

                explanation = (
                    engine.explain(
                        solve_inst,
                        sub,
                    )
                )

                st.session_state[
                    "solution"
                ] = {
                    "scenario": scenario,
                    "sub": sub,
                    "meta": meta,
                    "report": report,
                    "explanation": explanation,
                    "inst": solve_inst,
                }

                st.session_state[
                    "solve_error"
                ] = None

        except BaseException as exc:
            st.session_state[
                "solution"
            ] = None

            st.session_state[
                "solve_error"
            ] = {
                "error": str(exc),
                "type": (
                    type(exc).__name__
                ),
            }


# ============================================================
# Results
# ============================================================

solve_error = st.session_state.get(
    "solve_error"
)

if solve_error:
    st.error(
        "The optimiser did not "
        "produce a solution."
    )
    st.json(solve_error)


solution = st.session_state.get(
    "solution"
)

if solution:
    st.header(
        "4. Optimisation Results"
    )

    render_solution(
        sub=solution["sub"],
        report=solution["report"],
        meta=solution["meta"],
        inst=solution["inst"],
        scenario=solution["scenario"],
        key_prefix="main",
    )

    explanation = solution.get(
        "explanation"
    )

    if (
        explanation is not None
        and not explanation.empty
    ):
        st.subheader(
            "Delay Explanation"
        )

        st.dataframe(
            explanation,
            use_container_width=True,
            hide_index=True,
        )


st.divider()

st.caption(
    "Feasibility and safety checks are "
    "performed by the deterministic "
    "optimisation and validation engine."
)
