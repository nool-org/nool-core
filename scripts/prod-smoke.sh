#!/usr/bin/env bash
# Smoke check for a running compose.prod.yml stack, through Caddy + nginx.
#
#   API_DOMAIN=api.example.in ORIGIN=https://admin.example.in ./scripts/prod-smoke.sh
#   HTTP=http://... HTTPS=https://... ./scripts/prod-smoke.sh   # explicit URLs
#
# ORIGIN must be one of NGINX_CORS_ORIGINS. -k: also works before the real
# Let's Encrypt cert is issued (or with API_DOMAIN=localhost, Caddy's own CA).
set -uo pipefail
HTTP=${HTTP:-http://${API_DOMAIN:-localhost}}
HTTPS=${HTTPS:-https://${API_DOMAIN:-localhost}}
ORIGIN=${ORIGIN:-}
fail=0

check() {  # name expected actual
    if [ "$2" = "$3" ]; then echo "ok   $1"; else echo "FAIL $1: expected '$2', got '$3'"; fail=1; fi
}
code() { curl -sk -o /dev/null -w '%{http_code}' "$@"; }
header() { curl -sk -D - -o /dev/null "${@:2}" | tr -d '\r' | grep -i "^$1:" | cut -d' ' -f2-; }

check "http redirects to https"   308 "$(code "$HTTP/api/v1/x")"
check "https /health"             200 "$(code "$HTTPS/health")"
check "auth /health"              200 "$(code "$HTTPS/auth/health")"
check "core needs auth"           401 "$(code "$HTTPS/api/v1/admin/schools")"
check "HSTS header"               "max-age=31536000; includeSubDomains" "$(header Strict-Transport-Security "$HTTPS/health")"
check "unknown origin gets no CORS" "" "$(header Access-Control-Allow-Origin -H 'Origin: https://not-allowed.invalid' "$HTTPS/health")"
if [ -n "$ORIGIN" ]; then
    check "allowed origin echoed" "$ORIGIN" "$(header Access-Control-Allow-Origin -H "Origin: $ORIGIN" "$HTTPS/health")"
fi
exit $fail
