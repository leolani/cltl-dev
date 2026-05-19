# syntax = docker/dockerfile:1.2
# Builds the app-level orchestration service.
# Build context: app/ directory (requires cltl/cltl-base:latest to exist first)
# Named build context: leolani (supplied via --build-context or docker-compose additional_contexts)

ARG base_image=cltl/cltl-base:latest
FROM ${base_image}

WORKDIR /app

COPY --from=leolani . /leolani/

COPY py-app/requirements.txt ./requirements.txt
COPY setup.py ./
COPY py-app ./py-app

RUN pip install --no-index --no-build-isolation --find-links=/leolani -r requirements.txt && \
    rm -rf /leolani && \
    find /usr/local/lib/python3.10 -type d -name __pycache__ -exec rm -rf {} +

RUN pip install --no-deps .

COPY docker-app/app.py ./

HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["python", "app.py"]
