# E-Commerce API Gateway Platform

## Problem Statement

This project simulates a real e-commerce platform where customer web/mobile apps communicate with one API Gateway endpoint instead of calling backend services directly. NGINX sits in front of multiple Django gateway instances and load balances traffic between them. Backend services are configured dynamically in the database, and each route stores one target URL.

## Architecture

```text
Customer Web/Mobile App
          |
          v
NGINX Load Balancer
          |
          +-- API Gateway Instance 1
          +-- API Gateway Instance 2
          +-- API Gateway Instance 3
          |
          +-- Any configured service
              examples: product-service, order-service, account-service, transaction-service
```

The gateway provides authentication, application API keys, dynamic routing, Redis-backed rate limiting, response caching, NGINX gateway load balancing, and request analytics.

## Request Flow Example

Customer request:

```http
GET /api/products
Authorization: Bearer <customer_jwt>
X-API-Key: ak_live_xxx
```

Gateway flow:

```text
1. Validate the application API key.
2. Validate the customer JWT.
3. Find the project linked to the API key.
4. Resolve GET /products to the configured Product Service route.
5. Apply rate limiting.
6. Return cached product response if available.
7. Forward to the route's target URL if one is configured, otherwise return a no-backend error.
9. Store request analytics.
```

## Why An API Gateway Is Needed

An API Gateway gives client apps one stable entry point while backend services can scale and change independently. It centralizes cross-cutting concerns that should not be duplicated inside every microservice:

- authentication
- API key validation
- rate limiting
- routing
- load balancing
- caching
- observability and analytics

## API Key Vs JWT

JWT identifies the customer/user making the request.

```http
Authorization: Bearer <customer_jwt>
```

API key identifies the client application or partner consuming the gateway, such as Nykaa, Myntra, or Amazon.

```http
X-API-Key: ak_live_xxx
```

API keys are stored securely as SHA-256 hashes. The raw key is returned only once when it is created.

## Main Django Apps

```text
apps/users              Developer accounts, projects, applications dashboard
apps/authentication     Login, refresh, API keys, application permissions
apps/routes             Dynamic service and route target URL configuration
apps/gateway            Middleware, route resolver, proxy, rate limit, cache
apps/analytics          Request logs and analytics summary
```

## Core APIs

Developer/customer auth:

```http
POST /api/auth/register
POST /api/auth/login
POST /api/auth/refresh
POST /login
```

Developer configuration:

```http
GET  /api/projects
POST /api/projects
GET  /api/applications
POST /api/applications
POST /api/keys
POST /api/keys/<id>/revoke
GET  /api/services
POST /api/services
GET  /api/routes
POST /api/routes
```

Customer-facing gateway:

```http
GET    /api/products
GET    /api/products/1
GET    /api/users/1
POST   /api/orders
GET    /api/orders/101
GET    /api/transactions
```

Legacy gateway path still works:

```http
GET /gateway/products
```

Analytics:

```http
GET /analytics
GET /api/analytics/summary
```

## Example Route Configuration

Create project:

```json
{
  "name": "Ecommerce Platform API",
  "plan": "free",
  "rate_limit": 100,
  "window_seconds": 60
}
```

Create partner/client applications:

```json
{ "name": "Nykaa", "plan": "normal" }
{ "name": "Myntra", "plan": "premium" }
{ "name": "Amazon", "plan": "premium" }
```

Create backend services:

```json
{ "name": "Product Service", "slug": "product-service" }
{ "name": "Order Service", "slug": "order-service" }
{ "name": "User Service", "slug": "user-service" }
```

For another project, services can be completely different:

```json
{ "name": "Account Service", "slug": "account-service" }
{ "name": "Transaction Service", "slug": "transaction-service" }
```

Create product route attached to the Product Service:

```json
{
  "name": "Product listing",
  "backend_service": 1,
  "method": "GET",
  "path": "/products",
  "cache_ttl_seconds": 60,
  "target_url": "http://real-product-service.internal/products"
}
```

If no target is configured for a route, the gateway returns:

```http
HTTP 503
```

```json
{
  "detail": "No healthy backends for this route."
}
```

## Rate Limiting Design

The gateway applies rate limiting before forwarding a request. The counter is based on the API key, so one key has one shared limit across all routes it calls. The limit comes from the application's plan, with optional custom numeric overrides.

Example:

```text
Normal application: 100 requests/minute per API key
Premium application: 1000 requests/minute per API key
```

If Redis is available, counters are stored there. If Redis is unavailable during local development or tests, the gateway falls back to an in-memory counter. When the limit is exceeded:

```text
Redis key   = gateway_rate:api_key:<api_key_id>
Redis value = request count
TTL         = 60 seconds
```

On every request, the gateway increments the counter. The first request sets the TTL. When the count becomes greater than the plan limit, the gateway blocks the request.

```http
HTTP 429
```

```json
{
  "error": "Rate limit exceeded"
}
```

## Product Cache Design

GET requests can be cached by setting `cache_ttl_seconds` on the route.

```text
First request  -> Gateway -> Service response -> cache response
Next request   -> Gateway -> Redis/cache -> return response
```

Non-GET requests invalidate cached responses for that route's backend service inside the project.

## Gateway Load Balancing Design

The project load balances at the gateway layer using NGINX. NGINX receives public traffic and distributes requests across three Django gateway containers.

```text
Client
  -> NGINX
      -> api-gateway-1
      -> api-gateway-2
      -> api-gateway-3
```

All gateway instances share the same MySQL and Redis:

```text
MySQL -> projects, applications, API keys, services, routes, logs
Redis -> rate limit counters and cached responses
```

Each route points to one backend `target_url`. Backend multi-instance service discovery can be added later as a future enhancement.

## Analytics

Every gateway request is logged with:

- user/application
- endpoint
- timestamp
- response status
- latency
- downstream service

Analytics response:

```json
{
  "total_requests": 5000,
  "average_latency": 80,
  "error_rate": 1.2
}
```

## Local Development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver 0.0.0.0:8000
```

On Windows:

```powershell
.venv\Scripts\activate
```

## Docker

```bash
cp .env.example .env
docker compose up --build
```

Docker services:

```text
nginx             Public entry point on 8000
api-gateway-1     Django gateway instance
api-gateway-2     Django gateway instance
api-gateway-3     Django gateway instance
db                MySQL shared by all gateway instances
redis             Redis shared by all gateway instances
```

## Testing

```bash
python manage.py test authentication users gateway analytics
```

The test suite covers:

- gateway routing
- invalid routes
- valid, invalid, and revoked API keys
- rate limiting
- product cache hit/miss behavior

## Scaling Discussion

For higher scale, the gateway can be extended with Redis Cluster for distributed rate limiting, Kubernetes or cloud load balancing for gateway replicas, Kafka for async analytics, and separate customer identity services. Backend services can later be scaled with service discovery or multiple backend instances behind their own load balancers.

Summary:

```text
I built an e-commerce API Gateway that acts as a single entry point between customer applications and backend microservices. It performs authentication, rate limiting, dynamic routing, caching, load balancing, and request analytics.
```
