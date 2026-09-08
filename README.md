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
GET /gateway/products
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
POST /admin-api/auth/register
POST /admin-api/auth/login
POST /admin-api/auth/refresh
```

Developer configuration:

```http
GET  /admin-api/projects
POST /admin-api/projects
GET  /admin-api/applications
POST /admin-api/applications
POST /admin-api/keys
POST /admin-api/keys/<id>/revoke
GET  /admin-api/services
POST /admin-api/services
GET  /admin-api/routes
POST /admin-api/routes
```

Backend proxy gateway:

```http
GET    /gateway/products
GET    /gateway/products/1
GET    /gateway/users/1
POST   /gateway/orders
GET    /gateway/orders/101
GET    /gateway/transactions
```

Analytics:

```http
GET /admin-api/analytics/summary
```

## Example Route Configuration

Create project:

```json
{
  "name": "Ecommerce Platform API"
}
```

Create partner/client applications:

```json
{ "name": "Nykaa", "plan": "normal" }
{ "name": "Myntra", "plan": "premium" }
{ "name": "Amazon", "plan": "premium" }
```

When an application is created, the gateway also creates its first API key and returns the raw key once in the response.

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
  "target_url": "http://fashion-product-service:8000/products",
  "auth_policy": "api_key_only"
}
```

Use `"auth_policy": "api_key_and_jwt"` for user-protected backend routes such as orders, profile, or payment.

Docker includes separate dummy backend services for end-to-end gateway demos. Example route target URLs:

```text
Fashion routes:
GET  /users    -> http://fashion-user-service:8000/users
GET  /products -> http://fashion-product-service:8000/products
POST /orders   -> http://fashion-order-service:8000/orders

Banking routes:
GET  /users    -> http://banking-user-service:8000/users
GET  /accounts -> http://banking-account-service:8000/accounts
POST /payments -> http://banking-payment-service:8000/payments
```

Each dummy backend returns JSON with its service name, method, and received path.

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

The gateway applies token bucket rate limiting before forwarding a request. The bucket is based on the API key, so one key has one shared limit across all routes it calls. The bucket capacity and refill speed come from the application's plan.

Example:

```text
Normal application: 100 requests/minute per API key
Premium application: 1000 requests/minute per API key
```

If Redis is available, bucket state is stored there. If Redis is unavailable during local development or tests, the gateway falls back to an in-memory bucket. When the limit is exceeded:

```text
Redis key    = gateway_rate:api_key:<api_key_id>
Redis fields = tokens, updated_at
TTL          = 120 seconds
```

On every request, the gateway refills tokens based on elapsed time, consumes one token if available, and blocks the request when no token is available.

```http
HTTP 429
```

```json
{
  "error": "Rate limit exceeded"
}
```

## Product Cache Design

GET responses are cached by the gateway for 60 seconds.

```text
First request  -> Gateway -> Service response -> cache response
Next request   -> Gateway -> Redis/cache -> return response
```

Non-GET requests invalidate cached GET responses inside the project.

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
fashion-user-service       Dummy backend service
fashion-product-service    Dummy backend service
fashion-order-service      Dummy backend service
banking-user-service       Dummy backend service
banking-account-service    Dummy backend service
banking-payment-service    Dummy backend service
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
