# MSME valuation API — single-container deployment.
# Build realdata.db BEFORE building the image (python etl.py), or mount it.
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY core/ core/
COPY mock_api/ mock_api/
COPY realdata/ realdata/
COPY dashboard/ dashboard/
COPY ui/ ui/
COPY run.py validate.py api.py etl.py ./
# comment out if mounting the DB as a volume instead:
COPY realdata.db* ./

EXPOSE 8733
HEALTHCHECK --interval=30s --timeout=5s \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8733/api/v1/health')"
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8733"]
