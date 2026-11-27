# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY pyproject.toml README.md ./
COPY app ./app
COPY mock_provider ./mock_provider
RUN python -m venv /opt/venv && /opt/venv/bin/pip install .

FROM python:3.12-slim
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    GATEWAY_POLICY_PATH=/app/config/policy.yml GATEWAY_AUDIT_LOG_PATH=/app/data/audit.log
RUN useradd --system --uid 10001 --home /app gateway
WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY config/policy.example.yml /app/config/policy.yml
RUN mkdir -p /app/data && chown -R gateway /app/data
USER gateway
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"
CMD ["uvicorn", "app.main:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log", "--proxy-headers"]
