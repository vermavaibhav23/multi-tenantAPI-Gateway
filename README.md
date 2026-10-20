# ServLink — Multi-Tenant Backend-as-a-Service (BaaS) Platform

**A shared backend services platform powered by a multi-tenant API Gateway.**

Small businesses often have limited development teams and budgets. ServLink explores a way for multiple businesses to access reusable, industry-specific backend services through one central platform, rather than each business building the same infrastructure from scratch.

At the heart of ServLink is a **Django REST Framework API Gateway**. It identifies each business through an API key, routes requests to services in its industry project, enforces permissions and subscription-plan rate limits, applies safe response caching, and records request analytics.

### What the platform demonstrates

| Area | Implementation |
|---|---|
| **Shared service bundles** | E-commerce, Healthcare and Banking projects, each grouping three demo backend services |
| **Multi-tenancy** | Multiple business applications share a project's services while retaining distinct API keys, rate limits and analytics |
| **Gateway functionality** | API-key authentication, optional JWT policies, dynamic routing, route permissions and request forwarding |
| **Performance** | Redis Lua-based token-bucket rate limiting and 60-second caching of eligible GET responses |
| **Scalability** | NGINX round-robin load balancing across three Dockerized Django gateway instances |
| **Persistent data** | MySQL for projects, applications, service/route configuration and request logs |

**Technology stack:** Python, Django REST Framework, MySQL, Redis, NGINX, Docker / Docker Compose, HTTPX, JWT.

**Important scope note:** ServLink is an **architectural demonstration**, not a production-ready BaaS product. Its Dockerized backend microservices are lightweight request-echo services that confirm the service name, application ID, HTTP method and path. They do **not** implement real CRUD, product inventories, patient records, scheduling, or payments. This keeps the project focused on the API Gateway, shared infrastructure, routing and tenant isolation.

### Example use case

Hospital A and Clinic B are separate **Applications** under the **Healthcare Project**. Each has its own API key and plan, but both call `GET /gateway/doctors` and reach the **same running Doctor Service** container. The gateway identifies the correct business, enforces its limits and forwards the request. The dummy service simply echoes which backend received it.

## Data model and tenant boundary

This revision moves public base paths and backend targets onto `BackendService` and consolidates method-specific Route records using migrations. Existing incompatible routing configurations must be resolved before applying the migration.

| Entity | Meaning |
|---|---|
| User | ServLink administrator (`is_staff=True`); owner of platform configuration |
| Project | Industry bundle of reusable backend services |
| Application | Subscribing business belonging to exactly one project |
| APIKey | Hashed credential identifying one application; multiple keys share its plan limit |
| BackendService | Project-owned microservice with one unique public `base_path` and approved internal `target_url` |
| Route | One project-specific activation/configuration record per BackendService, with allowed HTTP methods and JWT policy |
| RoutePermission | Optional application-specific permission with its own `allowed_methods` list |
| RequestLog | Existing project/application/key/route request analytics |

```text
User → Project → Application → APIKey
          │           └──────→ RoutePermission → Route
          └──→ BackendService ─────────────────→ Route
```

Projects also directly own their routes. Multiple businesses share the same project's routes and service records. Applications do not create additional backend containers. Existing owner scoping remains: a staff administrator manages their own projects, not another administrator's configuration.

## Architecture and request flow

```text
Hospital A (Application 4, API key A) ─┐
                                    ├→ NGINX :8000 → gateway-1 / gateway-2 / gateway-3
Clinic B (Application 5, API key B) ──┘                        │
                                    shared MySQL ←───────────┤
                                    shared Redis ←───────────┤
                                                             └→ one Doctor Service container
```

1. Middleware validates `X-API-Key`, expiry/revocation and application activity.
2. The key determines the Application and its Project. Caller-supplied tenant IDs do not select a project.
3. The gateway matches the project's active BackendService base path by complete path segment, checks the Route's allowed methods, the Application's method-specific grants and optional JWT policy.
4. Redis atomically applies the business's token bucket, keyed by `gateway_rate:application:<id>`.
5. The gateway validates the configured backend origin, then checks cache only when eligible.
6. It removes `X-API-Key`, spoofed `X-GateScale-*` headers, and hop-by-hop headers, then inserts `X-GateScale-Application-ID` from the validated application.
7. The shared dummy service returns its name, application ID, method and path. The gateway records the request using the existing MySQL analytics model.

