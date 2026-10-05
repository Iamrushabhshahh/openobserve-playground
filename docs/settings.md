# Settings, explained

Everything here is already set up for you. This page is for when you want to know *why*.

## The settings in `docker-compose.yml`

| Setting | What it does | Why it's on |
|---|---|---|
| Image `openobserve-enterprise:v1.0.4` | The OpenObserve version | Pinned, so a restart never upgrades your data by surprise. |
| `O2_SERVICE_STREAMS_ENABLED=true` | Lets OpenObserve figure out which logs, traces and metrics belong to the same service | Without it, "View Logs" on a trace opens an empty page. |
| `O2_SERVICE_STREAMS_SAMPLE_RATE=1` | Looks at every record, not a sample | A laptop sends little data, so sampling would miss things. |
| `O2_SERVICE_GRAPH_PROCESSING_INTERVAL_SECS=60` | Rebuilds the service graph every minute | Otherwise you'd wait a long time to see it. |
| `ZO_ENABLE_CROSS_LINKING=true` | Adds "jump to related" links on logs and traces | Makes moving between logs and traces easy. |
| `ZO_INGEST_ALLOWED_UPTO=87600` | Accepts data up to 10 years old | The default is 5 hours. `make demo` and history imports send older data. |
| `ZO_SKIP_SSRF_CHECKS=true` | Lets alerts call addresses on your own machine | So alerts can reach the little alert receiver. **Only for your laptop. Remove it on a shared server.** |

`make up` also adds a "local" setting to service discovery. Out of the box it only recognises
apps running in Kubernetes or the cloud. This teaches it to recognise apps on your laptop too.

## Which OpenObserve build?

The default is the **Enterprise** build, because service discovery (which powers "trace → logs")
only exists there. You can self-host it for free up to a daily data limit; see the
[license page](https://openobserve.ai/docs/enterprise-setup/license-and-pricing/). A laptop
uses a tiny fraction of that.

Prefer the open-source build? Add this to `.env`, delete `./data`, and run `make restart`:

```bash
O2_IMAGE=public.ecr.aws/zinclabs/openobserve:v1.0.4
```

Traces, logs and metrics all work. A few of the links between them won't.

## Send your own data

Use the login from your `.env`.

**Any OpenTelemetry SDK** (traces, logs and metrics):

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:5080/api/default
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic $(printf '%s:%s' "$ZO_ROOT_USER_EMAIL" "$ZO_ROOT_USER_PASSWORD" | base64),stream-name=my_app"
```

**A quick test log line**, no SDK needed:

```bash
source .env
curl -u "$ZO_ROOT_USER_EMAIL:$ZO_ROOT_USER_PASSWORD" \
  -d '[{"message":"hello from the playground","level":"info"}]' \
  http://localhost:5080/api/default/hello/_json
```

Then look in **Logs → stream `hello`**.

**Other ways in:**

| What | Address |
|---|---|
| OTLP over HTTP | `http://localhost:5080/api/default` (SDKs add `/v1/traces`, `/v1/logs`, `/v1/metrics`) |
| JSON logs | `POST http://localhost:5080/api/default/<stream>/_json` |
| Prometheus remote write | `http://localhost:5080/api/default/prometheus/api/v1/write` |

## Upgrade

Always try a new version on a **copy** of your data first. That's how this playground moved
from v0.92.2 to v1.0.4: the data upgraded itself on first start (database schema 64 → 77), and
every stream, dashboard and alert came through.

```bash
make backup                                                                     # save a copy first
echo 'O2_IMAGE=o2cr.ai/openobserve/openobserve-enterprise:<new-version>' >> .env
make restart
```

## Back up and restore

```bash
make backup          # writes backups/o2-data-<date>.tgz
```

To restore: `make down`, move `data` out of the way, unpack the backup, `make up`.

## All the commands

```text
make up                  start, wait until ready, set up service discovery
make demo                load example agent data + the Claude Code dashboards
make demo-live           keep adding example tasks
make claude-code         connect your real Claude Code + import its dashboards
make claude-code-alerts  add the Claude Code alerts (after your first prompt)
make alerts-log          watch alerts arrive
make status / logs       check on OpenObserve
make restart             apply changes to docker-compose.yml or .env
make backup / reset      save a copy / delete everything
make down                stop
```
