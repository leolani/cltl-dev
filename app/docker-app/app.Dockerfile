# syntax = docker/dockerfile:1.2

FROM python:3.9

WORKDIR /app

COPY emissor /src/emissor
COPY cltl-combot /src/cltl-combot
COPY cltl-emissor-data /src/cltl-emissor-data
COPY cltl-context /src/cltl-context
COPY app/py-app/src /src/app-service

RUN pip install --no-cache-dir \
    -e /src/emissor \
    -e "/src/cltl-combot[external]" \
    -e "/src/cltl-emissor-data[client]" \
    -e "/src/cltl-context[service]" \
    -e /src/app-service

COPY app/docker-app /app

HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["python", "app.py"]
