ARG base_image=cltl/cltl-base:latest
FROM ${base_image}

COPY --from=leolani . /leolani/

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-index --no-build-isolation --find-links=/leolani -r requirements.txt && \
    rm -rf /leolani

COPY . .

CMD ["python", "run_diagnostics.py"]
