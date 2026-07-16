# Finance Tracker add-on image (DEC-021).
# build.yaml is dead (Supervisor >= 2026.04 no longer injects BUILD_FROM) — the base is
# pinned HERE. base-python is multi-arch (aarch64 for the Yellow + amd64 for testing);
# every compiled dep (pydantic-core, etc.) ships musl-aarch64 wheels, and
# --only-binary=:all: is the tripwire that fails the build loudly if a future dep would
# need an on-device Rust/C compile instead of silently taking minutes-to-hours.
ARG BUILD_FROM=ghcr.io/home-assistant/base-python:3.13-alpine3.24
FROM ${BUILD_FROM}

ENV PIP_BREAK_SYSTEM_PACKAGES=1 \
    PYTHONUNBUFFERED=1

# Pins per DEC-021 (verified musl-aarch64 wheel availability 2026-07).
RUN pip3 install --no-cache-dir --only-binary=:all: \
    fastapi==0.139.0 \
    uvicorn==0.51.0 \
    pydantic==2.13.4 \
    paho-mqtt==2.1.0

# App source: for a LOCAL add-on the build context is the add-on folder on the HA host,
# so the deploy bundle carries the app under app/ (assembled by addon/build_bundle.sh).
# actuals.db is NEVER in the image — it lives in the add-on's private /data volume.
WORKDIR /app
COPY app/ /app/

COPY run.sh /run.sh
RUN chmod a+x /run.sh
CMD [ "/run.sh" ]

LABEL org.opencontainers.image.title="Finance Tracker" \
      org.opencontainers.image.source="https://github.com/RR-AMATOK/income-tax-calculator" \
      org.opencontainers.image.description="Personal finance tracker HA add-on"
