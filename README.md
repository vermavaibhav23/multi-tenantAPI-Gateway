# ServLink — Multi-Tenant API Gateway (Request Preview Mode)

ServLink is a **Django REST Framework gateway demonstration**. It validates API keys, resolves multi-tenant routes, enforces route-method and application permissions, optionally verifies application-user JWTs, and applies a shared Redis rate limit. **Instead of contacting backend microservices, it returns a JSON preview of where the request would be sent.**

**There are no backend service containers or outbound service calls in this mode.** The `BackendService` records are *virtual destination configurations*, not running services. This is a **routing and security simulation**, not a full functioning HTTP reverse proxy.

## Example

With a Doctor Service virtual origin of `http://healthcare-doctor-service:8000` and a registered public route `/doctors`:

```http
POST /gateway/doctors/456?source=app&source=web
X-API-Key: <your-application-key>
Content-Type: application/json

{"doctor_id":456,"operation":"update"}
```

After the authorization and rate-limit checks pass, the gateway returns HTTP **200**:

```json
{
  "status": "validated",
  "mode": "preview",
  "forwarded": false,
  "target_url": "http://healthcare-doctor-service:8000/doctors/456?source=app&source=web",
  "method": "POST",
  "path": "/doctors/456",
  "query_params": {"source": ["app", "web"]},
  "application_id": 12,
  "project_id": 10,
  "route_id": 301,
  "backend_service_id": 201,
  "auth_policy": "api_key_only",
  "client_user_id": null,
  "content_type": "application/json",
  "body": {"doctor_id": 456, "operation": "update"},
  "body_encoding": "json"
}
```

The IDs above are illustrative. The actual response uses IDs from your database.

- `target_url` combines the registered service origin, the full requested path after `/gateway`, and the original query string.
- `method` is the incoming method (GET, POST, PUT, PATCH, DELETE), which must be enabled through `RouteMethod`.
- `query_params` always contains arrays to preserve repeated parameters.
- `body`, `content_type`, and `body_encoding` appear **only when a request has a nonempty body**. JSON is decoded into an object/array; UTF-8 is displayed as text; other bytes are base64-encoded.
- `client_user_id` is set only after successful application-user JWT verification. The gateway **does not return** raw API keys, bearer tokens, cookies, or untrusted caller-supplied headers.
- `forwarded: false` explicitly means **no backend was contacted**. The result is a request preview, not a downstream service response.
- These previews are **never response-cached**, because they may contain a caller's request body and reflect current identity/policy information.

## Docker architecture

```text
                          HTTP :8000
                             |
                          NGINX
                             |
                   +---------+---------+
                   |         |         |
                Gateway 1 Gateway 2 Gateway 3
                   |         |         |
                   +---------+---------+
                             |
          +------------------+------------------+
          |                                     |
  MySQL Router                          Redis Cluster
  6446 writes / 6447 reads              3 primaries + 3 replicas
          |
  MySQL InnoDB Cluster
  1 primary + 2 secondaries

  Gateway returns JSON request preview (NO backend containers)
```

The three gateway replicas share MySQL and Redis. NGINX uses round-robin balancing. MySQL is configured as a single-primary Group Replication InnoDB Cluster with MySQL Router; Redis Cluster provides routing cache and globally coordinated fixed-window rate limiting. These are **development deployments on a single Docker host**, not host-level production HA.

### Compose setup

```bash
cp .env.example .env
# Edit .env to set secure MYSQL_ROOT_PASSWORD, MYSQL_PASSWORD,
# DJANGO_SECRET_KEY, and CLIENT_JWT_SIGNING_KEY.
docker compose up --build -d
docker compose ps
docker compose exec api-gateway-1 python manage.py createsuperuser
docker compose exec api-gateway-1 python manage.py seed_demo --admin-email admin@example.com
```

