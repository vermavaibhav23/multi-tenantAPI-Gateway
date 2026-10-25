# GateScale controlled gateway benchmark

This opt-in experiment runs the real gateway, not an endpoint that bypasses it.
Normal application files, settings, rate limits and Compose deployment are unchanged.
The branch builds on the forwarding fixes in PR #4.

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
