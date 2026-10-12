# Deploying noolAI on a Hostinger VPS (pilot)

Step-by-step runbook for one Hostinger **KVM 4** VPS (4 vCPU / 16 GB RAM,
Ubuntu, India region) running the whole backend with Docker Compose, with
HTTPS on your own domain. Written to be followed top to bottom by someone
who is not a server expert. Commands run on the server unless marked
**(laptop)**.

Replace `example.in` everywhere with your domain. You end up with:

| Hostname | What | Served by |
| --- | --- | --- |
| `https://api.example.in` | Android app + admin consoles' API | Caddy -> nginx -> auth/core/colearner |
| `https://schooladmin.example.in` | School Admin console | Caddy -> `school-admin` container |
| `https://admin.example.in` | Super Admin console | Caddy -> `super-admin` container |

```
Internet --80/443--> caddy (TLS, Let's Encrypt) --> nginx:80 --> auth, core, colearner
                                                --> school-admin:3000, super-admin:3000
          (internal only) core --> kg, generation, colearner, postgres; backup --> postgres; jaeger
```

**Why Caddy in front of nginx:** Caddy gets and renews Let's Encrypt
certificates for all three hostnames by itself, redirects http to https and
speaks HTTP/3 - no certbot, cron, reload hooks or the "nginx won't start
without a cert, certbot can't get a cert without nginx" first-boot problem.
nginx keeps doing what it already does (routing, CORS, rate limits, request
IDs, security headers) as an internal-only gateway. WebSockets (core's
`/api/v1/ws/...`) pass through both. The voice test's audio goes directly
between the phone and OpenAI (WebRTC); the server only handles the short
HTTPS setup/finish calls, so no extra ports are needed.

