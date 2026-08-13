# Deployment — putting the demo behind a URL

How to take the stack that runs on a laptop and expose it, over HTTPS, to a
handful of people scanning a QR code.

The short version: **one VM runs everything**, Caddy terminates TLS and serves
the UI, and the QR code points at the Azure DNS name. No second platform, no
CORS, no code changes.

---

## Why one box, and not a split frontend

This was nearly decided already, in [ui/src/api.js](../ui/src/api.js):

```js
// All calls go through /api, which Vite proxies to the FastAPI service in dev.
// One origin in the browser means no CORS configuration to get wrong.
const BASE = '/api'
```

The UI asks for a *relative* `/api/search`. In development,
[ui/vite.config.js](../ui/vite.config.js) proxies that to `127.0.0.1:8000` and
strips the `/api` prefix. So production needs exactly one thing: something that
does the same job as the Vite dev proxy. That is all Caddy is here for.

The consequence is worth stating plainly, because it is the reason this is easy:

- No `VITE_API_BASE_URL` to bake into the build.
- No `CORSMiddleware` in [api/main.py](../api/main.py) — the browser only ever
  sees one origin.
- The build that runs on your laptop is byte-for-byte the build that runs on the
  server.

Hosting the frontend separately (Vercel, Static Web Apps) would break all three.
It is a perfectly good setup and would take an afternoon, but it would mean
adding CORS configuration and a build-time API URL in order to get back to
where this already is. Rejected on those grounds — see D-014.

---

## What runs where

| Port | Service | Reachable from |
|---|---|---|
| 443 / 80 | Caddy | the internet |
| 8000 | FastAPI (uvicorn) | localhost only |
| 3838 | R Shiny dashboard | localhost only |
| 9092 | Kafka | localhost only |
| 8088 | ksqlDB | localhost only |
| 8081 | Schema Registry | localhost only |
| 8083 | Kafka Connect | localhost only |
| 5432 | PostgreSQL | localhost only |
| 6379 | Redis | localhost only |

Everything below 443 is bound to `127.0.0.1` in
[docker-compose.yml](../docker-compose.yml). This matters more than it looks:
Kafka, ksqlDB, Connect, Schema Registry, Postgres and Redis all run
unauthenticated PLAINTEXT. That is fine on a laptop and would be an open
database on a public IP. Containers talk to each other over the Docker network
(`kafka:29092`), and every host-side client runs on the same box, so loopback
binding costs nothing.

---

## Sizing

**16 GB RAM.** `Standard_B4ms` (4 vCPU / 16 GB, burstable) is the sweet spot.
Measured demand:

| Service | ~RSS |
|---|---|
| Kafka (`-Xmx1G`) | 1.3–1.6 GB |
| ksqlDB (`-Xmx1G`) | 1.3–1.6 GB |
| Connect (`-Xmx768M`) | 1.0–1.2 GB |
| Schema Registry | 0.4–0.6 GB |
| PostgreSQL | 0.2–0.3 GB |
| Redis (256 MB cap) | 0.3 GB |
| FastAPI + simulator + Shiny | 0.6–1.0 GB |
| **Total before headroom** | **~5.5–6.5 GB** |

8 GB will boot but leaves nothing for LightGBM training or a Playwright browser.
Disk: 64 GB StandardSSD is ample — four routes at seven-day retention is small.

Use a **Standard SKU public IP**, which is static, so the DNS name survives a
stop/start.

---

## Step 1 — VM and DNS name

Create the VM (Ubuntu 22.04 LTS, `Standard_B4ms`), then give its public IP a
**DNS name label**. Azure hands you a free hostname:

```
<label>.<region>.cloudapp.azure.com
```

This is what makes automatic TLS possible. `cloudapp.azure.com` is on the Public
Suffix List, so each label counts as its own registrable domain and Let's
Encrypt will issue for it. A bare IP address cannot get a certificate.

Set the NSG to allow **only**:

- 22 (SSH) — source restricted to your own IP, not `Any`
- 80 (HTTP) — Caddy needs it for the ACME challenge and the HTTPS redirect
- 443 (HTTPS)

