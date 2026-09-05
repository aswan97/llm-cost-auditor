# Runtime image for the auditor. Python 3.12 matches the floor in pyproject.toml
# and the lower CI matrix leg, so what runs here is what CI checks.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependency metadata first, so a source edit does not re-resolve the tree.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e ".[web]"

# The app is single-tenant and holds no secrets (SPEC.md §12), but it has no
# reason to be root either: it only ever reads logs and writes a run store.
RUN useradd --create-home --uid 10001 auditor \
 && mkdir -p /workspace /logs \
 && chown -R auditor:auditor /workspace /app
USER auditor

# The workspace holds config and the run store; mount it to keep runs across
# container restarts.
ENV LCA_WORKSPACE=/workspace
VOLUME ["/workspace"]
EXPOSE 8787

# Inside a container, loopback means "unreachable from the host", so the bind
# address has to be 0.0.0.0 — and compose maps it to 127.0.0.1 on the host so
# the loopback-only posture of §12 still holds end to end. `serve` prints the
# non-loopback warning either way, which is the honest thing for it to do.
CMD ["llm-cost-auditor", "serve", "--host", "0.0.0.0", "--port", "8787", \
     "--workspace", "/workspace"]

# ---------------------------------------------------------------------------

FROM base AS dev
USER root
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e ".[web,dev,zstd]"
COPY tests ./tests
COPY scripts ./scripts
RUN chown -R auditor:auditor /app
USER auditor
CMD ["pytest", "-q"]
