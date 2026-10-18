# Production Dockerfile for PolyArb
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies if required
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml /app/
COPY polyarb/ /app/polyarb/
COPY tests/ /app/tests/

# Install python dependencies
RUN pip install --no-cache-dir .

# Create unprivileged user for secure execution
RUN useradd -m -u 1001 polyarb && \
    mkdir -p /app/data && \
    chown -R polyarb:polyarb /app

USER polyarb

EXPOSE 8085

# Healthcheck to verify local dashboard availability
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8085/api/portfolio || exit 1

ENTRYPOINT ["python", "-m", "polyarb.server", "8085"]
