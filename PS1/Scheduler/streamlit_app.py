import io
import json
import os
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

import trackaccess_corrected as engine


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


def dataframe_to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8")


def json_to_bytes(obj) -> bytes:
    return json.dumps(obj, indent=2, default=str).encode("utf-8")


def save_uploaded_files(uploaded_files) -> tuple[str, list[str]]:
    """Save uploaded CSVs to a temporary directory and return missing filenames."""
    if "data_dir" not in st.session_state:
        st.session_state.data_dir = tempfile.mkdtemp(prefix="trackaccess_")

    data_dir = st.session_state.data_dir

    uploaded_by_name = {f.name: f for f in uploaded_files or []}

    for name, uploaded in uploaded_by_name.items():
        if name in REQUIRED_FILES:
            with open(os.path.join(data_dir, name), "wb") as out:
                out.write(uploaded.getbuffer())

    missing = [
        name for name in REQUIRED_FILES
        if not os.path.isfile(os.path.join(data_dir, name))
    ]
    return data_dir, missing


def load_instance_safe(data_dir: str):
    try:
        return engine.load_instance(data_dir), None
    except Exception as exc:
        return None, exc


def render_metric_row(report: dict):
    soft = report["soft_scores"]
    cols = st.columns(6)
    cols[0].metric("Feasible", "Yes" if report["feasible"] else "No")
    cols[1].metric("Hard violations", len(report["hard_violations"]))
    cols[2].metric("Overrun days", soft.get("overrun_days_total", 0))
    cols[3].metric("Excess access nights", soft.get("excess_access_nights_total", 0))
    cols[4].metric("ECLO nights", soft.get("eclo_nights_total", 0))
    cols[5].metric("Objective score", soft.get("objective_score", "—"))


