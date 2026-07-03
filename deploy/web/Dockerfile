# Public StadtRad website. Build context is the repo root (see docker-compose.yml).
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

COPY requirements-web.txt .
RUN pip install --no-cache-dir -r requirements-web.txt

# App + the reused analysis helpers it imports (_trips, weather). Data and the
# forecast artifact are bind-mounted read-only at runtime, not baked in.
COPY web/ web/
COPY analysis/_trips.py analysis/weather.py analysis/

RUN useradd --create-home app && chown -R app /app
USER app

EXPOSE 8000
CMD ["uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--limit-concurrency", "64"]
