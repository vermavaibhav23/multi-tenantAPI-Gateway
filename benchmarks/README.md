# GateScale controlled gateway benchmark

This opt-in experiment runs the real gateway, not an endpoint that bypasses it.
Normal application files, settings, rate limits and Compose deployment are unchanged.
The branch builds on the forwarding fixes in PR #4.

## Stress results: 26 October 2026

The [increasing-load experiment](https://github.com/vermavaibhav23/multi-tenantAPI-Gateway/actions/runs/40303601383)
on commit `2292178fe8568d45ca2bb5721a6370834291cdfb` completed all 24 gateway trials:
five one-minute load levels plus a recovery trial in each of four configurations.
All configurations kept up at 100 and 200 RPS with zero dropped iterations, and
all overloaded at 400, 800 and 1,000 offered RPS. The exact transition between
200 and 400 RPS was not measured. This was one exploratory sweep per topology.

| Setup | p95 at 200 RPS | Successful RPS at 1,000 offered | p95 at 1,000 offered | Dropped iterations at 1,000 offered |
| --- | ---: | ---: | ---: | ---: |
| Baseline | 59 ms | 293.19 | 1.62 s | 42,063 |
| Redis Cluster | 65 ms | 279.76 | 1.95 s | 42,687 |
| MySQL replicas | 124 ms | 237.82 | 2.52 s | 45,360 |
| Both | 236 ms | 222.84 | 2.33 s | 46,334 |

Those 223-293 RPS values are completion plateaus during overload, not sustainable
loss-free capacity. k6 had a fixed 300-VU ceiling: slow responses occupied the
available VUs, causing scheduled work to remain unsent. Dropped iterations are
not requests rejected by the gateway. All 285,844 completed measured gateway
requests were valid, with zero 429, 5xx or transport errors; 338,176 scheduled
iterations were dropped across the overload trials. Every recovery trial returned
to approximately 100 RPS with zero drops and p95 of 17.5/20.2/28.2/34.4 ms.
All four direct-backend controls passed at 2,000 RPS for 30 seconds.

CPU samples point to gateway quotas and shared-host CPU contention. At 1,000
offered RPS, baseline and Redis-only gateways used approximately one full CPU
each, at their one-CPU quotas. Median host busy percentages were approximately
93%, 96%, 99% and 100% respectively. In the MySQL-cluster cases, the gateways,
Router and extra database processes competed for the same four host CPUs.
k6 used about 15-18% of one CPU (median), with ample memory headroom; sampled
MySQL/Redis CPU usage was below their individual quotas. This does not establish
the independent Redis or MySQL limits. Generator-concurrency sensitivity, longer
holds and repeated runs near the transition remain untested.

Evidence: [all trials](reports/2026-10-26-stress/comparison.json),
[environment](reports/2026-10-26-stress/environment.json),
[time-aligned CPU samples](reports/2026-10-26-stress/resources.json), and
[controls, topology and recovery](reports/2026-10-26-stress/verification.json).
The raw artifact records occasional Docker-stats sampling gaps; every measured
trial retained multiple k6/host samples. The workflow succeeded because the
experiment completed: 12 overload trials intentionally failed the performance
thresholds. It is not an all-performance-checks-passed result. Use **Re-run all jobs**
on the linked run to repeat the exact measured commit.

## Measured results: 26 October 2026 (IST)

The [full GitHub Actions run](https://github.com/vermavaibhav23/multi-tenantAPI-Gateway/actions/runs/40226915201)
completed all four cases on commit `ef93adbd6179fa508175e5766a405fe524dcd6a1`.
All 36 gateway trials passed: 57,629 successful measured requests, zero incorrect
responses, zero HTTP 429 responses and zero dropped iterations. Each of the four
backend controls also passed at 200 RPS.

At 100 offered requests/second, medians across three 30-second trials were:

| Setup | Successful RPS | p95 success (ms) | Range of trial p95 (ms) | Passed trials at 100 RPS |
| --- | ---: | ---: | ---: | ---: |
| Baseline | 100.01 | 20.99 | 19.69-22.13 | 3/3 |
| Redis Cluster | 100.01 | 22.65 | 22.31-25.91 | 3/3 |
| MySQL replicas | 100.00 | 36.40 | 32.73-38.50 | 3/3 |
| Both | 100.00 | 42.05 | 36.35-48.58 | 3/3 |

The clusters worked but did not improve latency at this load. All components and
k6 shared one 4-CPU runner with approximately 16 GiB RAM. These results establish
behavior at the tested rates, not maximum or production capacity. Fixed case order,
short trials and shared-host variation also limit comparisons.

All three gateway upstreams answered the preflight probe. Both MySQL replicas
received SELECTs (31,322/29,992 in the MySQL-only case; 30,584/30,747 in the combined
case), and all three Redis primaries processed commands in both cluster cases.
Counters include warmup, monitoring and Router metadata queries; they are not
exact measured-request counts. Resource samples also include diagnostic probes
and do not isolate load-generator CPU, so they alone cannot identify a bottleneck.

Versioned evidence: [all 36 trials](reports/2026-10-26/comparison.json),
[environment](reports/2026-10-26/environment.json), and
[topology/counter checks](reports/2026-10-26/verification.json).
Raw k6 summaries, logs and resource samples are attached to the run for 14 days.
To repeat this exact measured commit, open that run and choose **Re-run all jobs**.
The initial attempts exposed a benchmark restart-policy mistake and an inherited
Router-image authentication defect; the fixes are described below. The normal
deployment still uses the older Router image and needs that compatibility fix
separately; this benchmark does not validate that unchanged deployment.

## Experiment design

### Increasing-load stress experiment

An explicit `[run-stress]` commit runs all four setups sequentially at
100, 200, 400, 800 and 1,000 offered requests/second, for 60 seconds per level.
After a ten-second pause, a final 60-second trial at 100 RPS checks recovery.
This exploratory sweep has one trial per level; near-limit results need repetition
before being described as sustainable capacity. No latency SLA is assumed.

The generator preallocates 300 virtual users and keeps that fixed throughout,
avoiding runtime VU allocation during the sweep. It has one CPU and 1 GiB memory.
The same gateway, Redis and MySQL resource limits, API-key workload, 100 tenants,
cache behavior and 2,000/min/app limit are used in all setups. At 1,000 total RPS,
evenly distributed requests offer about 600/min/app, below that test limit.
A direct-backend control runs at 2,000 RPS for 30 seconds before each setup.

Resource sampling includes the one-off k6 container and host CPU counters/memory.
Per-trial timestamps allow analysis of samples within the measured request window,
separating load from most setup/inspection work. Successful HTTP p95/p99, all-request
elapsed p95, completed requests, 5xx/4xx/network errors, dropped iterations and
successful throughput including drain time are retained. Overload threshold failures
are measurements and do not abort subsequent levels. A green workflow means the
experiment completed; inspect individual trial thresholds for performance failures.

```sh
python -m benchmarks.run --rates 100 200 400 800 1000 --duration 60 --repeats 1 --vus 300 --recovery-rate 100
```

| Case | Gateway | Redis | MySQL |
| --- | --- | --- | --- |
| baseline | 3 behind NGINX | 1 standalone | 1 server |
| redis-cluster | 3 behind NGINX | 3 primaries + 3 replicas | 1 server |
| mysql-replicas | 3 behind NGINX | 1 standalone | 1 primary + 2 replicas, MySQL Router |
| combined | 3 behind NGINX | 3 primaries + 3 replicas | 1 primary + 2 replicas, MySQL Router |

All cases use three Gunicorn instances (2 workers, 4 threads each per instance),
NGINX round-robin balancing with retries disabled, DEBUG=False, the same database
connection lifetime, a controlled NGINX JSON backend, 100 generated tenant apps in one
project, explicit route permissions and the same rate limit of 2,000/min/app.
Both route and response caching remain enabled, as does the actual Redis Lua limiter.
Only benchmark settings change the rate limit; normal plans stay at 100/1,000.

The default experiment offers 10, 50 and 100 requests/second for 30 seconds each,
repeated three times per topology. k6 uses a constant-arrival-rate executor and
records dropped iterations if the generator cannot keep up. Each tenant alternates
four repeated requests with one unique URL across cycles. Overall, 80% of offered
requests use a hot URL and 20% are unique. Cache TTL is unchanged (60 seconds).
The hot path is `/gateway/products/1`; misses use other positive numeric IDs.
ID ranges do not overlap between measured runs within a configuration.
Warmup requests are excluded from custom correctness, successful latency and
throughput metrics. The built-in HTTP metrics also include warmup, so use the custom
metrics in comparison.json. Throughput divides successful completions by the offered
window or the last completion time, whichever is longer, including drain time.

## Explicit test-backend contract

The test backend implements `GET /products/{id}` for positive numeric IDs of up to
12 digits. It returns synthetic product JSON with the requested ID. This is a
defined fixture API, not a claim that the real business services implement that
endpoint or that those product records exist in a business database. No arbitrary
query parameters are used. Malformed/unknown paths return 404; query parameters on
the supported product path return 400. k6 checks the returned ID as well as status
and service name, so a wrong cached product or incorrectly forwarded path fails.
GateScale still has one configured route, `/products`; different IDs exercise
response-cache keys but not additional route-cache entries.

Before load, preflight checks verify the backend contract and confirm requests
actually reach three distinct gateway upstreams through the load balancer.
For future tests against real services, use only supported endpoints and seeded
records. If an endpoint has no meaningful varying inputs, test its cached path or
use a separate response-cache-disabled scenario; do not invent unsupported suffixes.

## Run away from a resource-constrained laptop

The `Gateway four-topology benchmark` GitHub Actions workflow runs all four cases
sequentially on the same hosted runner. It uploads results, topology checks, MySQL
query counters, Redis counters and sampled container CPU/memory usage. It never
uploads API keys. Ordinary branch pushes run configuration and lightweight NGINX
functional checks, using gateway stand-ins. To launch the full experiment before
merging, push a change under benchmarks/ or the workflow file to
codex/isolated-gateway-benchmark with `[run-benchmark]` in the head commit message.
This explicit opt-in runs all four cases after validation. After the workflow
exists on the default branch, use Actions > Gateway four-topology
benchmark > Run workflow to launch the full experiment on the selected branch.
For startup troubleshooting only, `[check-replicas]` runs a five-second, 10 RPS
check of the two MySQL-replica cases. It is not the full comparison.

Alternatively, on a Linux Docker host with at least 7 GiB available to Docker:

```sh
pip install PyYAML==6.0.2
python -m benchmarks.run
```

For a short smoke run:

```sh
python -m benchmarks.run --rates 10 --duration 10 --repeats 1
```

Generated configurations and keys are ignored under benchmarks/generated/.
Results are ignored under benchmarks/results/. The normal Compose project is not
started. Only the benchmark load balancer binds to 127.0.0.1:18080; the three
gateways have no published host ports. Databases and Redis
have no published host ports. Credentials are synthetic and only for this deployment.
Database name is forced to gatescale_benchmark. Containers are stopped after each
case, while project-scoped named volumes remain available for inspection.
MySQL containers retain an automatic restart policy because replica initialization
uses MySQL Clone, which shuts down and restarts each recipient server.
The benchmark uses Oracle's MySQL Router 8.0.46 image. The inherited Docker Hub
Router image failed with `caching sha2: Unknown packet for public key` during
PyMySQL authentication, before load could start. MySQL documents that defect
as bug 35015376, fixed in its
[8.0.33 release notes](https://dev.mysql.com/doc/relnotes/mysql-router/26.10/en/news-8-0-33.html).
This image override is benchmark-only; the normal deployment file is unchanged.
The [replica startup verification](https://github.com/vermavaibhav23/multi-tenantAPI-Gateway/actions/runs/40226546217)
passed both replica topologies, including read-only alias checks and real gateway
requests. Those five-second checks are setup validation, not capacity results.
The script refuses an existing running/stopped benchmark project; inspect it before
manually cleaning it up. Reusing retained volumes is a rerun, not a fresh-data trial.

## Interpret the evidence

Compare successful RPS, p95/p99 successful latency, invalid responses, 429s and
dropped iterations at the SAME offered rate. Check all repeats, not only the best.
The backend is separately tested at twice the highest target rate before each case.
Snapshots assert three ONLINE MySQL members and that the read alias reaches a
read-only replica; Redis topology must have three primaries and three replicas.
Per-node MySQL Com_select deltas show whether work reached the replicas.
Redis hits/misses include route metadata as well as response-cache operations;
they are NOT a pure response-cache hit ratio.

These are short, single-host trials. They do not establish production capacity,
failover behavior, write scalability, JWT performance or microservice performance.
All components and k6 share the host. Per-node resource budgets stay constant;
clusters have more total allocated resources, so this is not an equal-total-resource
efficiency comparison. Three gateways reduce the risk of a single gateway being
the bottleneck but do not guarantee that Redis/MySQL limits are reached. Shared
host CPU, the load balancer or the load generator may still limit throughput.
Report no improvement honestly rather than claiming that more nodes must be faster.
The 100 RPS maximum is an initial experiment, not a saturation target or capacity claim.
Increase offered load only after inspecting CPU/memory, generator drops and errors.
