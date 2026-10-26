# Public StadtRad website. Build context is the repo root (see docker-compose.yml).
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# libgomp1: OpenMP runtime required by the LightGBM wheel (forecast service).
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Web/poller deps, then the heavier forecast-service deps (lightgbm/scikit-learn).
COPY requirements-web.txt requirements-forecast.txt ./
RUN pip install --no-cache-dir -r requirements-web.txt -r requirements-forecast.txt

# App + the reused analysis modules + the GBFS poller + the deriver and forecast
# loops — all four services (web, poller, deriver, forecast) run off this one
# image, different commands.
# Data and the forecast report are bind-mounted at runtime, not baked in.
COPY web/ web/
COPY analysis/_trips.py analysis/derived.py analysis/weather.py analysis/forecast.py analysis/
COPY scripts/gbfs_poller.py scripts/derive.py scripts/
COPY deploy/forecast-loop.sh deploy/derive-loop.sh deploy/

RUN useradd --create-home app && chown -R app /app
USER app

EXPOSE 8000
CMD ["uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--limit-concurrency", "64"]