NGINX retains round-robin balancing across three gateways. All gateways use the same MySQL and Redis. The backends have no databases. Docker backend ports are exposed only on the Compose network, not published to the host. Direct backend callers are not authenticated by the dummy itself: the identity header is trusted only behind the gateway on this internal network.

## Industry bundles

`seed_demo` creates these records with API-key-only routes:

| Project slug | BackendService | Shared Docker destination | Example path |
|---|---|---|---|
| ecommerce | User Service | fashion-user-service:8000 | /users |
| ecommerce | Product Service | fashion-product-service:8000 | /products |
| ecommerce | Order Service | fashion-order-service:8000 | /orders |
| healthcare | Doctor Service | healthcare-doctor-service:8000 | /doctors |
| healthcare | Appointment Service | healthcare-appointment-service:8000 | /appointments |
| healthcare | Patient Service | healthcare-patient-service:8000 | /patients |
| banking | Account Service | banking-account-service:8000 | /accounts |
| banking | Transaction Service | banking-transaction-service:8000 | /transactions |
| banking | Payment Service | banking-payment-service:8000 | /payments |

The existing `fashion-*` names are retained for compatibility. The legacy `banking-user-service` container remains available for old routes, but is not part of the Banking demo bundle. Four containers are added, independent of how many businesses subscribe.

Each bundle has three services with **one Route per service**: **9 services and 9 routes** total across the demo. Each Route allows `GET`, `POST`, `PUT`, `PATCH`, and `DELETE` by default. The dummy server handles `HEAD` and `OPTIONS` internally; those methods are not exposed by the gateway proxy.

## Local development

Python 3.12+:

```bash
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py seed_demo --admin-email admin@example.com
python manage.py runserver 0.0.0.0:8000
```

Use the email entered during `createsuperuser`. The command requires an existing active staff user, never creates credentials, and refuses to take over another administrator's projects. Re-running it creates only missing demo records; it preserves existing routes, disabled services, authentication policies and applications. It does not create businesses or API keys.

Local Python execution defaults to SQLite. Seeded URLs use Docker DNS names, so use Compose for the full demo. To test a local dummy instead, run it on a separate port, approve that origin, and create a custom route through the existing API. Export environment variables in your shell; Django does not automatically read `.env`.

## Docker demo

```bash
cp .env.example .env
# PowerShell equivalent: Copy-Item .env.example .env
# Start Docker Desktop / Docker Engine first.
docker compose up --build -d
docker compose exec api-gateway-1 python manage.py createsuperuser
docker compose exec api-gateway-1 python manage.py seed_demo --admin-email admin@example.com
```

The migration container waits for MySQL to accept TCP connections. The three gateways wait for migrations. PyMySQL is registered as the MySQL driver, with its RSA authentication dependency for MySQL 8. The public gateway is `http://localhost:8000`.

## Administrator APIs and business creation

Login/refresh remain available; management endpoints require an authenticated, active staff user. Public self-registration is closed: `/admin-api/auth/register` is staff-only and creates nonstaff accounts for legacy JWT testing, never staff accounts. Bootstrap administrators with `createsuperuser`; business subscribers are Applications, not User records.

```http
POST /admin-api/auth/login
Content-Type: application/json

{"email":"admin@example.com","password":"<your password>"}
```

Use the returned access token as `Authorization: Bearer <admin_access_token>` for:

```text
GET/POST /admin-api/projects
GET/POST /admin-api/services
GET/POST /admin-api/routes
GET/POST /admin-api/applications
POST     /admin-api/keys
POST     /admin-api/keys/<id>/revoke
GET      /admin-api/dashboard
GET      /admin-api/analytics/summary
```

Read `GET /admin-api/projects` for actual IDs, then create businesses via `POST /admin-api/applications`:

```json
{"project": 2, "name": "Hospital A", "plan": "premium"}
```