def render_solution(sub: dict, report: dict, meta: dict, key_prefix: str = ""):
    render_metric_row(report)

    status_cols = st.columns(3)
    status_cols[0].info(f"CP-SAT status: **{meta.get('status', 'Unknown')}**")
    status_cols[1].info(f"Solver time: **{meta.get('seconds', 0):.1f}s**")
    status_cols[2].info(f"Scenario: **{report.get('scenario', '—')}**")

    if report["hard_violations"]:
        st.error("The schedule contains hard violations.")
        st.dataframe(
            pd.DataFrame(report["hard_violations"]),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.success("The schedule passed the built-in validator.")

    tab_access, tab_occ, tab_results, tab_diag = st.tabs(
        ["Access Schedule", "Occupancy", "Contract Results", "Diagnostics"]
    )

    with tab_access:
        st.dataframe(sub["access"], use_container_width=True, hide_index=True)

    with tab_occ:
        st.dataframe(sub["occupancy"], use_container_width=True, hide_index=True)

    with tab_results:
        st.dataframe(sub["results"], use_container_width=True, hide_index=True)

    with tab_diag:
        soft = report.get("soft_scores", {})
        st.subheader("Soft Scores")
        st.json(soft)

        hotspots = report.get("detail", {}).get("capacity_hotspots", [])
        if hotspots:
            st.subheader("Capacity Hotspots")
            st.dataframe(pd.DataFrame(hotspots), use_container_width=True, hide_index=True)

    st.subheader("Download Submission")
    d1, d2, d3, d4 = st.columns(4)

    d1.download_button(
        "Download SCHEDULE_ACCESS.csv",
        dataframe_to_csv_bytes(sub["access"]),
        file_name="SCHEDULE_ACCESS.csv",
        mime="text/csv",
        key=f"{key_prefix}_download_access",
        use_container_width=True,
    )
    d2.download_button(
        "Download SCHEDULE_OCCUPANCY.csv",
        dataframe_to_csv_bytes(sub["occupancy"]),
        file_name="SCHEDULE_OCCUPANCY.csv",
        mime="text/csv",
        key=f"{key_prefix}_download_occ",
        use_container_width=True,
    )
    d3.download_button(
        "Download RESULTS.csv",
        dataframe_to_csv_bytes(sub["results"]),
        file_name="RESULTS.csv",
        mime="text/csv",
        key=f"{key_prefix}_download_results",
        use_container_width=True,
    )
    d4.download_button(
        "Download REPORT.json",
        json_to_bytes(report),
        file_name="REPORT.json",
        mime="application/json",
        key=f"{key_prefix}_download_report",
        use_container_width=True,
    )


st.title("🚆 Railway Track Access Optimiser")
st.caption(
    "Decision-support tool for railway possession scheduling across Scenarios A, B and C."
)

with st.sidebar:
    st.header("How to use")
    st.markdown(
        """
1. Upload all **8 input CSV files**.
2. Review the instance summary.
3. Select a scenario.
4. Run the optimiser.
5. Review feasibility, delays, ECLO usage and capacity.
6. Download the three submission CSVs.
        """
    )

    st.divider()

    st.subheader("Solver Settings")
    time_limit = st.number_input(
        "Time limit per solve (seconds)",
        min_value=5,
        max_value=1800,
        value=180,
        step=5,
    )

    workers = st.number_input(
        "CP-SAT workers",
        min_value=1,
        max_value=64,
        value=int(engine.CONFIG.get("workers", 8)),
        step=1,
    )
    engine.CONFIG["workers"] = int(workers)

    extra_weeks = st.number_input(
        "Starting overrun allowance (weeks)",
        min_value=0,
        max_value=64,
        value=int(engine.CONFIG.get("extra_weeks", 8)),
        step=1,
    )
    engine.CONFIG["extra_weeks"] = int(extra_weeks)


st.header("1. Upload Instance")

uploaded_files = st.file_uploader(
    "Upload the eight PS1 CSV files",
    type=["csv"],
    accept_multiple_files=True,
)

data_dir, missing = save_uploaded_files(uploaded_files)

if missing:
    st.warning(
        "Missing files: " + ", ".join(missing)
    )
    st.stop()

st.success("All eight input files are available.")

inst, load_error = load_instance_safe(data_dir)

if load_error is not None:
    st.error(f"Could not load the instance: {load_error}")
    st.stop()


st.header("2. Instance Overview")

c1, c2, c3, c4 = st.columns(4)
c1.metric("Activities", len(inst["A"]))
c2.metric("Contracts", len(inst["contracts"]))
c3.metric("Locations", len(inst["cap"]))
c4.metric("Planning horizon", f"{inst['horizon_weeks']} weeks")

with st.expander("View input data"):
    input_tabs = st.tabs(
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
    with input_tabs[0]:
        st.dataframe(inst["lines"], use_container_width=True, hide_index=True)
    with input_tabs[1]:
        st.dataframe(inst["stations"], use_container_width=True, hide_index=True)
    with input_tabs[2]:
        st.dataframe(inst["sectors"], use_container_width=True, hide_index=True)
    with input_tabs[3]:
        st.dataframe(inst["supply"], use_container_width=True, hide_index=True)
    with input_tabs[4]:
        st.dataframe(inst["buffers"], use_container_width=True, hide_index=True)
    with input_tabs[5]:
        st.dataframe(inst["contracts"].reset_index(), use_container_width=True, hide_index=True)
    with input_tabs[6]:
        st.dataframe(inst["acts"].reset_index(), use_container_width=True, hide_index=True)


st.header("3. Optimise")

scenario = st.radio(
    "Select scenario",
    options=["A", "B", "C"],
    horizontal=True,
    format_func=lambda s: {
        "A": "Scenario A — Capacity hard, delay allowed",
        "B": "Scenario B — Deadline hard, ECLO/capacity flexibility",
        "C": "Scenario C — Balanced",
    }[s],
)

scenario_help = {
    "A": "No ECLO. Capacity is rigid. The optimiser minimises priority-weighted delay.",
    "B": "Planned completion dates are hard. ECLO and capacity flexibility can be used at a cost.",
    "C": "Balances priority-weighted delay, excess access and ECLO usage.",
}
st.caption(scenario_help[scenario])

if st.button("Run Optimiser", type="primary", use_container_width=True):
    with st.spinner(f"Optimising Scenario {scenario}..."):
        try:
            # Reload the instance so repeated solves start from a clean state.
            inst = engine.load_instance(data_dir)

            sub, meta = engine.solve_with_autoextend(
                inst,
                scenario,
                time_limit=float(time_limit),
                verbose=False,
            )

            if sub is None:
                st.session_state.solution = None
                st.session_state.solve_error = meta
            else:
                report = engine.validate(inst, sub)
                explanation = engine.explain(inst, sub)

                st.session_state.solution = {
                    "scenario": scenario,
                    "sub": sub,
                    "meta": meta,
                    "report": report,
                    "explanation": explanation,
                    "inst": inst,
                }
                st.session_state.solve_error = None

        except Exception as exc:
            st.session_state.solution = None
            st.session_state.solve_error = {"error": str(exc)}


if st.session_state.get("solve_error"):
    st.error("The optimiser did not produce a solution.")
    st.json(st.session_state.solve_error)


if st.session_state.get("solution"):
    sol = st.session_state.solution

    st.header("4. Optimisation Results")
    render_solution(
        sol["sub"],
        sol["report"],
        sol["meta"],
        key_prefix="main",
    )

    if sol["explanation"] is not None and not sol["explanation"].empty:
        st.subheader("Delay Explanation")
        st.dataframe(
            sol["explanation"],
            use_container_width=True,
            hide_index=True,
        )

    st.header("5. What-if Replanning")
    st.caption(
        "Change the weekly capacity of one location and re-optimise while penalising schedule churn."
    )

    location_options = sorted(sol["inst"]["cap"].keys())
    selected_location = st.selectbox(
        "Location",
        options=location_options,
    )

    current_capacity = int(sol["inst"]["cap"][selected_location])
    new_capacity = st.number_input(
        "New capacity",
        min_value=0,
        max_value=max(20, current_capacity + 10),
        value=current_capacity,
        step=1,
    )

    churn_weight = st.number_input(
        "Schedule churn penalty",
        min_value=0,
        max_value=100,
        value=int(engine.CONFIG.get("churn_weight", 2)),
        step=1,
    )

    if st.button("Re-optimise with Capacity Change", use_container_width=True):
        if int(new_capacity) == current_capacity:
            st.info("The selected capacity is unchanged.")
        else:
            with st.spinner("Replanning..."):
                try:
                    fresh_inst = engine.load_instance(data_dir)

                    new_sub, new_meta, moved = engine.replan(
                        fresh_inst,
                        sol["scenario"],
                        {selected_location: int(new_capacity)},
                        sol["sub"],
                        time_limit=float(time_limit),
                        churn_weight=int(churn_weight),
                    )

                    if new_sub is None:
                        st.error("No feasible replanned schedule was found.")
                        st.json(new_meta)
                    else:
                        # Validate against the modified capacity, not the restored instance.
                        validation_inst = engine.load_instance(data_dir)
                        validation_inst["cap"][selected_location] = int(new_capacity)
                        new_report = engine.validate(validation_inst, new_sub)

                        st.session_state.replan_solution = {
                            "sub": new_sub,
                            "meta": new_meta,
                            "report": new_report,
                            "moved": moved or [],
                            "location": selected_location,
                            "old_capacity": current_capacity,
                            "new_capacity": int(new_capacity),
                        }

                except Exception as exc:
                    st.error(f"Replanning failed: {exc}")

    if st.session_state.get("replan_solution"):
        repl = st.session_state.replan_solution

        st.subheader("Replanning Impact")
        r1, r2, r3 = st.columns(3)
        r1.metric("Changed location", repl["location"])
        r2.metric(
            "Capacity",
            repl["new_capacity"],
            delta=repl["new_capacity"] - repl["old_capacity"],
        )
        r3.metric("Activities moved", len(repl["moved"]))

        if repl["moved"]:
            st.write("Moved activities:")
            st.code(", ".join(repl["moved"]))

        render_solution(
            repl["sub"],
            repl["report"],
            repl["meta"],
            key_prefix="replan",
        )


st.divider()
st.caption(
    "Safety and feasibility decisions are produced by the deterministic optimisation and validation engine."
)
