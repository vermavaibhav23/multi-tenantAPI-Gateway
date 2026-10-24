# GateScale controlled gateway benchmark

This opt-in experiment runs the real gateway, not an endpoint that bypasses it.
Normal application files, settings, rate limits and Compose deployment are unchanged.
The branch builds on the forwarding fixes in PR #4.

| Case | Gateway | Redis | MySQL |
| --- | --- | --- | --- |
| baseline | 1 | 1 standalone | 1 server |
| redis-cluster | 1 | 3 primaries + 3 replicas | 1 server |
| mysql-replicas | 1 | 1 standalone | 1 primary + 2 replicas, MySQL Router |
| combined | 1 | 3 primaries + 3 replicas | 1 primary + 2 replicas, MySQL Router |

All cases use Gunicorn (2 workers, 4 threads each), DEBUG=False, the same database
connection lifetime, a fixed NGINX JSON backend, 100 generated tenant apps in one
project, explicit route permissions and the same rate limit of 2,000/min/app.
Both route and response caching remain enabled, as does the actual Redis Lua limiter.
Only benchmark settings change the rate limit; normal plans stay at 100/1,000.

The default experiment offers 10, 50 and 100 requests/second for 30 seconds each,
repeated three times per topology. k6 uses a constant-arrival-rate executor and
records dropped iterations if the generator cannot keep up. Each tenant alternates
four repeated requests with one unique URL across cycles. Overall, 80% of offered
requests use a hot URL and 20% are unique. Cache TTL is unchanged (60 seconds).
Warmup requests are excluded from custom correctness, successful latency and
throughput metrics. The built-in HTTP metrics also include warmup, so use the custom
metrics in comparison.json. Throughput divides successful completions by the offered
window or the last completion time, whichever is longer, including drain time.

## Run away from a resource-constrained laptop

The `Gateway four-topology benchmark` GitHub Actions workflow runs all four cases
sequentially on the same hosted runner. It uploads results, topology checks, MySQL
query counters, Redis counters and sampled container CPU/memory usage. It never
uploads API keys. The benchmark branch automatically runs it; after merge, use
Actions > Gateway four-topology benchmark > Run workflow.

Alternatively, on a Linux Docker host with at least 6 GiB available to Docker:

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
started. The benchmark gateway binds only to 127.0.0.1:18080. Databases and Redis
have no published host ports. Credentials are synthetic and only for this deployment.
Database name is forced to gatescale_benchmark. Containers are stopped after each
case, while project-scoped named volumes remain available for inspection.
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
efficiency comparison. A gateway CPU bottleneck may hide database improvements.
Report no improvement honestly rather than claiming that more nodes must be faster.
The 100 RPS maximum is an initial experiment, not a saturation target or capacity claim.
Increase offered load only after inspecting CPU/memory, generator drops and errors.
