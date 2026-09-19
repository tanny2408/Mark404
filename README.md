# Railway Track Access Optimiser

A decision-support tool for railway possession scheduling, developed for the **NebulaX Hackathon – PS1: Railway Track Access Optimisation**.

The application reads the eight PS1 input CSV files, builds a railway scheduling model, solves Scenarios **A, B and C** with **Google OR-Tools CP-SAT**, validates the generated schedule, and exports the required submission files.

A Streamlit interface is included for interactive optimisation, diagnostics, downloads, and what-if replanning.

---

## Features

- Reads all 8 PS1 input files
- Builds activity routes, occupied locations, buffers, dependencies, capacities and contract rules
- Solves railway access scheduling with **OR-Tools CP-SAT**
- Supports all three scenarios:
  - **Scenario A** – capacity is hard, delay is allowed
  - **Scenario B** – planned completion dates are hard, ECLO/capacity flexibility is allowed
  - **Scenario C** – balanced optimisation
- Handles workload, planned starts, predecessors, capacity, workfronts, weekly access limits, PM/PC/C rules, co-sharing, ECLO and safety buffers
- Generates:
  - `SCHEDULE_ACCESS.csv`
  - `SCHEDULE_OCCUPANCY.csv`
  - `RESULTS.csv`
- Produces validation and diagnostic reports
- Supports capacity-based what-if replanning
- Penalises unnecessary schedule churn during replanning
- Includes a Streamlit web UI
- Can be deployed to **Google Cloud Run**

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
    └── Scheduler/
        ├── streamlit_app.py
        └── trackaccess_corrected.py
```

---

## Input Files

The optimiser expects these eight CSV files:

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

The Streamlit app allows users to upload these files directly.

---

## Output Files

For each scenario, the optimiser produces:

### `SCHEDULE_ACCESS.csv`

Defines when each activity receives access.

Typical columns:

```text
activity_id
access_seq
week
eclo
access_night
```

### `SCHEDULE_OCCUPANCY.csv`

Defines the railway resources occupied by each scheduled activity.

Typical columns:

```text
activity_id
week
location_id
co_share_group
```

### `RESULTS.csv`

Contains contract-level completion results.

Typical columns:

```text
scenario
contract_number
simulated_completion_date
overrun_days
```

The tool can also produce:

```text
REPORT.json
EXPLANATION.csv
SUMMARY.csv
```

---

## Optimisation Model

The core solver uses **Google OR-Tools CP-SAT**.

The main scheduling decision is:

```text
X[a, w] = 1
```

if activity `a` is scheduled in week `w`.

ECLO is modelled separately:

```text
E[a, w] = 1
```

if that access is an ECLO access.

To keep the CP-SAT model integral:

```text
Standard access = 2 workload units
ECLO access     = 3 workload units
```

representing 1.0 and 1.5 units of work respectively.

---

## Scenario Objectives

### Scenario A

Capacity is hard and ECLO is not allowed.

```text
Objective = priority-weighted delay
```

### Scenario B

Planned completion dates are hard.

```text
Objective =
7 × excess access nights
+
5 × ECLO nights
```

### Scenario C

Balances delay, excess access and ECLO.

```text
Objective =
priority-weighted delay
+
7 × excess access nights
+
5 × ECLO nights
```

---

## Local Installation

Python 3.11 is recommended.

Install dependencies:

```bash
pip install -r requirements.txt
```

Example `requirements.txt`:

```txt
pandas>=2.0,<3.0
ortools>=9.10,<10.0
streamlit>=1.35,<2.0
```

---

## Run the Streamlit App

From the repository root:

```bash
streamlit run PS1/Scheduler/streamlit_app.py
```

Or:

```bash
cd PS1/Scheduler
streamlit run streamlit_app.py
```

Then open:

```text
http://localhost:8501
```

The UI flow is:

```text
Upload instance
    ↓
Review data
    ↓
Select Scenario A / B / C
    ↓
Run optimiser
    ↓
Review feasibility and score
    ↓
Inspect access / occupancy / contract results
    ↓
