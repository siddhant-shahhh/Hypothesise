# Weaver control plane.
#
# The PROXY is not containerised on purpose: it runs as a subprocess of the
# user's MCP client and spawns the upstream MCP servers as its own children.
# Putting it in a container would cut it off from both. This image is the
# control plane — the API and dashboard — which is the part that makes sense
# to run as a service.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WEAVER_HOME=/data

WORKDIR /app

# Dependency layer first, so source edits do not reinstall the world.
COPY pyproject.toml README.md ./
COPY packages/ ./packages/
RUN pip install --no-cache-dir . && \
    rm -rf /root/.cache

# Scenarios ship so `--demo` works in the container.
COPY scenarios/ ./scenarios/

# Non-root: this thing exists to hold security telemetry.
RUN useradd --create-home --uid 10001 weaver && \
    mkdir -p /data && chown -R weaver:weaver /data /app
USER weaver

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/readyz', timeout=2).status==200 else 1)"

ENTRYPOINT ["weaver"]
CMD ["api", "--host", "0.0.0.0", "--port", "8080"]