```json
{"project": 2, "name": "Clinic B", "plan": "normal"}
```

Both use the Healthcare project ID (replace `2` with its actual ID). Each response includes an independent `api_key.key`; retain it securely because only the hash is stored. Suggested additional applications are Fashion Store A (`normal`), Fashion Store B (`premium`), Bank A (`premium`), and Finance App B (`normal`), under the corresponding projects. Nonstaff accounts cannot create applications or assign plans.

## Shared-service demonstration

```bash
curl http://localhost:8000/gateway/doctors -H "X-API-Key: <hospital_key>"
curl http://localhost:8000/gateway/doctors -H "X-API-Key: <clinic_key>"
curl -X POST http://localhost:8000/gateway/doctors -H "X-API-Key: <hospital_key>" --data 'any body'
```

The first two requests reach the same container and return different application IDs:

```json
{"response":"success","backend_service":"healthcare-doctor-service","application_id":4,"method":"GET","path":"/doctors"}
```

```json
{"response":"success","backend_service":"healthcare-doctor-service","application_id":5,"method":"GET","path":"/doctors"}
```

Adding `X-GateScale-Application-ID: 9999` cannot impersonate another business; the gateway replaces it. Missing, invalid, expired or revoked API keys return 401. A Healthcare key cannot resolve Banking-only `/accounts` routes. Optional `RoutePermission` records further restrict an application; once records exist, only active matching permissions grant access. With no records, the business may access its project's routes.

The `api_key_and_jwt` Route policy applies to all enabled methods of that service's base path and validates a bearer token in addition to the key. This JWT system uses the platform's User table and is **not a complete customer-identity solution for unrelated businesses**. The primary multi-business demo uses API-key-only routes.

## Dynamic routing and security

Register a microservice's public base path and approved internal target first:

```http
POST /admin-api/services
Content-Type: application/json

{"project":2,"name":"Doctor Service","base_path":"/doctors","target_url":"http://healthcare-doctor-service:8000/doctors"}
```

Then activate one Route record for the returned BackendService ID:

```http
POST /admin-api/routes
Content-Type: application/json

{"project":2,"name":"Doctor API","backend_service":10,"allowed_methods":["GET","POST","PUT","PATCH","DELETE"],"auth_policy":"api_key_only"}
```

Replace `10` with the actual returned service ID. The Route no longer accepts `method`, `path`, or `target_url`. Those are now, respectively, the Route's `allowed_methods` list and the BackendService's `base_path` and `target_url`.

For example, `GET /gateway/doctors/456` matches `/doctors` and forwards to `http://healthcare-doctor-service:8000/doctors/456`. Requests preserve their HTTP method, body and query string. Both `UNIQUE(project_id, base_path)` on BackendService and `UNIQUE(project_id, backend_service_id)` on Route are enforced at the database level. Runtime resolution rejects inactive or foreign-project backend associations.

To allow an Application to GET but not DELETE, add a `RoutePermission` linked to its Route with `allowed_methods=["GET"]`. The legacy default remains unrestricted only when an Application has no RoutePermission records. Once permissions are created, access is limited to active permissions and their allowed methods. To disable a method for every Application, remove it from the Route's `allowed_methods`.

**Migration note:** The migration consolidates existing GET/POST/PUT/PATCH/DELETE records by project/base path, merges application grants without expanding allowed methods, and preserves the existing route JWT policy. It intentionally stops with a clear error if a service uses multiple public base paths, or if records for the same path disagree about destination, backend service or JWT policy. Back up the database and resolve those differences before deployment; the data consolidation is not reversible.

Route configuration is read from MySQL on each request so updates are not hidden by per-worker caches.

Only exact origins in `GATEWAY_ALLOWED_BACKEND_ORIGINS` may be proxied. Defaults are the Docker dummy services, including the retained legacy service. Custom origin configuration replaces that list; an empty list denies all destinations. Both route creation and forwarding validate it. Metadata addresses, unknown hosts, changed ports, credentials in URLs and non-HTTP schemes are rejected. Redirects are not followed; HTTP environment proxy settings are ignored. Only approve operator-controlled hosts and DNS; network egress restrictions remain a deployment responsibility.