**Database:** the pilot runs PostgreSQL in Docker on the same server, with
nightly dumps copied off the server. Moving to managed PostgreSQL is the
recommended later step - see [Later: managed PostgreSQL](#later-managed-postgresql).

---

## 1. Buy the server and domain

1. Hostinger hPanel -> **VPS** -> **KVM 4**. Location: **India (Mumbai)**.
   Operating system: **Ubuntu 24.04 with Docker** (any Ubuntu LTS template
   with Docker is fine; plain Ubuntu also works - install Docker with
   `curl -fsSL https://get.docker.com | sh`).
2. During setup, add your **SSH public key** (laptop: `ssh-keygen -t ed25519`
   if you don't have one, then paste `~/.ssh/id_ed25519.pub`). Set a strong
   root password too and store it in a password manager.
3. Note the server's **public IPv4 address** (hPanel -> VPS -> Overview).
4. Buy the domain (Hostinger or any registrar) if you don't have one.

## 2. Secure the server

```bash
# (laptop) first login
ssh root@SERVER_IP

# Updates + a non-root user that can run docker
apt update && apt -y full-upgrade
adduser deploy                      # set a password (used for sudo)
usermod -aG sudo,docker deploy
mkdir -p /home/deploy/.ssh && cp ~/.ssh/authorized_keys /home/deploy/.ssh/
chown -R deploy:deploy /home/deploy/.ssh && chmod 700 /home/deploy/.ssh
```

In a **second** terminal check `ssh deploy@SERVER_IP` works and `sudo -v`
accepts the password. Only then switch off root and password logins:

```bash
cat > /etc/ssh/sshd_config.d/99-hardening.conf <<'EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
EOF
systemctl restart ssh
```

Firewall (only SSH, HTTP, HTTPS):

```bash
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 443/udp        # HTTP/3
ufw enable
ufw status verbose
```

> **Docker bypasses ufw for published ports.** Anything listed under
> `ports:` in a compose file is reachable from the internet even if ufw
> says deny. That is why only the `caddy` service publishes ports in
> production. Never add `ports:` to another service, and never start the
> admin repos' own `compose.yml` on the server (they publish 3100/3101).

Brute-force protection and automatic security updates:

```bash
apt -y install fail2ban unattended-upgrades
cat > /etc/fail2ban/jail.local <<'EOF'
[sshd]
enabled = true
backend = systemd
maxretry = 5
bantime = 1h
EOF
systemctl enable --now fail2ban
fail2ban-client status sshd

dpkg-reconfigure -plow unattended-upgrades     # answer Yes
```

Docker log rotation (do this **before** the first `up` - it only applies to
containers created afterwards). If `/etc/docker/daemon.json` already
exists, merge these keys into it instead of overwriting:

```bash
cat > /etc/docker/daemon.json <<'EOF'
{
  "log-driver": "local",
  "log-opts": { "max-size": "20m", "max-file": "5" }
}
EOF
systemctl restart docker
```

Optional but cheap insurance against an out-of-memory kill during image
builds - a 4 GB swap file:

```bash
fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

From now on log in as `deploy`.

## 3. DNS

At your registrar's DNS settings add three **A records** pointing to the
server's IPv4 (TTL 300 is fine):

| Type | Name | Value |
| --- | --- | --- |
| A | `api` | SERVER_IP |
| A | `schooladmin` | SERVER_IP |
| A | `admin` | SERVER_IP |

Don't add AAAA (IPv6) records unless the server has working IPv6. Check
from your laptop before going further - each must print the server IP:

```bash
nslookup api.example.in && nslookup schooladmin.example.in && nslookup admin.example.in
```

Caddy can only get certificates once these resolve.

## 4. Production Firebase project

Use a **new** project for production - never the dev/test project (its
accounts, API key and service account have been on many laptops).

1. <https://console.firebase.google.com> -> **Add project** -> name
   `noolai-prod`. Note the actual **Project ID** shown (it may get a suffix).
   Google Analytics is not needed.
2. **Build -> Authentication -> Get started -> Sign-in method ->
   Email/Password -> Enable** (leave "Email link" off).
3. **Authentication -> Settings -> Authorized domains:** add
   `schooladmin.example.in` and `admin.example.in`; remove `localhost`.
4. **Project settings -> General -> Your apps -> Add app -> Web** (`</>`),
   nickname `nool-prod-web`, no Hosting. Copy the config values
   (`apiKey`, `authDomain`, `projectId`, `appId`, `storageBucket`,
   `messagingSenderId`). The Android app and both admin consoles all use
   this web config (they use the Firebase JS SDK).
5. **Restrict the API key:** Google Cloud Console (same project) -> **APIs &
   Services -> Credentials** -> the "Browser key (auto created by
   Firebase)" -> **API restrictions -> Restrict key** -> select **Identity
   Toolkit API** and **Token Service API** -> Save. Leave **Application
   restrictions = None**: the app (JS SDK, sends no Android signature) and
   the server's password check (`FIREBASE_WEB_API_KEY`, sends no referrer)
   would both be rejected by Android-app or HTTP-referrer restrictions.
6. **Service account:** Project settings -> **Service accounts -> Generate
   new private key**. This JSON is the most sensitive file in the system
   (full admin over all accounts). Copy it straight to the server in step 6
   and then delete it from your Downloads folder. Never email it or commit it.
7. **Password reset email:** Authentication -> **Templates -> Password
   reset** -> set the sender name (e.g. "noolAI"), reply-to address, and
   wording. (The School Admin console's "Forgot password" uses it.) Send
   yourself one to check it doesn't land in spam. Optional: a custom
   sender domain (Templates -> customize domain) improves deliverability.
8. First accounts: only the Super Admin is created by hand (step 9).
   School Admins, teachers and students are created from the consoles,
   which generate a one-time temporary password.

## 5. Get the code onto the server

The repos are private, so give the server read-only **deploy keys** (one
per repo - GitHub doesn't allow reusing a deploy key):

```bash
for r in nool-core nool-school-admin nool-super-admin; do
  ssh-keygen -t ed25519 -N '' -f ~/.ssh/$r -C "deploy@nool-vps $r"
  cat >> ~/.ssh/config <<EOF
Host github-$r
  HostName github.com
  User git
  IdentityFile ~/.ssh/$r
  IdentitiesOnly yes
EOF
  echo "== $r: add this as a read-only deploy key"; cat ~/.ssh/$r.pub
done
```

On GitHub, each repo -> **Settings -> Deploy keys -> Add deploy key** (leave
"Allow write access" off) with the matching `.pub` line. Then:

```bash
mkdir -p ~/apps && cd ~/apps
git clone git@github-nool-core:nool202608-gif/nool-core.git
git clone git@github-nool-school-admin:nool202608-gif/nool-school-admin.git
git clone git@github-nool-super-admin:<org>/nool-super-admin.git
# check out the branch/tag you are releasing, e.g.: git -C nool-core checkout main
```

## 6. Configure secrets

```bash
cd ~/apps/nool-core
cp .env.production.example .env
chmod 600 .env
nano .env        # fill every [SECRET] / [CHANGE] value - see the comments
```

Generate the database password with
`openssl rand -base64 32 | tr -d '/+='`. Store a copy of the finished
`.env` and the Firebase JSON in your password manager - the server's copy
is not a backup.

Firebase service account (from step 4.6):

```bash
# (laptop)
scp ~/Downloads/noolai-prod-*.json deploy@SERVER_IP:~/apps/nool-core/secrets/firebase-service-account.json

# (server) the containers run as a non-root user (uid 1000 by default):
# the file must be readable by that uid, and by nobody else.
cd ~/apps/nool-core
chmod 700 secrets
docker compose build auth
docker compose run --rm --no-deps --entrypoint id auth -u     # usually prints 1000
sudo chown 1000:1000 secrets/firebase-service-account.json    # use the printed uid
sudo chmod 600 secrets/firebase-service-account.json
```

Admin consoles - their production values are baked in at build time:

```bash
cd ~/apps/nool-school-admin
cat > .env <<'EOF'
NEXT_PUBLIC_API_BASE_URL=https://api.example.in
NEXT_PUBLIC_FIREBASE_API_KEY=...
NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN=noolai-prod.firebaseapp.com
NEXT_PUBLIC_FIREBASE_PROJECT_ID=noolai-prod
NEXT_PUBLIC_FIREBASE_APP_ID=...
NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET=noolai-prod.firebasestorage.app
NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID=...
EOF
chmod 600 .env
docker compose build        # build only - never `up` here (it publishes port 3100)
# repeat in ~/apps/nool-super-admin
```

This produces the `nool-school-admin-web` / `nool-super-admin-web` images
that nool-core's `compose.prod.yml` runs (profile `admin`).

## 7. First start

`.env` contains `COMPOSE_FILE=compose.yml:compose.prod.yml` and
`COMPOSE_PROFILES=admin`, so in `~/apps/nool-core` a plain `docker compose
...` always means production (dev's `compose.override.yml`, with its open
database port, is never picked up by accident).

```bash
cd ~/apps/nool-core
docker compose config --quiet && echo "config ok"
docker compose run --rm --no-deps caddy caddy validate --config /etc/caddy/Caddyfile
docker compose up -d --build
docker compose ps
```

What happens: images build (first time ~5-10 min), postgres starts,
`migrate` runs `alembic upgrade head` and exits (status `exited (0)` is
correct), then core and the rest start, then nginx, then Caddy, which
requests the three certificates. Watch it:

```bash
docker compose logs migrate
docker compose logs -f caddy      # look for "certificate obtained successfully"; Ctrl+C to stop
```

If certificate issuance fails: DNS isn't pointing here yet, or port 80/443
is blocked (ufw, or Hostinger's own hPanel firewall if you enabled one).
Fix and `docker compose restart caddy`. Avoid restarting in a tight loop -
Let's Encrypt rate-limits repeated failures.

## 8. Verify

```bash
cd ~/apps/nool-core
API_DOMAIN=api.example.in ORIGIN=https://admin.example.in ./scripts/prod-smoke.sh
```

All lines must say `ok` (http->https redirect, `/health`, auth health,
core rejecting an unauthenticated call with 401, HSTS, CORS allow-list).
Then every service's readiness from inside the network:

```bash
for s in auth core kg generation colearner; do
  printf '%-11s ' $s
  docker compose exec -T $s python -c "import urllib.request as u; print(u.urlopen('http://localhost:8000/ready').read().decode())" 2>&1 | tail -1
done
```

Each should report ready. A 503/error names what is missing (usually a
`.env` value or the Firebase file). Also open `https://schooladmin.example.in`
and `https://admin.example.in` in a browser (padlock, login page loads).

Confirm nothing but Caddy listens publicly:

```bash
sudo ss -tlnp    # public listeners: only :22 (sshd), :80 and :443 (docker-proxy)
```

## 9. Create the first Super Admin

The backend links a login to a `users` row by Firebase UID; every later
account is created from the consoles, but the first Super Admin is made by
hand:

1. Firebase console (noolai-prod) -> **Authentication -> Users -> Add
   user**: your email + a strong password. Copy the **User UID**.
2. Set the role claim (runs `services/auth/scripts/set_role.py` inside the
   auth container, which already has the Firebase credentials):

   ```bash
   cd ~/apps/nool-core
   docker compose exec -T auth python - --uid PASTE_UID --role SUPER_ADMIN --display-name "Your Name" \
     < services/auth/scripts/set_role.py
   ```

3. Create the application user:

   ```bash
   docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
   INSERT INTO users (id, firebase_uid, email, display_name, role, status)
   VALUES (gen_random_uuid(), 'PASTE_UID', 'you@example.in', 'Your Name', 'SUPER_ADMIN', 'ACTIVE');
   SQL
   ```

4. Sign in at `https://admin.example.in`. From there create schools and
   School Admins; School Admins create teachers and students.

## 10. Load content (curriculum, question bank, images)

A fresh database has only the schema. Content is loaded with the
`scripts/*.py` tools (see each script's header), which run on an operator
laptop, read the checkout's `.env` and connect to `localhost:POSTGRES_PORT_HOST`.
Production Postgres has no public port, so tunnel to it over SSH:

```bash
# (server) the postgres container's internal IP
docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' nool-core-postgres-1
# (laptop) forward local 15432 to it; leave running
ssh -N -L 15432:POSTGRES_IP:5432 deploy@SERVER_IP
```

Use a **separate checkout** on the laptop whose `.env` has the production
`POSTGRES_USER/PASSWORD/DB`, `POSTGRES_PORT_HOST=15432` and the production
`AZURE_STORAGE_CONNECTION_STRING`, so you can't mix it up with dev. Load
content only after step 9 (`import_dataset.py` attributes rows to the
Super Admin). Never run `scripts/e2e_*.py` against production - they
create test data ("E2E", "SMOKE e2e").

## 11. Backups

**On the server (automatic):** the `backup` service writes a `pg_dump`
every night at `BACKUP_HOUR_UTC` (default 21:00 UTC = 02:30 IST) into the
`nool_core_postgres_backups` volume and keeps `BACKUP_KEEP_DAYS` (7).

```bash
docker compose exec backup sh /backup.sh once     # take one now
./scripts/restore.sh                              # list dumps
```

**Off the server (you must set this up):** a backup on the same disk dies
with the server. Copy dumps nightly to Backblaze B2 (cheap, ~US$6/TB-month)
or Google Drive with rclone, encrypted (the database holds students'
personal data):

1. Backblaze: create a **private** bucket (e.g. `nool-db-backups`) with a
   lifecycle rule "keep only the last version, delete after 30 days", and
   an **application key restricted to that bucket**.
2. On the server:

   ```bash
   sudo apt -y install rclone
   sudo rclone config
   #  n) new remote "b2"     -> Backblaze B2, paste keyID + applicationKey
   #  n) new remote "b2crypt" -> Encrypt/Decrypt, remote = b2:nool-db-backups,
   #     filename encryption standard, generate a strong password + salt
   #     -> SAVE BOTH crypt passwords in the password manager (no password = no restore)
   ```

3. Optional dead-man switch: create a free check at <https://healthchecks.io>
   (period 1 day) so you get an email if a night's copy doesn't happen.
4. Nightly copy, an hour after the dump (server clock is UTC):

   ```bash
   sudo tee /etc/cron.d/nool-offsite-backup >/dev/null <<'EOF'
   30 22 * * * root rclone copy /var/lib/docker/volumes/nool_core_postgres_backups/_data b2crypt: --max-age 3d --log-file /var/log/rclone-nool.log && curl -fsS -m 10 https://hc-ping.com/YOUR-CHECK-UUID >/dev/null
   EOF
   ```

   (`copy`, not `sync`: the 7-day pruning on the server never deletes the
   off-site copies; B2's lifecycle rule does that after 30 days. Drop the
   `&& curl ...` part if you skipped step 3.)

**Restore drill (do it before launch, then monthly):** prove a dump from
B2 actually restores, without touching production:

```bash
f=$(sudo rclone lsf b2crypt: | sort | tail -1); echo "$f"
sudo rclone copy "b2crypt:$f" /tmp/drill/
docker run -d --name restore-drill -e POSTGRES_PASSWORD=drill nool-core-postgres:16-pgvector
sleep 10
docker cp "/tmp/drill/$f" restore-drill:/tmp/drill.dump
docker exec restore-drill sh -c 'createdb -U postgres drill && pg_restore -U postgres -d drill --no-owner --no-privileges /tmp/drill.dump && psql -U postgres -d drill -c "select count(*) as users from users"'
docker rm -f restore-drill && sudo rm -rf /tmp/drill
```

The user count should match production (`docker compose exec postgres sh -c
'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select count(*) from users"'`).

**Real restore** (destructive - replaces the database with the dump):
follow the header of `scripts/restore.sh` (stop core/auth/colearner,
`./scripts/restore.sh <file>`, start them again). To restore an off-site
dump, first copy it into the volume directory
`/var/lib/docker/volumes/nool_core_postgres_backups/_data/` with
`sudo rclone copy`.

Also keep a Hostinger **snapshot** (hPanel -> VPS -> Snapshots) before
risky changes like OS upgrades - it is a whole-server undo, not a database
backup.

## 12. Updating (redeploy)

```bash
cd ~/apps/nool-core
git fetch && git log --oneline HEAD..origin/main        # what's coming
git tag -f deploy-prev                                  # remember what's running now
docker compose exec backup sh /backup.sh once           # pre-deploy backup
git pull --ff-only
docker compose up -d --build                            # migrate runs before core starts
docker compose logs migrate | tail -5
API_DOMAIN=api.example.in ./scripts/prod-smoke.sh
docker image prune -f                                   # reclaim old image layers
```

Expect a few seconds of 502s while core/auth restart (single replica). Do
it outside school hours. Admin consoles: `git pull && docker compose build`
in their repo, then `docker compose up -d school-admin super-admin` in
nool-core.

**Zero-data-loss migrations ("expand, then contract"):** a release's
migration must keep working with the *previous* code too, so a rollback
never needs a downgrade:

- Add tables/columns as nullable or with a server default; backfill in the
  migration or a script.
- Don't rename or drop a column/table in the same release that stops using
  it. Ship the code that stops using it first; drop it in a later release.
- Review every new file in `database/migrations/versions/` before deploying:
  any `drop_column`, `drop_table`, `alter_column(... nullable=False)` or
  type change deserves a second look and a fresh backup.

## 13. Rollback

```bash
cd ~/apps/nool-core
git checkout deploy-prev
docker compose up -d --build
```

If the release's migrations were additive (section 12), that's it - old
code ignores new columns. If a migration has to be undone, downgrade it
**with the new code still checked out** (old code doesn't have the
migration file), then check out the old code:

```bash
docker compose run --rm migrate alembic downgrade -1    # once per migration to undo
git checkout deploy-prev && docker compose up -d --build
```

If data was damaged, restore the pre-deploy dump (section 11). Return to
the normal branch afterwards with `git checkout main`.

## 14. Monitoring

- **Uptime:** a free external monitor (UptimeRobot, Better Stack or
  similar), 5-minute interval, email/phone alert:
  - `https://api.example.in/auth/ready` - expect 200 (auth + Firebase config)
  - `https://api.example.in/api/v1/me` - expect **401** (proves nginx ->
    core works; a dead core gives 502). Use a monitor type that allows an
    expected status code.
  - `https://schooladmin.example.in/api/health`, `https://admin.example.in/api/health` - expect 200
  - Turn on the monitor's SSL-expiry alert too (Caddy renews automatically;
    this catches it if renewal ever breaks).
- **Backups:** the healthchecks.io check from section 11.
- **Costs:** OpenAI dashboard -> Limits: set a monthly budget and an
  email alert threshold for the production key.
- **Disk:** `df -h /` and `docker system df` monthly; keep usage under 80%.
- **Logs:** `docker compose logs --since 1h core` (rotated automatically, section 2).
- **Traces:** Jaeger has no public port. View it from the laptop through
  an SSH tunnel to its internal IP (as in section 10, port 16686) and open
  `http://localhost:16686`.

## 15. Cost and capacity

- The full stack idles at about **3 GB RAM**; KVM 4 (16 GB, 4 vCPU) leaves
  ample headroom for a pilot of a few schools. Voice audio doesn't pass
  through the server (phone <-> OpenAI directly), so the server's load is
  REST calls and grading; the expensive part is OpenAI usage, not the VPS.
- colearner keeps live session state in memory - run exactly one replica.
  If CPU or memory regularly stays above ~70% (`docker stats`), upgrade to
  KVM 8 in hPanel (in place, same IP) rather than adding servers.
- Monthly costs to expect: the VPS (check hPanel - longer terms are much
  cheaper per month, renewals cost more), the domain (yearly), Backblaze
  B2 (cents at this size), OpenAI usage (set the budget limit), and the
  Google Play developer account (US$25 once).

## Later: managed PostgreSQL

`CLAUDE.md` says production should prefer managed PostgreSQL; Docker
Postgres on the VPS is a pilot decision. When moving (e.g. to a managed
Postgres in Mumbai with pgvector support): make the host/port in
`compose.yml` (`POSTGRES_HOST: postgres`, `POSTGRES_PORT: 5432` for core and
migrate) come from `.env`, drop the `postgres` and `backup` services from
the production overlay (the provider does backups/point-in-time recovery),
restore the latest dump into the managed instance with `pg_restore`, and
set `POSTGRES_*` in `.env`. The application doesn't depend on
Docker-specific database behavior.

---

## Pre-launch checklist

Tick every line before real students use it. The app-side items are in
nool-apps `docs/RELEASE_ANDROID.md`.

**Secrets and config**
- [ ] `.env` filled from `.env.production.example`, `chmod 600`; no
      placeholder (`example.in`, empty `[SECRET]`) left
- [ ] Firebase is the **new production project** everywhere (core `.env`,
      both admin builds, the app build); service account file `chmod 600`
- [ ] Production `OPENAI_API_KEY` with a budget limit; Azure storage set
      (`ASSET_BASE_URL` not the azurite default)
- [ ] Copies of `.env`, the Firebase JSON and the rclone crypt passwords in
      the password manager
- [ ] Nothing secret committed: `git status` clean in every repo on the
      server and laptop

**Dev-only things off**
- [ ] `ENVIRONMENT=production`; playground returns 404:
      `curl -s -o /dev/null -w '%{http_code}\n' https://api.example.in/api/v1/colearner/playground` -> `404`
- [ ] Only ports 22/80/443 open: `sudo ss -tlnp` and an external port
      scan show no 5432/8080/16686/3100
- [ ] No azurite, no dev override (`docker compose ps` lists no `azurite`)
- [ ] Release app build has no dev sign-in hook and no cleartext HTTP
      (see `docs/RELEASE_ANDROID.md`)

**HTTPS and CORS**
- [ ] `scripts/prod-smoke.sh` all `ok`, with `ORIGIN` set to an admin console
- [ ] Padlock on all three hostnames; http redirects to https
- [ ] `NGINX_CORS_ORIGINS` = exactly the two admin console origins (no `*`, no localhost)

**Data**
- [ ] Fresh database from migrations only - no dev dump restored
- [ ] No test data: `select count(*) from question_papers where name like 'E2E%'`
      is 0, and no seed/test accounts in Firebase Authentication -> Users
- [ ] First Super Admin signs in; a test school created end to end, then
      deleted/deactivated

**Backups and monitoring**
- [ ] Nightly dump present (`./scripts/restore.sh` lists one)
- [ ] Off-site copy present in B2 and the **restore drill passed**
- [ ] Uptime monitors green and alerting to a phone/email someone reads
- [ ] OpenAI budget alert set; healthchecks.io backup check green
