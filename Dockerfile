# syntax=docker/dockerfile:1
# Opseval CLI image (FR-DX-01, ADR-0038). Built from this checkout, not from PyPI: no release
# exists before Gate 0. Every base image is pinned by digest and every Python dependency is
# installed from uv.lock with its hash checked, so a build is reproducible and a tampered
# package fails the build instead of shipping.
#
#   docker build -t opseval .
#   docker run --rm opseval demo                       # exit 0
#   docker run --rm -v "$PWD:/work" opseval run suites/smoke.yaml --profile profile.yaml \
#       --target wss://agent.example.com/ws -e OPSEVAL_ALLOWED_HOSTS=agent.example.com
#
# The console is not in this image: `opseval serve` binds to loopback only (ADR-0031), which
# a container cannot be reached on. Run the console on your own machine.

ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.10.4@sha256:4cac394b6b72846f8a85a7a0e577c6d61d4e17fe2ccee65d9451a8b3c9efb4ac

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY packages/opseval packages/opseval
COPY packages/pytest-opseval/pyproject.toml packages/pytest-opseval/README.md packages/pytest-opseval/
COPY packages/pytest-opseval/src packages/pytest-opseval/src
# The runtime dependencies exactly as locked, with hashes; then Opseval itself as a wheel.
RUN uv export --frozen --package opseval --no-dev --no-emit-workspace --format requirements-txt \
        --output-file /out/requirements.txt \
    && uv build --package opseval --wheel --out-dir /out

FROM ${PYTHON_IMAGE}
LABEL org.opencontainers.image.title="opseval" \
      org.opencontainers.image.description="Black-box evaluation for conversational AI agents" \
      org.opencontainers.image.licenses="Apache-2.0"
COPY --from=build /out /tmp/opseval
RUN pip install --no-cache-dir --no-deps --require-hashes -r /tmp/opseval/requirements.txt \
    && pip install --no-cache-dir --no-deps /tmp/opseval/*.whl \
    && rm -rf /tmp/opseval \
    && useradd --create-home --uid 10001 opseval \
    && mkdir /work && chown opseval /work
USER opseval
WORKDIR /work
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["opseval"]
CMD ["--help"]