Replace the seeder email with the active staff user's email. `seed_demo` registers three projects (Healthcare, Banking, E-commerce), nine **virtual** BackendService records, nine base-path Routes, and 45 RouteMethod policies. It does **not** start or require the corresponding service hostnames. Re-running the command preserves existing route and method records. Create applications and API keys through the administrator endpoints before calling `/gateway/...`.

Do not run `docker compose down -v` if the MySQL/Redis volumes contain data you need. Existing pre-cluster MySQL volumes are not mounted by the new cluster; migrate from a verified database backup if upgrading an older deployment.

### Run unit tests without Docker

```bash
python -m venv .venv
pip install -r requirements.txt
python manage.py migrate
python manage.py test authentication users routes gateway analytics client_auth gateway_project.test_db_router
```

The default non-Compose settings use SQLite for local development and tests. Gateway tests use a fake Redis client to validate routing and shared rate-limit logic without real Redis.

## Gateway request flow

1. **API key:** `GatewayAuthenticationMiddleware` hashes `X-API-Key` and identifies its active Application and Project. It rejects missing, expired, revoked, or invalid keys.
2. **Route resolution:** Strip `/gateway`; use the verified Project ID and first remaining path segment to resolve the active `Route`. A 60-second Redis route-cache entry may avoid the route metadata MySQL lookup.
3. **Method policy:** Read `RouteMethod` in MySQL for the route and incoming method; return **405** for unavailable methods.
4. **Access checks:** Apply `RoutePermission`. If policy is `api_key_and_jwt`, authenticate an application-scoped client-user JWT (platform staff JWTs are not interchangeable).
5. **Rate limit:** Atomically increment the Application's fixed-window counter in Redis using a Lua script; return **429** if over limit, or **503** if the shared rate limiter is unavailable (fail closed).
6. **Virtual destination:** Require an active BackendService with a configured `base_url`; validate that its scheme/host/port match `GATEWAY_ALLOWED_BACKEND_ORIGINS`. Only operator-approved origins are accepted, even in preview mode.
7. **Preview:** Compose `base_url + complete requested path + original query string`; return request metadata and body as JSON. **Do not invoke an HTTP client.**

Errors such as invalid keys, missing routes, denied application access, invalid JWTs, disallowed destination origins, and exceeded rate limits still return their respective HTTP error statuses rather than a preview.

### Routing schema

| Model | Main attributes and meaning |
|---|---|
| Project | Administrative owner, unique project name/slug |
| Application | Belongs to Project, plan (normal/premium), active flag |
| APIKey | One active key record per Application; securely stored hash and revocation/expiry |
| BackendService | Project, name, slug, **virtual** `base_url`, active flag |
| Route | Project, BackendService, distinct **single-segment** `base_path`, active flag |
| RouteMethod | Route, HTTP method, `api_key_only` or `api_key_and_jwt` |
| RoutePermission | Optional Application ↔ Route permission record |
| ApplicationUser | Application-scoped email/password and token version for JWT authorization |

`BackendService → Route` is 1:N; one configured virtual origin can serve multiple public prefixes. `Route → RouteMethod` is 1:N. Registered base paths must be a single segment, such as `/doctors`, while requests can have arbitrary suffixes such as `/doctors/456/appointments`. No path rewriting is performed. Without RoutePermission rows for an Application, its project's routes are allowed by default; if grants exist, only active grants permit access.

### Redis route metadata cache

The key `route:{project_id}:doctors` uses a Redis Cluster hash tag (actual key syntax `route:{10}:doctors`). The entry stores route ID/path and virtual destination ID/origin. HTTP-method JWT policies and route permissions are **not cached**; they are fetched from MySQL on requests.

A 60-second TTL bounds outdated metadata when invalidation fails. Model-save and model-delete signals invalidate route snapshots after the database commits. Bulk updates, raw SQL, and `QuerySet.update()` bypass model signals and must call `gateway.route_cache.invalidate_route(...)` or `invalidate_project_routes(...)` after commit. The generation-key Lua guard prevents stale concurrent cache fills from undoing invalidation.