Download submission files
    ↓
Optional what-if replanning
```

---

## Run the Solver from Python

```python
import trackaccess_corrected as engine

inst = engine.load_instance("path/to/data")

submission, meta = engine.solve_with_autoextend(
    inst,
    scenario="C",
    time_limit=180,
)

report = engine.validate(inst, submission)

print(meta)
print(report)
```

---

## Validation

The project includes an internal validator that checks:

- full workload delivery
- planned start compliance
- predecessor order
- weekly access limits
- workfront limits
- possession mix legality
- capacity limits
- safety closures
- ECLO rules
- result consistency

A successful result looks like:

```text
feasible = true
hard_violations = []
```

The official PS1 reference validator should still be treated as the final source of truth.

---

## Example Results

One tested run produced:

| Scenario | Feasible | Overrun Days | Excess Access Nights | ECLO Nights | Objective |
|---|---:|---:|---:|---:|---:|
| A | Yes | 42 | 0 | 0 | 131.6 |
| B | Yes | 0 | 0 | 10 | 50.0 |
| C | Yes | 21 | 0 | 4 | 44.5 |

These values depend on the solver configuration and input instance.

---

## What-if Replanning

The Streamlit app supports temporary capacity changes, for example:

```text
SEC:ALP:S02_S03:EB
capacity 4 → 1
```

The system then re-optimises the schedule while applying a schedule-churn penalty so that unaffected activities are kept in their original weeks where possible.

---

## Docker

Example Dockerfile:

```dockerfile
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./

RUN pip install --upgrade pip \
    && pip install -r requirements.txt

COPY PS1/Scheduler ./PS1/Scheduler

WORKDIR /app/PS1/Scheduler

EXPOSE 8080

CMD ["sh", "-c", "exec streamlit run streamlit_app.py \
    --server.address=0.0.0.0 \
    --server.port=${PORT:-8080} \
    --server.headless=true \
    --browser.gatherUsageStats=false"]
```

Build:

```bash
docker build -t railway-track-access .
```

Run:

```bash
docker run --rm -p 8080:8080 -e PORT=8080 railway-track-access
```

Then open:

```text
http://localhost:8080
```

---

## Deploy to Google Cloud Run

Set the Google Cloud project:

```bash
gcloud config set project YOUR_PROJECT_ID
```

Enable required services:

```bash
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com
```

Deploy from the repository root:

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

After deployment, Cloud Run returns a public HTTPS URL.

The application continues running even after Cloud Shell or the Google Cloud Console is closed.

---

## Architecture

```text
                     ┌─────────────────────┐
                     │    Streamlit UI     │
                     └──────────┬──────────┘
                                │
                                ▼
                     ┌─────────────────────┐
                     │  Optimisation Core  │
                     │     OR-Tools        │
                     │      CP-SAT         │
                     └──────────┬──────────┘
                                │
                  ┌─────────────┴─────────────┐
                  │                           │
                  ▼                           ▼
        ┌──────────────────┐       ┌──────────────────┐
        │ Constraint Logic │       │ Validation Layer │
        └────────┬─────────┘       └────────┬─────────┘
                 │                          │
                 └────────────┬─────────────┘
                              ▼
                    ┌──────────────────┐
                    │   PS1 CSV Data   │
                    └──────────────────┘
```

The optimisation engine handles feasibility and scheduling deterministically.

Natural-language or AI features can be added as an explanation layer, but should not be responsible for safety-critical scheduling decisions.

---

## Technology Stack

- Python 3.11
- Google OR-Tools CP-SAT
- pandas
- Streamlit
- Docker
- Google Cloud Run

---

## Development Notes

This implementation is a hackathon decision-support prototype.

Before final submission, schedules should be checked against the official reference validator, especially for interpretation-sensitive rules such as:

- platform occupancy semantics
- safety buffer expansion
- interchange behaviour
- ECLO continuity
- possession sharing
- scenario-specific horizon rules

---

## Team

Developed for the **NebulaX Hackathon – Railway Track Access Optimisation (PS1)**.
