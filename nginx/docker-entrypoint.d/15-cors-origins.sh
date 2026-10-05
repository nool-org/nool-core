#!/bin/sh
# Turns NGINX_CORS_ORIGINS ("https://a.example https://b.example", space or
# comma separated) into map entries for templates/default.conf.template.
# Runs from the nginx image's entrypoint before the envsubst step.
set -euf
out=/etc/nginx/cors_origins.map
: > "$out"
for origin in $(echo "${NGINX_CORS_ORIGINS:-}" | tr ',' ' '); do
    case "$origin" in
        *[!A-Za-z0-9.:/-]*) echo "15-cors-origins.sh: ignoring invalid origin '$origin'" >&2 ;;
        http://*|https://*) printf '"%s" $http_origin;\n' "$origin" >> "$out" ;;
        *) echo "15-cors-origins.sh: ignoring invalid origin '$origin'" >&2 ;;
    esac
done
echo "15-cors-origins.sh: $(wc -l < "$out") CORS origin(s) allowed"