### Redis rate limiting

A normal Application gets **100 requests per 60 seconds**, and a premium Application gets **1,000 requests per 60 seconds**, across **all gateway replicas**. A Lua script atomically increments a time-window counter and expires it at the window boundary. This is fixed-window limiting, so bursts near boundaries are possible. Redis Cluster promotes replicas on primary failure; replication is asynchronous, so counters might briefly roll back across failovers.

### MySQL consistency

Three MySQL 8 nodes use Group Replication in single-primary mode. MySQL Router directs write connections to the active primary at `6446` and balances read-only connections over secondaries at `6447`. `PrimaryReplicaRouter` routes Django reads/writes as appropriate and keeps reads inside atomic transactions on the primary. `group_replication_consistency=BEFORE_AND_AFTER` coordinates the required transaction application before reads/after writes for strong post-acknowledgment consistency, at some latency cost.

## Administrator API

Staff JWT from `POST /admin-api/auth/login` is required for administration; end-user Application JWTs do not grant admin privileges.

| Endpoint | Function |
|---|---|
| `GET/POST /admin-api/projects` | Projects |
| `GET/POST /admin-api/services` | Virtual destinations and origins |
| `GET/PATCH/PUT /admin-api/services/<id>` | Update virtual origin / active state |
| `GET/POST /admin-api/routes` | Public path-to-destination mappings |
| `GET/PATCH/PUT /admin-api/routes/<id>` | Update a route |
| `GET/POST /admin-api/route-methods` | Allowed methods and JWT policy |
| `GET/PATCH/PUT /admin-api/route-methods/<id>` | Change method policy |
| `GET/POST /admin-api/route-permissions` | Application-to-route access |
| `GET/PATCH/PUT /admin-api/route-permissions/<id>` | Change access grants |
| `GET/POST /admin-api/applications` | Tenant Applications; create first API key |
| `POST /admin-api/keys` | Rotate an Application API key |
| `POST /admin-api/keys/<id>/revoke` | Revoke an API key |
| `GET/POST /admin-api/application-users` | Staff provision/list Application users |
| `POST /admin-api/application-users/<id>/disable` | Disable user / revoke tokens |
| `GET /admin-api/dashboard` | Owner-scoped configuration counts |
| `POST /gateway/auth/register` | End-user registration with Application API key |
| `POST /gateway/auth/login` | Issue Application-scoped JWT |
| `POST /gateway/auth/refresh` | Refresh Application-scoped JWT |

Register a virtual origin and then the public route (IDs below are examples):

```json
{"project":10,"name":"Doctor Service","slug":"healthcare-doctor-service","base_url":"http://healthcare-doctor-service:8000"}
```

POST this to `/admin-api/services`; then POST to `/admin-api/routes`:

```json
{"project":10,"name":"Doctors API","backend_service":201,"base_path":"/doctors"}
```

Finally register `RouteMethod` records via `/admin-api/route-methods`, for example:

```json
{"route":301,"method":"POST","auth_policy":"api_key_only"}
```

The service origin must be added to `GATEWAY_ALLOWED_BACKEND_ORIGINS` if it differs from the configured demo origins. This allowlist **does not imply the host is running or reachable**; URL validation only checks the approved origin.

## Limits and honest scope

- ServLink currently demonstrates **authenticating, authorizing, rate limiting, and resolving requests**, then reporting their intended destination.
- It does **not** demonstrate a downstream HTTP round trip, real microservice availability, backend failures, request forwarding, backend cache hits, or end-to-end throughput of a reverse proxy.
- `ProductResponseCache` remains as an internal, separately tested cache utility for future use, but **gateway previews never use it**. Storing previews in the former GET response cache would create unnecessary stale or sensitive responses.
- Request bodies are reflected to their original callers and should not be logged or stored. Avoid submitting real secrets as test bodies.
- The sample MySQL, Redis and NGINX deployments need credentials, TLS, backups, process supervision and multi-host placement before production use.
