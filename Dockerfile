# OPTIONAL: container image for Linux dev / parity. Primary deployment is native Windows + Funnel.
# Playwright base image includes Chromium + OS deps for browser-use.
FROM mcr.microsoft.com/playwright/python:v1.58.0-jammy

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    IN_DOCKER=true \
    BROWSER_USE_SETUP_LOGGING=false \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# All server modules (wildcard avoids stale COPY list when new tools are added).
COPY *.py ./

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
