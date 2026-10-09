# Scraper maturity ladder

A five-tier yardstick for scrapers, built from how established open-source and
commercial tools are put together: Scrapy (AutoThrottle, stats collector,
`JOBDIR` pause/resume, feed exports), scrapy-redis (shared frontier across
workers), Crawlee / Apify (persistent request queue, autoscaled pool, run
statistics, scheduled actors), and the X-specific tools snscrape and twscrape.

Each tier assumes everything below it.

| Tier | Name | What it adds | Reference points |
|---|---|---|---|
| 1 | Script | One process fetches a page, parses it, prints or saves it. No retries; any error ends the run. | Typical tutorial scraper; this repo's original `twitter scraper.py` |
| 2 | Robust tool | Typed data model, parser tests against fixtures, retries with backoff, a rate limiter, several export formats, a real CLI. | snscrape |
| 3 | High-throughput engine | Async I/O with hundreds of connections, a rate budget shared by all workers and driven by the server's own headers, streaming output with flat memory, reproducible benchmarks. | Scrapy/Twisted core, Crawlee's HTTP crawler |
| 4 | Durable, observable crawler | A persistent job (frontier) that survives crashes and resumes where it stopped; dedup so nothing is fetched twice; failures parked in a dead-letter list and retried later; crawling outward along links (replies, quotes, retweets); live statistics (status codes, latency percentiles, retries, throughput) exported as JSON and Prometheus metrics; concurrency that adapts to server health; alarms when the payload shape drifts. | Scrapy `JOBDIR` + stats + AutoThrottle, Crawlee `RequestQueue` + `AutoscaledPool` |
| 5 | Distributed, continuously running service | Several worker processes share one frontier through leases that expire if a worker dies, and share one global rate budget; scheduled monitoring that re-scrapes targets on an interval, keeps an engagement time series and emits change events (new, edited, deleted, metrics moved) to files or webhooks; health endpoint. | scrapy-redis, Apify scheduled actors, commercial social-listening pipelines |

## Where xscraper sits

Before this work it was at **tier 3** (PR #4): an async engine, a
server-driven shared rate budget, streaming export and a benchmark harness.
A crash lost a run's progress, nothing followed replies or quotes beyond the
single `thread` command, and the only visibility was log lines.

It now covers **tiers 4 and 5**:

| Capability | Where |
|---|---|
| Persistent, deduplicated frontier with leases; resume after a crash | `xscraper/jobs.py`, `xscraper crawl` |
| Failures retried with backoff, then parked as failed (`job retry`) | `JobStore.complete` |
| Link following: reply parents, quotes, retweets, up to `--depth` | `jobs.links` |
| Run statistics as JSON and Prometheus, `/healthz` | `xscraper/metrics.py` |
| Adaptive (AIMD) concurrency | `metrics.AdaptiveLimit` |
| Payload-drift alarm | `metrics.DriftMonitor` |
| Several processes on one job, one job-wide rate budget and 429 pause | `xscraper/shared.py`, `crawl --processes` |
| Scheduled monitoring with change events, engagement history, webhook outbox | `xscraper/watch.py`, `xscraper watch` |

What it deliberately stops short of:

* **Multi-host.** The frontier and rate budget live in one SQLite file, so
  all workers run on one machine (or share one local volume). Going
  multi-host means moving `JobStore` and `shared.py` onto a server database
  such as Postgres or Redis; the lease protocol stays the same.
* **Scale beyond the rate budget**, for the reasons below.

## Boundary

Tier 5 here means scaling *out* under one rate budget, not multiplying the
budget. Commercial scrapers often reach their volume by rotating residential
proxies, pooling many accounts, spoofing browser fingerprints or solving
CAPTCHAs. Those exist to get around X's access controls, so they are out of
scope. The workers in tier 5 share a single budget that still follows X's
`x-rate-limit-*` signals; adding workers improves resilience and
parse/export capacity, not the request rate X allows.
