FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements-docker.txt .
RUN pip install --upgrade pip && pip install -r requirements-docker.txt
COPY LDDC ./LDDC
COPY webapp ./webapp
RUN mkdir -p /music /data
ENV MUSIC_ROOT=/music STATE_DIR=/data SCAN_INTERVAL_MINUTES=0
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')"
CMD ["uvicorn", "webapp.app:app", "--host", "0.0.0.0", "--port", "8080"]