Do not open 9092, 8088, 8083, 8081, 5432 or 6379. Nothing outside the box needs
them.

## Step 2 — Base packages

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-v2 python3-pip python3-venv \
                    r-base libpq-dev qrencode
sudo usermod -aG docker $USER   # log out and back in
```

R packages for the dashboard ([dashboard/app.R](../dashboard/app.R) needs
shiny, DBI, RPostgres, ggplot2, dplyr):

```bash
sudo R -e "install.packages(c('shiny','DBI','RPostgres','ggplot2','dplyr'), repos='https://cloud.r-project.org')"
```

This one is slow — 15–20 minutes compiling from source on a burstable VM. Start
it and do Step 3 in another SSH session.

Caddy:

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

## Step 3 — The project and its secrets

```bash
sudo mkdir -p /srv/naijafares && sudo chown $USER:$USER /srv/naijafares
# copy the project up (rsync, scp, or clone if you have put it in a repo)
cd /srv/naijafares
# A venv, not a system-wide pip install. Modern Ubuntu refuses the latter
# (PEP 668: "externally-managed-environment"), and `pip install --user` hides
# the packages from systemd, which runs as a specific User=. Both failures
# surface identically and unhelpfully as "No module named uvicorn".
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt -r requirements-ml.txt

# The systemd units call /srv/naijafares/.venv/bin/python, so verify it works:
.venv/bin/python -c "import uvicorn, fastapi, redis, psycopg; print('deps ok')" 
```

Then `cp .env.example .env` and **change the one default that is not a secret
today but will be on a public box**:

```bash
sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 24)/" .env
```

`naijafares_dev` is the default in both [.env.example](../.env.example) and
[common/config.py](../common/config.py). Fine on a laptop; not fine on a VM,
even with Postgres bound to loopback.

**Use hex, not a passphrase.** `postgres_dsn()` in
[common/config.py](../common/config.py) builds the URL by f-string with no
percent-encoding, so a password containing `@`, `:`, `/`, `?`, `#` or `%`
corrupts the DSN and produces a confusing connection error. `openssl rand -hex
24` is 96 bits of entropy in characters that cannot break it.

**Set it before Step 4, not after.** `POSTGRES_PASSWORD` is only read when the
container initialises an empty data directory. Once the `postgres-data` volume
exists, editing `.env` changes nothing and you need `ALTER USER` plus a
`docker compose down -v` or a manual reset.

`.env` is the only place the password lives. The JDBC sink carries a
`{{POSTGRES_PASSWORD}}` placeholder that `stream/deploy_connectors.py` fills at
deploy time, so no real credential is ever written to
[stream/connectors/](../stream/connectors/) — which matters, because `.env` is
gitignored and that directory is not. Check it resolved before deploying:

```bash
python stream/deploy_connectors.py --dry-run   # password shown as ***
```

Everything else in `.env` has a working fallback, which is deliberate — the
stack starts and the demo runs with no API keys at all, and credential-gated
carriers report `simulate` rather than silently fetching nothing (D-006).

**`ANTHROPIC_API_KEY` stays on the server.** It is read by
[api/nl_search.py](../api/nl_search.py) inside FastAPI. Because the frontend is
served as static files and never sees environment variables, there is no way for
it to leak into the browser bundle. This is another thing the single-origin
design gets for free.

## Step 4 — Bring the stack up

```bash
cd /srv/naijafares
./scripts/bootstrap.sh
```

That is the whole step. Do **not** hand-run `docker compose up -d` and the
deploy scripts individually — [bootstrap.sh](../scripts/bootstrap.sh) exists
because doing it by hand invites doing it half-right, and two of its steps
matter more on a fresh VM than they ever do on a laptop:

- It runs `docker compose build connect` first. The Connect image is built
  locally from [infra/connect/Dockerfile](../infra/connect/Dockerfile) rather
  than pulled, and older Compose versions ignore `pull_policy: build` and fail
  with "pull access denied" on a machine that has never built it. A new VM is
  exactly that machine.
