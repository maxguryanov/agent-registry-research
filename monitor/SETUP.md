# Setting it up

Seven steps, all in a browser. No terminal, no server, no installation.

Once done, the system runs itself: new events every hour, a fresh measurement
and a rebuilt page every night.

---

## 1. Create the database

Go to [neon.com](https://neon.com), sign up, create a project.

On the project page find **Connection string** and copy it. It looks like:

```
postgresql://neondb_owner:PASSWORD@ep-something-123456.eu-central-1.aws.neon.tech/neondb?sslmode=require
```

Two choices on that page are worth getting right.

**Region: keep the default US East.** The database is contacted by GitHub's
runners, not by your laptop, and those sit on the US east coast. A probe run
makes a couple of thousand round trips, so a database near the runners is
faster than one near you. You will rarely query it directly.

**Copy the direct connection string, not the pooled one.** If the host in the
string contains `-pooler`, switch *Connection pooling* off and copy again. A
transaction pooler does not hold session-level advisory locks, and the indexer
uses one to stop two runs overlapping. Nothing gets corrupted either way — the
inserts are idempotent — but the work would be done twice. The code warns if it
spots a pooled string.

The free tier is enough: this stores tens of megabytes a year against a 0.5 GB
allowance. Postgres 18 is fine; the schema was developed against 16 and uses
nothing version-specific.

## 2. Give the repository the connection string

In this repository: **Settings → Secrets and variables → Actions → New
repository secret**.

| Name | Value |
|---|---|
| `DATABASE_URL` | the connection string from step 1 |
| `PROBER_CONTACT` | an email address you actually read |

`PROBER_CONTACT` goes into the `User-Agent` of every request the prober makes.
Someone whose server you are contacting may want to reach you. Without it the
prober refuses to run.

Optionally, a third:

| Name | Value |
|---|---|
| `BASE_RPC_URLS` | comma-separated RPC endpoints, best first |

Only two of the five public Base endpoints can serve the historical log
queries this needs, so a free key from [Alchemy](https://alchemy.com) makes
step 5 faster and less likely to stall. Put it first:

```
https://base-mainnet.g.alchemy.com/v2/YOUR_KEY,https://mainnet.base.org,https://base.drpc.org
```

Leave the secret unset to use the public endpoints. That works; it is slower.

## 3. Let the workflows write to the repository

**Settings → Actions → General → Workflow permissions → Read and write
permissions → Save.**

The nightly job commits the rebuilt page. Without this it will fail at the
last step, after doing all the work.

## 4. Create the tables

**Actions → Operations → Run workflow → task: `create-tables` → Run.**

Takes a few seconds. When it finishes, open the run and check the log: it
should list eight tables and a cursor sitting at block 41,453,264, meaning
nothing has been indexed yet.

## 5. Load the history

**Actions → Operations → Run workflow → task: `backfill-history` → Run.**

This walks 8.9 million blocks from the registry's deployment to now: roughly
50 minutes with a private RPC key, longer on public endpoints. You can close
the tab; it keeps running.

If it stops before reaching the tip — the time budget runs out, an endpoint
misbehaves — **just run it again**. It resumes from where it stopped. Running
it twice on the same blocks cannot create duplicates.

Check progress any time with task `status`. When `blocks behind` is under a
few thousand, the history is loaded.

## 6. Take the first measurement

**Actions → Operations → Run workflow → task: `probe-now` → Run.**

This selects the panel of about 2,000 agents, checks every one of them, and
publishes the page. Expect 75 to 90 minutes.

## 7. Turn on the website

**Settings → Pages → Source: Deploy from a branch → Branch: `main`, folder:
`/docs` → Save.**

The folder `/docs` only appears in the dropdown after step 6 has committed it.
A minute later the page is live at:

```
https://maxguryanov.github.io/agent-registry-research/
```

That URL is also the API: `.../api/summary.json` and the rest.

---

## After that

Nothing. Two schedules take over:

| When | What |
|---|---|
| every hour, at :17 | new events from Base |
| every night, 03:40 UTC | probe the panel, rebuild the page, commit it |

GitHub emails you if a run fails.

---

## When something looks wrong

**First: Actions → Operations → task `status`.** It prints the cursor position,
how far behind the chain it is, the last run and its error if any, event counts
per type, and the panel composition. Most questions are answered there.

**A red run.** Open it and read the failed step. The cursor only moves when a
chunk is committed, so a failed indexer run has lost nothing — the next hourly
run continues. A failed probe run can be continued with task `full-sweep` and
`resume_run` set to its run id.

**The page shows old numbers.** Check that the nightly job ran. If a scheduled
workflow has stopped firing, GitHub disabled it after 60 days without
repository activity and emailed you about it; there is a button in the Actions
tab to switch it back on. The nightly commit normally prevents this.

**Someone asks not to be crawled.** Add their host to `excluded_hosts` in the
database. Neon's web console has a SQL editor:

```sql
INSERT INTO excluded_hosts (host, reason, requested_by)
VALUES ('example.com', 'operator asked by email', 'ops@example.com');
```

It takes effect on the next run and covers subdomains. Their agents are then
recorded as excluded rather than as dead, so the figures do not silently get
worse because someone asked for privacy.

**You want a full census rather than the panel.** Task `full-sweep` probes
every agent in the registry. It makes one request per agent to other people's
servers, so it is for preparing a report edition, not for routine use, and at
64,000 agents it may need to be resumed once.

---

## What it costs

| | |
|---|---|
| GitHub Actions | free on a public repository |
| Neon Postgres | free tier, ~0.5 GB, years of headroom |
| GitHub Pages | free |
| **Total** | **$0 per month** |

The paid tier becomes relevant if the registry grows by a factor of fifty, at
which point the daily probe stops fitting in one job. Not before.
