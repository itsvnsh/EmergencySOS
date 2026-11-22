# EmergencySOS web app (Tier 3). Used by docker-compose.yml together with a MySQL 8 container:
#     docker compose up --build
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FLASK_HOST=0.0.0.0 \
    FLASK_PORT=5000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# run as a normal user, not root
RUN useradd --create-home --uid 1000 sos && chown -R sos /app
USER sos

EXPOSE 5000
CMD ["python", "docker/entrypoint.py"]
