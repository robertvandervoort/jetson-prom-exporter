FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml .
COPY src/ src/
COPY wheelhouse/ wheelhouse/
RUN pip install --no-cache-dir --timeout 120 --retries 10 prometheus_client distro smbus2 setuptools wheel \
    && if ls wheelhouse/jetson_stats-*.whl >/dev/null 2>&1; then \
        pip install --no-cache-dir --no-deps wheelhouse/jetson_stats-*.whl; \
    else \
        pip install --no-cache-dir --timeout 120 --retries 10 --no-build-isolation --no-deps jetson-stats; \
    fi

ENV PYTHONPATH=/app/src
EXPOSE 9101
ENTRYPOINT ["python", "-m", "jetson_prom_exporter"]