- It waits for all six containers to report healthy and exits non-zero if they
  do not, rather than running `create_topics.py` against a broker that is not
  ready yet.

First run takes several minutes — the Connect image build dominates, and Connect
alone has a 60-second `start_period` with ksqlDB at 45. It is safe to re-run.

If it exits with `only N/6 services healthy`, check `docker compose ps` before
anything else. On a 16GB VM this is nearly always fine; on 8GB it is the JVM
services being starved.

**Ignore the script's closing suggestions on a server.** It ends by telling you
to run uvicorn, the simulator and `npm run dev` by hand, which is right on a
laptop and wrong here: those become systemd units in Step 6, and the UI is
served as a static build by Caddy rather than by Vite's dev server. Continue
with Step 5.

## Step 5 — Build the UI

```bash
cd /srv/naijafares/ui
npm ci
npm run build                       # writes ./dist - must succeed before the copy

# The web root is a SEPARATE directory from the source. Serving
# /srv/naijafares/ui directly would publish src/, package.json and
# node_modules/ along with the site.
sudo rm -rf /srv/naijafares/web
sudo mkdir -p /srv/naijafares/web
sudo cp -r dist/. /srv/naijafares/web/
```

Check it landed before moving on - `index.html` and an `assets/` folder:

```bash
ls /srv/naijafares/web
```

`rm -rf` first because Vite fingerprints filenames (`index-a1b2c3.js`), so
without it every redeploy leaves the previous build's assets behind for ever.
`dist/.` rather than `dist/*` copies dotfiles too, and does not fail the way a
glob does when the directory is empty.

No environment variables at build time. That is the point.

## Step 6 — Run API, simulator and dashboard as services

Three systemd units, so they survive a reboot and restart on crash. Templates
are in [infra/systemd/](../infra/systemd/).

```bash
cd /srv/naijafares
sudo cp infra/systemd/*.service /etc/systemd/system/

# The units ship with User=CHANGE_ME on purpose - the right account differs
# per machine. This sets it to whoever is running the deploy.
sudo sed -i "s/^User=.*/User=$USER/" /etc/systemd/system/naijafares-*.service
grep -h '^User=' /etc/systemd/system/naijafares-*.service | sort -u   # verify

sudo systemctl daemon-reload
sudo systemctl enable --now naijafares-api naijafares-simulator naijafares-dashboard
sudo systemctl status naijafares-api --no-pager
```

Two failures worth recognising, because neither says what it means:

| Message | Cause |
|---|---|
| `Unit file naijafares-api.service does not exist` | The `cp` above was skipped |
| `Unknown user CHANGE_ME` / status `217/USER` | The `sed` above was skipped, or names an account that does not exist |

The Python services run `/srv/naijafares/.venv/bin/python`, so the venv must
exist and be readable by the account in `User=`. `No module named uvicorn` in
the journal means the venv was never created, or the install went elsewhere.

## Step 7 — Caddy

```bash
sudo cp /srv/naijafares/infra/caddy/Caddyfile /etc/caddy/Caddyfile
# Do NOT type the region from memory. That suffix is your VM's REGION, not a
# fixed value - a VM in switzerlandnorth is not *.uksouth.cloudapp.azure.com,
# and Caddy fails with NXDOMAIN on a name that does not exist.
# Read the real one from Azure (Portal: Public IP > Configuration > DNS name):
#   az network public-ip list --query "[].dnsSettings.fqdn" -o tsv
FQDN=<paste the fqdn from Azure>
echo "SITE_ADDRESS=$FQDN" | sudo tee /etc/caddy/caddy.env
sudo mkdir -p /var/log/caddy && sudo chown caddy:caddy /var/log/caddy
```

Caddy needs to be told to read that env file. Write the drop-in directly
rather than using `sudo systemctl edit caddy` - that opens an editor showing the
existing unit **commented out**, and typing the two lines into that block leaves
`EnvironmentFile=` outside any section, where systemd ignores it:

