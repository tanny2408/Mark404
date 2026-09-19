# Railway Track Access Optimiser

A railway possession scheduling and decision-support application developed for the **NebulaX Hackathon – PS1: Railway Track Access Optimisation**.

The project uses **Google OR-Tools CP-SAT** to generate feasible railway access schedules for Scenarios **A, B and C**, validates the generated schedule, visualises the result in an interactive Streamlit dashboard, and exports the required submission CSV files.

---

## Overview

The optimiser schedules railway activities across the planning horizon while considering constraints such as:

- planned activity start dates
- predecessor relationships
- required workload / access nights
- contract workfront limits
- maximum weekly accesses
- railway location capacity
- possession types (`PM`, `PC`, `C`)
- co-sharing rules
- safety closures and buffers
- ECLO usage
- contract priorities
- planned completion dates

The main optimisation engine is implemented in:

```text
PS1/Scheduler/trackaccess.py
```

The web interface is implemented in:

```text
PS1/Scheduler/streamlit_app.py
```

---

## Features

### Optimisation

- Solves **Scenario A**
- Solves **Scenario B**
- Solves **Scenario C**
- Uses **OR-Tools CP-SAT**
- Automatically extends the planning horizon when required
- Supports priority-weighted delay penalties
- Supports ECLO decisions
- Supports location capacity constraints
- Supports contract workfront and weekly access constraints
- Supports predecessor constraints
- Generates railway occupancy and co-sharing groups

### Validation

The application runs the built-in validation logic after each solve and reports:

- feasibility
- hard constraint violations
- total overrun days
- excess access nights
- ECLO nights
- priority-weighted score
- capacity hotspots

### Visualisation

The Streamlit dashboard includes:

- **Activity schedule timeline**
  - activities on the Y-axis
  - calendar time on the X-axis
  - colour-coded contract priorities
  - planned completion markers
  - ECLO highlighting
  - overrun highlighting

- **Capacity heatmap**
  - location × week
  - capacity usage based on possession / `co_share_group` count

- **Contract performance chart**
  - contract overrun days

- interactive Plotly hover, zoom and pan

### Submission Output

The application generates the three required output files:

```text
SCHEDULE_ACCESS.csv
SCHEDULE_OCCUPANCY.csv
RESULTS.csv
```

It also provides a downloadable diagnostic report:

```text
REPORT.json
```

---

## Project Structure

```text
Mark404/
├── Dockerfile
├── requirements.txt
├── README.md
└── PS1/
    ├── 01_data/
    ├── 02_references/
    ├── 03_submission_sample/
    ├── PS1_README.md
    └── Scheduler/
        ├── streamlit_app.py
        └── trackaccess.py
```

---

## Input Files

The optimiser expects the eight PS1 input files:

```text
01_LINES.csv
02_STATIONS.csv
03_SECTORS.csv
04_LOCATION_SUPPLY.csv
05_BUFFER_LOCATION.csv
06_PARAMETERS.csv
07_PROJECT_DETAILS.csv
08_ACTIVITY_DETAILS.csv
```

The Streamlit application supports two input modes:

### Bundled Dataset

If the Docker image or local repository contains:

```text
PS1/01_data/
```

the dashboard can load the bundled dataset directly.

### Uploaded Dataset

Users can also upload all eight CSV files through the Streamlit interface.

This is useful for testing alternative or hidden instances without rebuilding the application.

---

## Output Files

### `SCHEDULE_ACCESS.csv`

Describes when each activity is scheduled.

Example structure:

```text
activity_id
access_seq
week
eclo
access_night
```

### `SCHEDULE_OCCUPANCY.csv`

Describes which railway locations are occupied.

Example structure:

```text
activity_id
week
location_id
co_share_group
```

### `RESULTS.csv`

Contains contract completion results.

Example structure:

```text
scenario
contract_number
simulated_completion_date
overrun_days
```

---

## Optimisation Model

The solver uses a time-indexed CP-SAT formulation.

For activity `a` and week `w`:

```text
X[a,w] = 1
```

means activity `a` receives an access in week `w`.

For ECLO:

```text
E[a,w] = 1
```

means the scheduled access is an ECLO access.

The implementation uses integer workload units:

```text
Standard access = 2 units
ECLO access     = 3 units
```

which represents:

```text
Standard access = 1.0 workload
ECLO access     = 1.5 workload
```

The solver then applies activity, contract, railway capacity, possession and safety constraints before minimising the scenario-specific objective.

---

## Scenarios

### Scenario A

- ECLO is disabled
- railway capacity is treated as a hard constraint
- delay is allowed
- objective focuses on priority-weighted delay

### Scenario B

