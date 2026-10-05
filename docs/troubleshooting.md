# Troubleshooting

Find what you see, then try the fix.

### `make up` never finishes, and the logs say the password is "too weak"

OpenObserve wants a password with at least 8 characters, including an uppercase letter, a
lowercase letter, a number and a symbol. Change it in `.env`, then run `make restart`.

### I changed the password in `.env`, but the new one doesn't work

The login is saved the first time OpenObserve starts. After that, `.env` is ignored for it.
Change the password in the UI (**IAM → Users**), or run `make reset` to start over.

### Port 5080 is already in use

Another OpenObserve is probably running. Find it with `docker ps` and stop it, or change the
port in `docker-compose.yml`.

### I don't see a "View Trace" button on a log line

On the **Logs** page, open **More** and switch **Quick Mode** off. Quick Mode only loads the
columns on screen, so the trace link is missing.

### "View Logs" on a trace shows an empty "Pick a stream" page

OpenObserve hasn't linked that service to its logs yet. It checks every 10 minutes, so give it
a moment after new data arrives. In the meantime, open the span's **Logs** tab.

To see the logs of one trace only, add `AND trace_id = '<the id>'` to the logs search.

### The service graph is empty

It's rebuilt every minute from recent traces. Run `make demo-live` for a minute or two, then
refresh the page.

### A dashboard shows 0 or old numbers

Dashboards remember their last results. Click the refresh button at the top right.

### Two OpenObserve tabs keep logging each other out

Your browser shares one login cookie for everything on `localhost`, whatever the port. Open
the second one at `127.0.0.1:<port>` instead.

### "Too old data" when I send data

The `ZO_INGEST_ALLOWED_UPTO` line is missing from `docker-compose.yml`. Put it back and run
`make restart`.

### Creating an alert fails with "SSRF guard"

The alert points at your own machine and `ZO_SKIP_SSRF_CHECKS` isn't set. See
[settings](settings.md).

### My OTLP/JSON request fails with "invalid type: map, expected f64"

OpenObserve v0.92.2 doesn't accept decimal numbers (`doubleValue`) in OTLP sent as JSON. Use
protobuf (what SDKs use by default), or send the number as text.

### The Trace Graph or the service graph's Tree View looks squashed

That layout is buggy in this version. Use **Waterfall**, and the service graph's **Graph View**.