```bash
sudo mkdir -p /etc/systemd/system/caddy.service.d
sudo tee /etc/systemd/system/caddy.service.d/env.conf > /dev/null <<'EOF'
[Service]
EnvironmentFile=/etc/caddy/caddy.env
EOF
```

Now check systemd actually registered it. `systemctl cat | tail` is not a real
check - drop-ins print after the main unit, so a few lines of tail can easily
show only Caddy's own `[Install]` block and tell you nothing:

```bash
sudo systemctl daemon-reload
sudo systemctl show caddy -p EnvironmentFiles
```

Expect `EnvironmentFiles=/etc/caddy/caddy.env (ignore_errors=no)`. **Empty output
means it is not loaded** - check `ls -l /etc/systemd/system/caddy.service.d/`,
since `tee` fails silently to stdout if the directory was never created.

Then:

```bash
sudo systemctl restart caddy
sudo journalctl -u caddy -f      # watch the certificate being issued
```

**Before restarting, confirm DNS resolves.** Caddy cannot get a certificate for
a name that does not exist, and the failure is noisy but easy to misread:

```bash
dig +short "$FQDN"        # must return the VM's public IP
curl -s ifconfig.me                                  # what that IP should be
```

| Log message | Cause |
|---|---|
| `NXDOMAIN looking up A for ...` | The DNS name label was never set on the public IP (Step 1), or `SITE_ADDRESS` does not match the name that exists |
| challenge times out / connection refused | Ports 80 and 443 are not open in the Network Security Group. Let's Encrypt validates by connecting **inbound** |
| `too many failed authorizations` | Let's Encrypt rate limit from repeated failures - fix DNS first, then wait an hour |

Set the label with:

```bash
az network public-ip list -o table
az network public-ip update -g <resource-group> -n <ip-name> --dns-name naijafares
```

Certificate issuance takes a few seconds. If it fails, it is almost always port
80 being closed in the NSG — the ACME challenge needs it.

## Step 8 — Verify, then make the QR code

`$FQDN` was a shell variable set back in Step 7. If this is a new session it is
empty, and everything below silently targets `https://` — including the QR code,
which then scans to nothing. Read it from the file that actually holds it:

```bash
FQDN=$(sudo awk -F= '/^SITE_ADDRESS=/{print $2}' /etc/caddy/caddy.env)
echo "FQDN=$FQDN"        # must print the full hostname, not just "FQDN="

curl "https://$FQDN/api/health"
```

Test in this order. Each step rules out the one before it:

1. `curl localhost:8000/health` — is the API up?
2. `curl localhost:8000/search?origin=LOS&dest=ABV&date=...` — is there data?
3. `curl https://<name>/api/health` — is Caddy's prefix strip right?
4. Open `https://<name>/` in a browser — does the UI load and search?
5. Open `https://<name>/dashboard/` — does Shiny render?

A 404 at step 3 that works at step 1 means `handle_path` became `handle` in the
Caddyfile. That is the single most likely mistake in this whole document.

Then:

```bash
# Re-read it here too: this block is often run on its own, later.
FQDN=$(sudo awk -F= '/^SITE_ADDRESS=/{print $2}' /etc/caddy/caddy.env)
echo "encoding: https://$FQDN"
qrencode -o demo-qr.png -s 10 -l H "https://$FQDN"
```

Print at 3 cm or larger, with the URL as text underneath — someone's camera will
fail and they will type it. Test on both an iPhone and an Android; they use
different scanners.

## Optional — restrict who can use it

Anything with a public URL is reachable by anyone who scans it. If that matters,
add basic auth to the Caddyfile's final `handle` block:

```
basic_auth {
    demo <hash from: caddy hash-password>
}
```

Shared password on your slide.

---

## Cost, which is the real constraint

A 16 GB VM runs roughly **$120–150/month** at 24/7. Azure for Students is $100
of credit. Left running, that credit is gone in under three weeks — well before
demo day.

**Develop locally.** The stack was built to run on one laptop (D-001) and still
does. Use the VM for integration testing and the demo window only.

