#!/bin/sh
# The VPS's deploy entry point - installed as /opt/nool/deploy.sh and run as
# the forced command of the GitHub Actions deploy key (authorized_keys:
# command="/opt/nool/deploy.sh",restrict), so that key can do nothing else.
#
#   git archive HEAD | ssh root@VPS <repo>      (see .github/workflows/deploy.yml)
#
# Unpacks the pushed code into /opt/nool/<repo>, keeps the server-only files
# (.env, secrets/), rebuilds what that repo runs, and leaves the previous
# version in /opt/nool/<repo>.prev - roll back by swapping the two folders
# and running this repo's "up" line again.
set -eu
repo=${SSH_ORIGINAL_COMMAND:-${1:-}}
case "$repo" in
  nool-core|nool-kg|nool-apps|nool-school-admin|nool-pm-dashboard) ;;
  *) echo "deploy: unknown repo '$repo'" >&2; exit 2 ;;
esac

# one deploy at a time - two repos pushed together would race on nool-core
exec 9>/opt/nool/.deploy.lock
flock 9

dir=/opt/nool/$repo
new=$(mktemp -d /opt/nool/.incoming-XXXXXX)
tar -x -C "$new"
for keep in .env secrets; do
  [ -e "$dir/$keep" ] && cp -a "$dir/$keep" "$new/"
done
rm -rf "$dir.prev"
[ -d "$dir" ] && mv "$dir" "$dir.prev"
mv "$new" "$dir"
chmod 755 "$dir"

# nginx is left running across a rebuild but keeps the old containers'
# addresses, so after one it routes /auth and /api to the wrong place (or
# nowhere) - restart it so it looks them up again
core() { cd /opt/nool/nool-core && docker compose up -d --build --remove-orphans "$@" && docker compose restart nginx; }
case "$repo" in
  nool-core) core ;;
  nool-kg) core nool-kg ;;
  # landing bind-mounts files from $dir, which was just swapped for a new
  # folder - recreate it so it stops serving the old (soon deleted) one
  nool-apps) cd "$dir" && docker compose -f compose.demo.yml -f compose.server.yml up -d --build &&
    docker compose -f compose.demo.yml -f compose.server.yml up -d --force-recreate --no-deps landing ;;
  nool-school-admin) cd "$dir" && docker compose build && core school-admin ;;
  nool-pm-dashboard) cd "$dir" && docker compose build && core super-admin ;;
esac
docker image prune -f >/dev/null
echo "deploy: $repo is live"