- planned completion dates are hard
- ECLO and permitted capacity flexibility may be used
- objective penalises additional operational flexibility

### Scenario C

- balances delay, ECLO usage and capacity flexibility
- applies the configured Scenario C limits and penalties

The exact objective implementation is defined in:

```text
PS1/Scheduler/trackaccess.py
```

---

## Requirements

Python **3.11** is recommended.

The project uses the following Python dependencies:

```txt
pandas>=2.0,<3.0
ortools>=9.10,<10.0
streamlit>=1.35,<2.0
plotly>=5.20,<7.0
```

Install dependencies with:

```bash
pip install -r requirements.txt
```

---

## Run Locally

From the repository root:

```bash
streamlit run PS1/Scheduler/streamlit_app.py
```

Or:

```bash
cd PS1/Scheduler
streamlit run streamlit_app.py
```

By default, Streamlit uses:

```text
http://localhost:8501
```

To explicitly use port `8501`:

```bash
streamlit run streamlit_app.py --server.port 8501
```

---

## Using `trackaccess.py` Directly

The solver can also be executed directly from Python.

Example:

```python
import trackaccess as engine

inst = engine.load_instance("../01_data")

submission, meta = engine.solve_with_growing_horizon(
    inst,
    "C",
    180,
)

if submission is not None:
    report = engine.validate(inst, submission)

    print(meta)
    print(report)
```

---

## Dashboard Workflow

```text
Choose bundled data or upload 8 CSV files
                    ↓
             Load instance
                    ↓
           Review input summary
                    ↓
          Select Scenario A/B/C
                    ↓
             Run CP-SAT
                    ↓
          Validate the schedule
                    ↓
     ┌──────────────┼──────────────┐
     ↓              ↓              ↓
 Timeline      Capacity Heatmap   Contract Chart
     └──────────────┼──────────────┘
                    ↓
           Review CSV outputs
                    ↓
        Download submission files
```

---

## Docker

The Docker image should include the complete `PS1` directory so that the Streamlit application can access the bundled `PS1/01_data` dataset.

Dockerfile:

```dockerfile
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./

RUN pip install --upgrade pip \
    && pip install -r requirements.txt

COPY PS1 ./PS1

WORKDIR /app/PS1/Scheduler

EXPOSE 8080

CMD ["sh", "-c", "exec streamlit run streamlit_app.py \
    --server.address=0.0.0.0 \
    --server.port=${PORT:-8080} \
    --server.headless=true \
    --browser.gatherUsageStats=false"]
```

Build the image:

```bash
cd ~/Mark404
docker build --no-cache -t railway-track-access .
```

Run locally on port `8501`:

```bash
docker run --rm \
  -p 8501:8501 \
  -e PORT=8501 \
  railway-track-access
```

Then open:

```text
http://localhost:8501
```

---

## Google Cloud Run Deployment

From the repository root:

```bash
cd ~/Mark404
```

Deploy:

```bash
gcloud run deploy railway-track-access \
  --source . \
  --region asia-southeast1 \
  --allow-unauthenticated \
  --cpu 4 \
  --memory 4Gi \
  --timeout 900 \
  --max-instances 1
```

---

## Architecture

```text
                        ┌───────────────────────┐
                        │     Streamlit UI      │
                        │                       │
                        │ Upload / Select Data  │
                        │ Visual Analytics      │
                        │ CSV Downloads         │
                        └───────────┬───────────┘
                                    │
                                    ▼
                        ┌───────────────────────┐
                        │    trackaccess.py     │
                        │                       │
                        │    OR-Tools CP-SAT    │
                        └───────────┬───────────┘
                                    │
                   ┌────────────────┴────────────────┐
                   │                                 │
                   ▼                                 ▼
        ┌──────────────────────┐          ┌──────────────────────┐
        │ Constraint / Safety  │          │      Validator       │
        │       Logic          │          │                      │
        └──────────┬───────────┘          └──────────┬───────────┘
                   │                                 │
                   └────────────────┬────────────────┘
                                    │
                                    ▼
                        ┌───────────────────────┐
                        │   Submission Output   │
                        │                       │
                        │ SCHEDULE_ACCESS.csv   │
                        │ SCHEDULE_OCCUPANCY.csv│
                        │ RESULTS.csv           │
                        └───────────────────────┘
```

---

## Technology Stack

- **Python 3.11**
- **Google OR-Tools CP-SAT**
- **pandas**
- **Streamlit**
- **Plotly**
- **Docker**
- **Google Cloud Run**

---

## NebulaX Hackathon

Developed for:

**NebulaX Hackathon – PS1: Railway Track Access Optimisation**