## Rate limiting and caching

The unchanged Redis Lua token bucket provides Normal = 100 and Premium = 1000 requests/minute. These are bucket capacities with continuous refill. All API keys belonging to an application share its bucket across gateway instances; different applications have separate buckets. Exhaustion returns 429.

Redis failures retain the existing in-memory fallback. It is **per process**, so limits and cache invalidation are not globally consistent during an outage. Redis is required for the shared-state demonstration; this fallback is not a production failover strategy.

Eligible successful JSON GET responses retain a 60-second TTL. Keys contain Project ID, Application ID, authenticated-user identity and the final URL/query string. Mutating requests invalidate the project's cached responses across applications; this is intentionally broad and never touches another project's namespace.

Caching is conservative:

- Only hosts explicitly listed in `GATEWAY_CACHEABLE_BACKEND_HOSTS` are eligible (default: the three `fashion-*` dummy hosts).
- Healthcare and Banking backends are not cached by default.
- JWT-protected routes, requests carrying Authorization/Cookie, and responses with Set-Cookie, Vary, private/no-store/no-cache directives are not cached.
- Only HTTP 200 JSON responses are cached, preventing cached status changes.

Both settings can be supplied via the shell or Compose `.env`, and require gateway restart. Do not opt sensitive backends into caching merely to increase hit rate.

## Analytics

The existing RequestLog table retains project, application, API key, route/service, method, response status, latency and timestamp. Cache hits and gateway rejections after API-key identification are logged too. Staff can inspect all owned-project traffic or one business:

```http
GET /admin-api/analytics/summary?application_id=4
Authorization: Bearer <admin_access_token>
```

An unknown or another administrator's application returns 404; malformed filters return 400. Logs remain synchronous. Requests rejected before API-key identification cannot be attributed to a business; the summary is not a complete access/security audit log.

## Tests and validation

```bash
python manage.py test authentication users routes gateway analytics
python manage.py check
python manage.py makemigrations --check --dry-run
docker compose config --quiet
```

Tests exercise admin-only APIs, seed idempotency, all nine dummy destinations, two businesses sharing one real local HTTP server, spoofed identity headers, API-key stripping, cross-project denial, route permissions, application-scoped limits, isolated caches, invalidation, sensitive-cache bypass, analytics filters, and existing authentication/routing behavior.

An optional integration test runs the actual Lua script through two independent clients against shared Redis. It uses a unique test key and deletes only that key:

```bash
# Linux/macOS:
GATESCALE_TEST_REDIS_URL=redis://127.0.0.1:6379/15 python manage.py test gateway.test_baas.SharedRedisTests
# PowerShell:
# $env:GATESCALE_TEST_REDIS_URL='redis://127.0.0.1:6379/15'
# python manage.py test gateway.test_baas.SharedRedisTests
```

Without this variable the shared-Redis test is explicitly skipped. Unit tests simulate Redis failures to exercise the existing fallback; those tests alone do not prove multi-process Redis behavior.

## Limitations and interview introduction

No new tables, migrations, frontend, billing, subscription marketplace, Kubernetes, Kafka or Celery are introduced. Existing internal `X-GateScale-*` header names and `GATESCALE_*` environment-variable names are retained for compatibility; they are technical identifiers, not the new product name. Shared services demonstrate routing and tenant identification, not real BaaS functionality. The Docker setup still uses development credentials and Django runserver; production work would require secrets management, hardened deployment, private networking, trusted backend authentication, appropriate customer identity, durable observability and an explicit Redis outage policy. Cache invalidation is broad and does not provide transactional consistency with future business databases.

> I built **ServLink**, a multi-tenant Backend-as-a-Service platform centered on a Django API Gateway. I grouped shared demo backend services into E-commerce, Healthcare and Banking projects, with multiple businesses represented as applications under each project. The gateway validates API keys, enforces route permissions and application-level Redis rate limits, handles eligible caching, and records analytics in MySQL. NGINX balances traffic across three gateway instances. The backend services are deliberately lightweight request-echo implementations, so the engineering focus is on multi-tenant routing, shared infrastructure and scaling.
