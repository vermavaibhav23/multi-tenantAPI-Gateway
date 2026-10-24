"""Sequential, isolated four-topology experiment. Requires a Linux Docker engine."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import statistics
import threading
import time

from benchmarks.compose import CASES, ROOT, generate


def command(args, *, capture=False, check=True, timeout=1200):
    result = subprocess.run(args, cwd=ROOT, text=True, encoding='utf-8',
                            errors='replace', capture_output=capture, timeout=timeout)
    if check and result.returncode:
        if capture:
            print(result.stdout, result.stderr, flush=True)
        raise RuntimeError(f'Command failed ({result.returncode}): {args[:5]}')
    return result


def save_command(path, args, check=True):
    result = command(args, capture=True, check=check)
    path.write_text(result.stdout + ('\n' + result.stderr if result.stderr else ''), encoding='utf-8')
    return result


def sample_resources(compose, path, stop):
    with path.open('w', encoding='utf-8') as output:
        while not stop.is_set():
            try:
                ids = command(compose + ['ps', '-q'], capture=True, timeout=20).stdout.split()
                if ids:
                    stats = command(['docker', 'stats', '--no-stream', '--format', '{{json .}}', *ids],
                                    capture=True, timeout=20)
                    output.write(json.dumps({'time': datetime.now(timezone.utc).isoformat(),
                                             'stats': [json.loads(line) for line in stats.stdout.splitlines()]}) + '\n')
                    output.flush()
            except Exception as exc:
                output.write(json.dumps({'sampling_error': str(exc)}) + '\n')
            stop.wait(3)


def measure(compose, directory, rate, duration, run_id, direct=False):
    directory.mkdir(parents=True, exist_ok=True)
    # k6 runs as root only inside its isolated container to write mounted artifacts.
    args = compose + ['run', '--rm', '--no-deps', '-v', f'{directory.as_posix()}:/results',
        '-e', f'RATE={rate}', '-e', f'DURATION={duration}s', '-e', f'RUN_ID={run_id}',
        '-e', f'DIRECT={str(direct).lower()}', 'load', 'run', '/scripts/load.js']
    result = save_command(directory / 'k6.log', args, check=False)
    summary = directory / 'summary.json'
    if not summary.exists():
        raise RuntimeError(f'k6 did not produce measurements: {directory}')
    data = json.loads(summary.read_text())
    metrics = data['metrics']
    def value(metric, field, default=0):
        return metrics.get(metric, {}).get('values', {}).get(field, default)
    elapsed = max(duration, value('completion_elapsed_ms', 'max') / 1000)
    row = {
        'run': run_id, 'target_rps': rate, 'duration_seconds': duration,
        'completion_window_seconds': elapsed,
        'successful_requests': value('successful_requests', 'count'),
        'successful_rps': value('successful_requests', 'count') / elapsed,
        'p95_success_ms': value('successful_duration', 'p(95)', None),
        'p99_success_ms': value('successful_duration', 'p(99)', None),
        'invalid_response_fraction': 1 - value('valid_responses', 'rate'),
        'rate_limited_fraction': value('rate_limited', 'rate'),
        'dropped_iterations': value('dropped_iterations', 'count'),
        'thresholds_passed': result.returncode == 0,
    }
    print(json.dumps(row), flush=True)
    return row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cases', nargs='+', choices=list(CASES), default=list(CASES))
    parser.add_argument('--rates', nargs='+', type=int, default=[10, 50, 100])
    parser.add_argument('--duration', type=int, default=30)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--skip-build', action='store_true')
    args = parser.parse_args()
    if min([*args.rates, args.duration, args.repeats]) < 1:
        parser.error('Rates, duration and repeats must be positive')
    if max(args.rates) > 1000:
        parser.error('Above 1000 RPS requires revisiting rate-limit and load-generator headroom')
    info = json.loads(command(['docker', 'info', '--format', '{{json .}}'], capture=True, timeout=30).stdout)
    if info['OSType'] != 'linux':
        raise RuntimeError('A Linux Docker engine is required')
    if info['MemTotal'] < 6 * 1024**3:
        raise RuntimeError('Use a runner with at least 6 GiB available to Docker; do not overload a small laptop')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    output = ROOT / 'benchmarks' / 'results' / stamp
    output.mkdir(parents=True)
    metadata = {
        'started_at': stamp, 'git_sha': command(['git', 'rev-parse', 'HEAD'], capture=True).stdout.strip(),
        'cpu_count': info['NCPU'], 'docker_memory_bytes': info['MemTotal'],
        'docker_version': info['ServerVersion'], 'os': info['OperatingSystem'],
        'kernel': info['KernelVersion'], 'parameters': vars(args),
        'gateway_workers': 2, 'gateway_threads_per_worker': 4, 'gateway_cpu_limit': 1,
        'tenants': 100, 'per_tenant_limit_per_minute': 2000,
        'workload': 'GET /products; 80% hot URL, 20% unique URL; real API-key and explicit permission checks',
        'limitations': ['Single host, shared CPU/memory and load generator',
                       'Short repeated trials, not a production-capacity or failover claim',
                       'API-key-only GETs; no JWT, business transactions or database write benchmark',
                       'Cluster cases add resources; per-node budgets are held constant, not total budgets',
                       'Requested hot ratio is not a measured cache-hit ratio; expiry and eviction can change it'],
    }
    (output / 'environment.json').write_text(json.dumps(metadata, indent=2))
    if not args.skip_build:
        command(['docker', 'build', '-f', 'benchmarks/Dockerfile', '-t', 'gatescale-benchmark:local', '.'])
    command(['docker', 'pull', 'grafana/k6:0.54.0'])
    rows, failures = [], []
    for case in args.cases:
        print(f'=== Starting isolated benchmark: {case} ===', flush=True)
        config = generate(case)
        compose = ['docker', 'compose', '--project-directory', str(ROOT), '-p',
                   f'gatescale-bench-{case}', '-f', str(config)]
        directory = output / case
        directory.mkdir()
        save_command(directory / 'compose.json', compose + ['config', '--format', 'json'])
        stop = threading.Event()
        sampler = None
        created = False
        try:
            # Fail closed if this dedicated project already exists; never erase a previous run.
            if command(compose + ['ps', '-a', '-q'], capture=True).stdout.strip():
                raise RuntimeError(f'{case}: benchmark project already has containers; inspect it first')
            created = True
            command(compose + ['up', '-d', '--wait', '--wait-timeout', '600', 'gateway'])
            sampler = threading.Thread(target=sample_resources,
                                       args=(compose, directory / 'resources.jsonl', stop), daemon=True)
            sampler.start()
            inspection = compose + ['exec', '-T', 'gateway', 'python', '-m', 'benchmarks.inspect_stack']
            save_command(directory / 'before.json', inspection)
            # Demonstrate that the controlled backend can serve more than the target load.
            control = measure(compose, directory / 'backend-control', max(args.rates) * 2,
                              args.duration, f'{case}-backend', direct=True)
            if not control['thresholds_passed']:
                raise RuntimeError('Controlled backend failed; gateway comparison would be misleading')
            for repeat in range(1, args.repeats + 1):
                for rate in args.rates:
                    run_id = f'{case}-r{repeat}-{rate}rps'
                    run_dir = directory / run_id
                    run_dir.mkdir()
                    save_command(run_dir / 'before.json', inspection)
                    row = measure(compose, run_dir, rate, args.duration, run_id)
                    row.update(case=case, repeat=repeat)
                    rows.append(row)
                    save_command(run_dir / 'after.json', inspection)
                    (output / 'comparison.json').write_text(json.dumps(rows, indent=2))
                    # Saturation is a measured outcome; continue the remaining comparisons.
            save_command(directory / 'after.json', inspection)
        except Exception as exc:
            failures.append({'case': case, 'error': str(exc)})
            print(f'{case}: {exc}', flush=True)
        finally:
            stop.set()
            if sampler:
                sampler.join(timeout=25)
            save_command(directory / 'containers.json', compose + ['ps', '-a', '--format', 'json'], check=False)
            save_command(directory / 'containers.log', compose + ['logs', '--no-color', '--tail', '150'], check=False)
            # Only the dedicated project created for this experiment is cleaned up.
            # Named benchmark volumes persist for inspection; normal app resources are untouched.
            if created:
                command(compose + ['down'], check=False)
    (output / 'failures.json').write_text(json.dumps(failures, indent=2))
    (output / 'comparison.json').write_text(json.dumps(rows, indent=2))
    report = ['# GateScale four-topology results', '',
              f'Commit: `{metadata["git_sha"]}`. Three repeats by default; values below are medians of run summaries.', '',
              '| Setup | Offered RPS | Successful RPS | p95 success (ms) | Passed runs |',
              '| --- | ---: | ---: | ---: | ---: |']
    for case in args.cases:
        for rate in args.rates:
            group = [row for row in rows if row['case'] == case and row['target_rps'] == rate]
            if group:
                p95 = [row['p95_success_ms'] for row in group if row['p95_success_ms'] is not None]
                latency = f'{statistics.median(p95):.2f}' if p95 else 'N/A'
                report.append(f'| {case} | {rate} | '
                              f'{statistics.median(row["successful_rps"] for row in group):.2f} | '
                              f'{latency} | {sum(row["thresholds_passed"] for row in group)}/{args.repeats} |')
    report += ['', 'These are shared-host, short-duration results, not production capacity or proof of horizontal scaling.',
               'Both caching and rate limiting remained active. See environment.json, raw summaries and resource samples.',
               f'Infrastructure failures: {json.dumps(failures)}']
    report_text = '\n'.join(report) + '\n'
    (output / 'report.md').write_text(report_text)
    print(report_text, flush=True)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as summary:
            summary.write(report_text)
    print(f'Results: {output}', flush=True)
    if failures:
        raise SystemExit(1)


if __name__ == '__main__':
    main()

