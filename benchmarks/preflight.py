"""Functional checks before load; no measurements are claimed by these checks."""
from collections import Counter
import json
from pathlib import Path
import httpx


def verify(client, backend_url, gateway_url=None, api_key=None):
    for product_id in (1, 2, 123456):
        response = client.get(f'{backend_url}/products/{product_id}')
        assert response.status_code == 200, response.status_code
        assert response.json()['product']['id'] == product_id
    for path, expected in [('/products', 404), ('/products/0', 404),
                           ('/products/abc', 404), ('/products/1/missing', 404),
                           ('/products/1000000000000', 404), ('/unknown', 404),
                           ('/products/1?unsupported=1', 400)]:
        assert client.get(backend_url + path).status_code == expected, path
    distribution = Counter()
    if gateway_url:
        for index in range(30):
            product_id = 1 if index % 2 == 0 else index + 2
            response = client.get(f'{gateway_url}/products/{product_id}',
                                  headers={'X-API-Key': api_key} if api_key else {})
            assert response.status_code == 200, response.status_code
            assert response.json()['product']['id'] == product_id
            upstream = response.headers.get('X-Benchmark-Upstream')
            assert upstream, 'Missing upstream identity from load balancer'
            distribution[upstream] += 1
        assert len(distribution) == 3, f'Traffic did not reach all 3 gateways: {distribution}'
    return {'backend_contract': 'passed', 'gateway_response_counts': dict(distribution)}


if __name__ == '__main__':
    tenants = json.loads(Path('/private/credentials.json').read_text())
    with httpx.Client(timeout=15, trust_env=False) as client:
        print(json.dumps(verify(client, 'http://benchmark-backend:8000',
                                'http://load-balancer:8000/gateway', tenants[0]['api_key'])))
