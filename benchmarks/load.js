import http from 'k6/http';
import { check } from 'k6';
import exec from 'k6/execution';
import { Counter, Rate, Trend } from 'k6/metrics';

const tenants = JSON.parse(open('/private/credentials.json'));
const rate = Number(__ENV.RATE || 10);
const direct = __ENV.DIRECT === 'true';
const origin = direct ? 'http://benchmark-backend:8000' : 'http://load-balancer:8000/gateway';
const productOffset = Number(__ENV.PRODUCT_OFFSET || 1000);
const validResponses = new Rate('valid_responses');
const successfulDuration = new Trend('successful_duration', true);
const limited = new Rate('rate_limited');
const successes = new Counter('successful_requests');
const completion = new Trend('completion_elapsed_ms');

export const options = {
  scenarios: {
    traffic: {
      executor: 'constant-arrival-rate', rate, timeUnit: '1s',
      duration: __ENV.DURATION || '30s', preAllocatedVUs: 20, maxVUs: 200,
      gracefulStop: '15s',
    },
  },
  // Fail correctness and generator shortfalls, but retain measurements on failure.
  thresholds: {
    valid_responses: ['rate>0.99'], rate_limited: ['rate==0'],
    dropped_iterations: ['count==0'],
  },
  summaryTrendStats: ['avg', 'p(50)', 'p(95)', 'p(99)', 'max'],
};

export function setup() {
  // The same 100 hot URLs are warmed before every measured run.
  for (const tenant of tenants) {
    const r = http.get(`${origin}/products/1`, {
      headers: { 'X-API-Key': tenant.api_key }, tags: { name: 'warmup' }, timeout: '15s',
    });
    if (r.status !== 200 || r.json('service') !== 'benchmark-backend' || r.json('product.id') !== 1) {
      throw new Error(`Warmup failed: HTTP ${r.status}`);
    }
  }
}

export default function () {
  const iteration = exec.scenario.iterationInTest;
  const tenant = tenants[iteration % tenants.length];
  // Each tenant gets four hot requests then one unique request per five cycles.
  // Choosing iteration % 5 alone would pin all misses to the same 20 tenants.
  const miss = (Math.floor(iteration / tenants.length) + iteration % tenants.length) % 5 === 4;
  const productId = miss ? productOffset + iteration : 1;
  const response = http.get(`${origin}/products/${productId}`, {
    headers: { 'X-API-Key': tenant.api_key },
    tags: { name: miss ? 'unique' : 'hot' }, timeout: '15s',
  });
  let correct = false;
  try {
    correct = response.status === 200 && response.json('service') === 'benchmark-backend'
      && response.json('product.id') === productId;
  }
  catch (_) { /* Invalid JSON is a correctness failure. */ }
  check(response, { 'correct response': () => correct });
  validResponses.add(correct);
  limited.add(response.status === 429);
  completion.add(Date.now() - exec.scenario.startTime);
  successes.add(correct ? 1 : 0);
  if (correct) successfulDuration.add(response.timings.duration);
}

export function handleSummary(data) {
  return { '/results/summary.json': JSON.stringify(data, null, 2) };
}
