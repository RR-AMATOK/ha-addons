#!/usr/bin/with-contenv bashio
# Finance Tracker add-on launcher (DEC-021).
# Binds 0.0.0.0 INSIDE the container only — config.yaml publishes no ports, so the sole
# route in is HA's authenticated ingress proxy (172.30.32.2). Single worker (R5).

# MQTT (P1, optional): if the Mosquitto service is available, hand its credentials to
# the app via env. The app runs identically when these are absent.
if bashio::services.available mqtt; then
    export MQTT_HOST="$(bashio::services mqtt 'host')"
    export MQTT_PORT="$(bashio::services mqtt 'port')"
    export MQTT_USER="$(bashio::services mqtt 'username')"
    export MQTT_PASSWORD="$(bashio::services mqtt 'password')"
    bashio::log.info "MQTT broker discovered at ${MQTT_HOST}:${MQTT_PORT}"
else
    bashio::log.info "No MQTT service available — running without HA sensors"
fi

# /data is the add-on's private volume; resolve_db_path() already prefers it (DEC-006),
# the explicit export just makes the contract visible in logs/env.
export ACTUALS_DB_PATH=/data/actuals.db

cd /app
bashio::log.info "Starting Finance Tracker on :8099 (ingress-only)"
exec python3 -m uvicorn server:app --host 0.0.0.0 --port 8099 --log-level warning
