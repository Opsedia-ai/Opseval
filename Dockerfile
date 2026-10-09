# syntax=docker/dockerfile:1
# Opseval CLI image (FR-DX-01, ADR-0038). Built from this checkout, not from PyPI: no release
# exists before Gate 0. Every base image is pinned by digest (written out on each FROM line,
# so Dependabot can update it), and every Python package that enters the image, the wheel's
# build backend included, is installed from uv.lock with its hash checked: a tampered package
# fails the build instead of shipping.
#
#   docker build -t opseval .
#   docker run --rm opseval demo                       # exit 0
#   docker run --rm -e OPSEVAL_ALLOWED_HOSTS=agent.example.com -v "$PWD:/work" \
#       opseval run suites/smoke.yaml --profile profile.yaml --target wss://agent.example.com/ws
#
# The console is not in this image: `opseval serve` binds to loopback only (ADR-0031), which
# a container cannot be reached on. Run the console on your own machine.

FROM ghcr.io/astral-sh/uv:0.12.24@sha256:3af4716e991d6956a41e573eab705d0ee08500cd829ed30293eb8472f372c65a AS uv

FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY packages/opseval packages/opseval
COPY packages/pytest-opseval/pyproject.toml packages/pytest-opseval/README.md packages/pytest-opseval/
COPY packages/pytest-opseval/src packages/pytest-opseval/src
# The runtime dependencies exactly as locked, with hashes. The build backend too (the `build`
# group), installed hash-checked and used without isolation, so building the wheel fetches
# nothing unchecked.
RUN uv export --frozen --package opseval --no-dev --no-emit-workspace --format requirements-txt \
        --output-file /out/requirements.txt \
    && uv export --frozen --only-group build --no-emit-workspace --format requirements-txt \
        --output-file /tmp/build-requirements.txt \
    && pip install --no-cache-dir --require-hashes -r /tmp/build-requirements.txt
# Without a network, so building the wheel provably fetches nothing.
RUN --network=none pip wheel --no-cache-dir --no-build-isolation --no-deps --wheel-dir /out \
        packages/opseval

FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
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