```bash
# stop paying for compute (disk still costs a few dollars a month)
az vm deallocate -g <group> -n <vm>
# ~20 minutes before the demo
az vm start -g <group> -n <vm>
```

Deallocate, do not just shut down from inside the guest — a stopped-but-allocated
VM still bills. And start it **20 minutes ahead**, not five: the stack needs
several minutes to go healthy, and a dead URL behind a printed QR code is worse
than no QR code.

If your rubric does not mandate Azure, a 16 GB box at Hetzner is around €15/month
and nothing in this document changes except Steps 1 and the `az` commands.

---

## Demo-day checklist

- [ ] VM started ≥20 minutes ahead
- [ ] `docker compose ps` — all six healthy
- [ ] `systemctl is-active naijafares-api naijafares-simulator naijafares-dashboard`
- [ ] `curl https://<name>/api/health` returns OK
- [ ] Certificate not expiring (`caddy` renews automatically, but check)
- [ ] Simulator producing — Redis key count rising
- [ ] Recorded walkthrough on your laptop as a fallback

That last one matters. [AGENT_INSTRUCTIONS.md](../AGENT_INSTRUCTIONS.md) already
makes the simulator the answer to a flaky *source* on demo day. A video is the
answer to a flaky *network*, which is the one failure no amount of this document
prevents.

---

## Starting over from cold

Safe while the box holds nothing you want to keep. Images are not removed, so
the Connect build is not repeated and this is far quicker than the first run.

**The rule that makes this necessary:** `POSTGRES_PASSWORD` is only read when
the container initialises an *empty* data directory. Once the `postgres-data`
volume exists, editing `.env` changes nothing — and because
[common/config.py](../common/config.py) resolves an empty variable to `""`
rather than to its default, while
[docker-compose.yml](../docker-compose.yml) resolves it to `naijafares_dev`
via `${VAR:-default}`, the two ends disagree and you get
`fe_sendauth: no password supplied` from three layers away.

```bash
cd /srv/naijafares

# 1. stop anything holding connections
sudo systemctl stop naijafares-api naijafares-simulator naijafares-dashboard 2>/dev/null || true

# 2. destroy containers and volumes (postgres, kafka, redis)
docker compose down -v --remove-orphans
docker volume ls | grep naijafares          # must print nothing

# 3. set the password BEFORE anything starts
NEWPW=$(openssl rand -hex 24)
[ ${#NEWPW} -eq 48 ] || echo "openssl produced nothing - stop here"
sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$NEWPW|" .env
grep '^POSTGRES_PASSWORD=' .env

# 4. an exported variable beats the file - load_dotenv does not override it
unset POSTGRES_PASSWORD

# 5. verify what Python and the sink will actually use
python -c "from common.config import POSTGRES_PASSWORD as p; print(len(p))"   # 48, not 0
python stream/deploy_connectors.py --dry-run | grep connection.

# 6. rebuild
./scripts/bootstrap.sh
```

Steps 5 and 6 exist because the failure they catch is silent until something
far downstream cannot connect.

Then load data and check the sinks actually wrote — a connector reporting
RUNNING proves nothing (D-009):

```bash
python -m simulator.run --once
docker compose exec -T postgres psql -U naijafares -d naijafares \
  -c "select count(*) from offers_history;"
docker compose exec -T redis redis-cli dbsize
python -m ml.backfill
python -m ml.train
```

That leaves you at the end of Step 4. Continue from Step 5.

---

## Ruled out, and why

**Azure Event Hubs as managed Kafka.** Looks ideal — no broker to run. It does
not work here: Event Hubs has no log compaction, and ksqlDB requires compacted
internal topics for its state stores. Phase 2 is entirely ksqlDB. This is an
expensive dead end to discover late.

**Azure Container Apps / AKS.** The same six containers, more YAML, more money,
and a Kubernetes debugging surface during a live demo.

**App Service multi-container.** Retired, and the sidecar model that replaced it
is not somewhere to run a Kafka broker.

**Confluent Cloud + managed Postgres/Redis.** The right production answer and
the wrong student-project answer: real money, and it moves the streaming layer
being assessed off your own infrastructure.
