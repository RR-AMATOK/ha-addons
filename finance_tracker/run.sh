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
# --no-proxy-headers (SEV-001 hardening): uvicorn 0.51.0 defaults proxy_headers=True,
# installing ProxyHeadersMiddleware. That's currently harmless only because
# forwarded_allow_ips defaults to 127.0.0.1 while the Supervisor peer is 172.30.32.2 --
# but a future FORWARDED_ALLOW_IPS=* edit would let a member set
# X-Forwarded-For: 172.30.32.2 and rewrite request.client.host, defeating server.py's
# peer gate (_SUPERVISOR_PEER). This flag makes "request.client.host == real TCP peer"
# an explicit, drift-proof invariant per DEC-026: uvicorn must never trust forwarded
# headers from that peer.
exec python3 -m uvicorn server:app --host 0.0.0.0 --port 8099 --log-level warning --no-proxy-headers
