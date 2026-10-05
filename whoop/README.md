# WHOOP health data

See your WHOOP recovery, HRV, sleep and strain as OpenObserve dashboards, with alerts when
your recovery drops.

![WHOOP Readiness dashboard (made-up data)](../docs/images/whoop-readiness.jpg)

The part that talks to WHOOP lives in its own repo:
**[whoop-monitoring](https://github.com/Iamrushabhshahh/whoop-monitoring)**. This page shows
how to connect it to your playground.

## Set it up

1. Start the playground: `make up`
2. Get the collector, next to this folder:

   ```bash
   git clone https://github.com/Iamrushabhshahh/whoop-monitoring.git
   cd whoop-monitoring
   cp .env.example .env
   ```

3. In the collector's `.env`, use the **same login** as your playground:

   ```bash
   O2_URL=http://localhost:5080
   O2_URL_FROM_DOCKER=http://host.docker.internal:5080
   O2_USER=<the email from the playground .env>
   O2_PASSWORD=<the password from the playground .env>
   ALERT_WEBHOOK_URL=http://host.docker.internal:8080/o2-alert
   ```

4. Create a free WHOOP developer app and connect your account. The collector's
   [SETUP.md](https://github.com/Iamrushabhshahh/whoop-monitoring/blob/main/SETUP.md)
   walks you through it: `make login`, `make backfill`, `make provision`, `make up`.

## What you get

| Dashboard | Shows |
|---|---|
| Readiness | Recovery % (red / yellow / green), HRV, resting heart rate, blood oxygen, skin temperature |
| Sleep | Sleep stages each night, sleep needed vs slept, performance, breathing rate |
| Strain & Training | Daily strain, steps, calories, workouts, heart-rate zones |
| Collector Health | Is the collector running? Any errors? |

The collector also sends its own traces (`sync.run → sync.resource → whoop.request`), so it's
a small example of tracing a Python app.

<table>
  <tr>
    <td><img src="../docs/images/whoop-sleep.jpg" alt="Sleep dashboard (made-up data)"></td>
    <td><img src="../docs/images/whoop-strain.jpg" alt="Strain dashboard (made-up data)"></td>
  </tr>
</table>
