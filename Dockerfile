FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY PS1/Scheduler ./PS1/Scheduler

WORKDIR /app/PS1/Scheduler

EXPOSE 8080

CMD streamlit run streamlit_app.py \
    --server.address=0.0.0.0 \
    --server.port=${PORT:-8080} \
    --server.headless=true \
    --browser.gatherUsageStats=false