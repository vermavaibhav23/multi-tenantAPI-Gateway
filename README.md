# ServLink — Multi-Tenant Backend-as-a-Service API Gateway

ServLink is a Django REST Framework gateway that lets business applications share domain-specific demo microservices (Healthcare, Banking, E-commerce). It uses API keys to identify applications, Redis Cluster-backed fixed-window rate limiting, optional JWT verification on configured HTTP methods, safe GET caching, and NGINX across three gateway replicas.

**Scope:** The domain backend containers remain dummy request-echo services. ServLink now includes a shared, application-isolated **client-user authentication component** in the Django gateway deployment. It authenticates application users and issues scoped-to-application JWTs; it does not yet provide roles/RBAC, actual patients/bookings/payments, or a separately deployed AuthService container.

**Database write reduction:** API-key validation no longer updates `last_used_at` on
every request, and the gateway no longer inserts per-request `RequestLog` rows.
The analytics summary endpoint has been removed; `GET /admin-api/dashboard`
still returns project configuration counts, without traffic counts. Schema
migrations `authentication.0010` and `analytics.0006` drop the obsolete
column and table, respectively. **Back up existing databases first:** deleting
the request-log table permanently removes all historical request records.
API key expiry, revocation, application identity and route-permission checks
still use MySQL; only per-route destination metadata is cached in Redis.
In the Docker cluster setup, safe reads go through MySQL Router's read-only
port with Group Replication BEFORE_AND_AFTER consistency.

## Data model (routing schema)

| Table | Key fields | Uniqueness |
|---|---|---|
| Project | id, owner (ServLink staff User), name, slug | `name`; `slug` |
| Application | id, project, name, slug, plan, is_active | `(project, slug)` |
| APIKey | id, application (1:1), key_hash, expires_at, revoked_at | `key_hash`; `application` |
| BackendService | id, project, name, slug, base_url, is_active | `(project, name)`; `(project, slug)` |
| Route | id, project, backend_service, base_path, is_active | `(project, base_path)` |
| RouteMethod | id, route, method, auth_policy | `(route, method)` |
| RoutePermission | id, application, route, is_active | `(application, route)` |
| ApplicationUser | id, application, email, password_hash, is_active, token_version | `(application, email)` |

`BackendService → Route` is 1:N: one DoctorService can expose `/doctors` and `/specialties`, each with its own Route while sharing the same BackendService base URL. `Route → RouteMethod` is 1:N: one `/doctors` Route can expose GET (API key only) and DELETE (API key plus JWT) with independent policies. All routes in the same project must have distinct public base paths.

Existing `slug`, timestamp, description and key revocation fields are retained for compatibility even where omitted from the compact ER diagram. `Application → APIKey` is one-to-one: issuing a replacement key rotates the stored hash, invalidating the previous value.

**Important distinction:** RoutePermission grants application-level access to a whole route, not a user-level role. No matching RoutePermission rows means the application can access its project's routes by default; once any permissions exist for that application, only active grants allow access. RouteMethod controls HTTP method availability and basic JWT verification. Application-bound client-user JWTs are now implemented; role-based authorization (RBAC) and resource ownership checks are future enhancements.

## Gateway request flow

1. The `X-API-Key` is hashed and looked up in APIKey, identifying the Application and its Project. No caller-provided project/tenant ID is trusted for routing.
2. Remove `/gateway`, extract the first path segment (`/doctors/456` -> `/doctors`), then directly get `route:{project_id}:doctors` from Redis. On a miss, fetch that route plus its BackendService origin from primary MySQL and cache for 60 seconds. Only single-segment registered base paths are supported.
3. Query `RouteMethod` in **MySQL via Router's read-only endpoint** using Route ID and HTTP method. If absent, return 405. If its `auth_policy` is `api_key_and_jwt`, verify the application-bound client-user JWT. Method/JWT policies are not cached.
4. Apply RoutePermission (if present), per-Application Redis fixed-window rate limit, safe GET cache rules and backend destination-origin allowlist.
5. Strip `/gateway` from the incoming path and forward the original HTTP method, body and query string to `BackendService.base_url + entire remaining request path`. Strip the raw API key, bearer token and spoofable identity headers; attach verified `X-GateScale-Application-ID` and (when authenticated) `X-GateScale-Client-User-ID`. Downstream services must be protected from direct untrusted network access.
6. Return the downstream status/body without inserting a per-request database log.

Example: `GET /gateway/doctors/456` with Hospital A's API key, a Route whose `base_path=/doctors`, and DoctorService whose `base_url=http://healthcare-doctor-service:8000` is sent to `http://healthcare-doctor-service:8000/doctors/456`.

### Redis route configuration cache

We cache **one route per Redis key**, identified by the verified Project ID and
first path segment. For example, `GET /gateway/doctors/456` resolves the key
`route:{1}:doctors`. Redis Cluster hashes the `{1}` tag to its owning slot.
The cached JSON contains **Route ID, public base path, backend service ID and
backend base URL only**. We do **not** cache HTTP method/JWT policies or route
permissions.

- **Hit:** Use the cached Route ID and backend URL without querying the Route
  and BackendService tables.
- **Miss:** Query the exact active base path using `select_related('backend_service')`
  from MySQL. Store only that route for **60 seconds**. Unknown routes are not cached.
- **Invalidation:** After committed `Route.save()/delete()` or BackendService
  changes, clear the affected route's key. Each route also has a generation key,
  e.g. `routeversion:{1}:doctors`. A Lua compare-and-set prevents a stale
  concurrent cache fill from overwriting a newer invalidation.
- **Policy freshness:** `RouteMethod` is queried with `route_id + method`
  on every request. API-key validation, JWT verification, and application
  permissions also continue using MySQL.
- **Fallback:** If Redis route caching fails, the route is read from MySQL.
  Rate limiting separately fails closed with HTTP **503** if Redis cannot
  enforce the rate limit.

Registered base paths must contain **one segment** (e.g. `/doctors`, not
`/doctors/appointments`). The suffix after that segment can have any depth.
Migration `routes.0010` rejects pre-existing nested routes; rename them
before deploying if any are present.

`QuerySet.update()`, bulk SQL, and raw updates bypass model signals. After
those operations commit, explicitly call
`gateway.route_cache.invalidate_route(project_id, base_path)` (or
`invalidate_project_routes(project_id)` after a bulk service change). If a
Redis invalidation fails, stale routing data may remain up to the **60-second
TTL**, so this is not strict immediate route revocation.

### Redis backend response cache

For approved, non-personalized, API-key-only **GET** requests, cached responses
are keyed as `gateway_cache:v3:<application_id>:<complete_target_url>`, with
the raw query string included. Project ID, HTTP method and customer ID are not
duplicated in the key: Application IDs are unique, only GET is cached, and
authenticated/personalized requests are bypassed. Do not enable this cache
for responses that vary by user, language or other unmodeled headers.

The response TTL remains 60 seconds. POST/PUT/PATCH/DELETE requests invalidate
cached GET responses for every application in the same project, preserving
cross-application invalidation for shared backend services.

### MySQL InnoDB Cluster (single-primary + two read replicas)

The default `docker-compose.yml` now deploys three **MySQL 8.0 nodes**,
a **MySQL Shell bootstrap job**, and **MySQL Router**. Each MySQL node has
its own Docker volume and its own Docker IP. They all listen on internal port
`3306`, with `33061` reserved for Group Replication communication (not
published to the host). MySQL Shell's AdminAPI creates/joins the single-primary
cluster. The nodes coordinate writes through Group Replication.

- `mysql-1:3306`: starts as primary, accepts reads and writes.
- `mysql-2:3306` and `mysql-3:3306`: secondaries, serve reads.
- `mysql-router:6446`: read/write endpoint; new connections go to the
  **current primary**.
- `mysql-router:6447`: read-only endpoint; load-balances **connections**
  across healthy secondary nodes, not SQL statements individually.
- Django's `PrimaryReplicaRouter` sends SELECTs outside atomic write
  transactions to alias `replica` (6447), while UPDATE/INSERT/DELETE,
  locking reads, and reads within `transaction.atomic()` use `default`
  (6446). Routing is based on DB operations, **not** HTTP GET/POST methods.
  When SQLite is used in unit tests, everything stays on `default`.
- Every MySQL member uses `group_replication_consistency=BEFORE_AND_AFTER`:
  required preceding transactions are applied before reads, and writes wait
  for required application on other ONLINE members before acknowledgment.
  This reduces replica-staleness after acknowledged admin updates, but
  increases latency and can block during member failures.
- Group Replication elects a new primary after losing the current one,
  provided a majority of nodes remains. MySQL Router discovers the change
  and reroutes **new** read/write connections; broken in-flight DB
  connections still need retrying.

**Fresh-data deployment and existing-data safety:** The new cluster uses
`mysql_1_data`, `mysql_2_data`, and `mysql_3_data`. The former
`mysql_data` volume is deliberately **not mounted or deleted**. The new
cluster therefore initially contains a **fresh gateway database**. To retain
prior records, back up and restore them onto the **new cluster primary**
during a maintenance window; validate schema migrations and records before
redirecting traffic. Never run `docker compose down -v` against any setup
whose Docker volumes contain data you need.

**Start and inspect the cluster:**

```bash
cp .env.example .env   # Set MYSQL_ROOT_PASSWORD, MYSQL_PASSWORD, JWT secrets
docker compose up --build -d
docker compose ps
docker compose logs mysql-cluster-init mysql-router
docker compose exec mysql-1 mysql -uroot -p -e "SELECT * FROM performance_schema.replication_group_members;"
docker compose exec mysql-1 mysql -uroot -p -e "SELECT @@group_replication_consistency;"
```

A separate GitHub Actions workflow `MySQL InnoDB Cluster smoke test`
checks 3 ONLINE members, consistent reads through Router port 6447,
write routing through 6446 and primary failure/election.

**Local demo, not production HA:** All containers on one Docker host still
fail together if that host fails. MySQL Router is also a single point of
failure unless replicated. Secure secrets/TLS, resource limits, backups,
host placement, retry policies and monitoring are necessary before
production use. The example credentials and Docker defaults are for
development only.

### Redis Cluster (3 primaries + 3 replicas)

`docker-compose.yml` now provisions six Redis 7 nodes (`redis-node-1` through
`redis-node-6`) on an internal Docker network. Each node listens on container
port 6379 and has its **own named volume** for AOF and cluster metadata. The
`redis-cluster-init` one-shot service waits for node readiness, initializes
the cluster once with `redis-cli --cluster create ... --cluster-replicas 1`,
then requires three primaries, three replicas and all 16,384 hash slots before
the API gateways can start. The initialization step preserves existing cluster
state on subsequent starts.

```bash
docker compose up --build -d
docker compose ps
docker compose logs redis-cluster-init
docker compose exec redis-node-1 redis-cli cluster info
docker compose exec redis-node-1 redis-cli cluster nodes
```

All gateway replicas use `REDIS_CLUSTER_ENABLED=True` and
`REDIS_CLUSTER_URL=redis://redis-node-1:6379` (a startup seed, **not** a
pinned destination). A cached, cluster-aware Python client discovers the
topology and routes rate-limiter counters and response-cache keys to their
owning primaries. Reads and writes of rate-limit counters stay on the primary
to keep the increments atomic; replicas are used for failover, not to loosen
rate-limit consistency. Cache invalidation deliberately scans **each primary**
so a project write removes cached responses from every shard. Scanning is
a simple demo implementation and can become expensive with large key counts;
versioned cache keys are a possible improvement.

Redis Cluster uses hash slots, **not traditional consistent hashing**. The
three replicas improve tolerance of individual Redis node failures, but all
six containers still share the same Docker host; this is **not host-level
high availability**. Redis failover can briefly interrupt requests. During a
Redis outage, gateway and client-user login/registration rate-limiter errors
cause HTTP **503 Service Unavailable** (fail closed), instead of using
uncoordinated per-gateway counters. Cached GET responses are not served when
the request cannot pass the shared rate limiter. Do not expose the Redis cluster
network publicly; this demo does not configure Redis authentication or TLS.

**Running just the Redis Cluster integration tests from Compose:**

```bash
docker compose run --rm --no-deps \
  -e USE_SQLITE=True \
  -e GATESCALE_TEST_REDIS_CLUSTER_URL=redis://redis-node-1:6379 \
  api-gateway-1 python manage.py test gateway.test_redis_cluster
```

Outside Docker Compose, `REDIS_CLUSTER_ENABLED=False` retains the
standalone Redis connection for local development and tests. Moving from the
old standalone Redis container **does not automatically migrate cached
responses or current rate counters**; those are disposable, but plan a
maintenance window if preserving them matters.

### Fixed-window rate limiting

Each business application has its own Redis counter for each aligned time window. The normal plan allows **100 requests per 60 seconds**, while the premium plan allows **1,000 requests per 60 seconds**. All three API Gateway replicas use the same Redis key (`gateway_rate:application:<application_id>:<window_number>`) so the count is shared. A Lua script atomically increments the counter and gives new counters an expiry at the end of the window. Requests beyond the limit receive HTTP **429**, and the counter resets in the next window.

**Limitations:** Fixed windows can permit bursts near a window boundary. If a Redis shard cannot enforce an application's limit, the gateway responds with **503** and does not forward the request. When the primary is reachable and the limit is exceeded, it responds with **429**. All three gateway replicas therefore fail closed rather than falling back to inconsistent in-process counters. Replication is still asynchronous, so a failover may lose recently acknowledged increments.

## Administrator REST endpoints

Admin APIs use a valid ServLink staff JWT, obtained from `POST /admin-api/auth/login`. Administrator accounts are not end users of client applications.

| Endpoint | Purpose |
|---|---|
| `POST /admin-api/auth/login` | Staff login; returns access + refresh JWTs |
| `POST /admin-api/auth/refresh` | Refresh staff access JWT |
| `POST /admin-api/auth/register` | Staff-only creation of a nonstaff Django user |
| `GET/POST /admin-api/projects` | List or create projects |
| `GET/POST /admin-api/services` | List or register BackendServices |
| `GET/PATCH/PUT /admin-api/services/<id>` | Read/update a service URL or active state |
| `GET/POST /admin-api/routes` | List or create project base-path Route mappings |
| `GET/PATCH/PUT /admin-api/routes/<id>` | Read/update a route, invalidate old/new route keys |
| `GET/POST /admin-api/route-methods` | List or create HTTP method + JWT policy per Route |
| `GET/PATCH/PUT /admin-api/route-methods/<id>` | Update HTTP/JWT policy (always read from MySQL) |
| `GET/POST /admin-api/route-permissions` | List or create an application-to-route permission |
| `GET/PATCH/PUT /admin-api/route-permissions/<id>` | Read/update a route permission |
| `GET/POST /admin-api/applications` | List or register business applications (creates first API key) |
| `GET/POST /admin-api/application-users` | Staff lists or creates end-user accounts for owned applications |
| `POST /admin-api/application-users/<id>/disable` | Staff disables an application user and immediately invalidates their tokens |
| `POST /gateway/auth/register` | Register an application user via the application backend's API key |\n| `POST /admin-api/keys` | Rotate application's only API key, returning the new secret once |
| `POST /admin-api/keys/<id>/revoke` | Revoke application API key |
| `GET /admin-api/dashboard` | Owner-scoped project configuration counts (no traffic analytics) |

Register the internal microservice origin first, then one or more public routes (use actual IDs returned by your server):

```http
POST /admin-api/services
Authorization: Bearer <staff_access_jwt>
Content-Type: application/json

{"project":10,"name":"Doctor Service","slug":"healthcare-doctor-service","base_url":"http://healthcare-doctor-service:8000"}
```

```http
POST /admin-api/routes
Authorization: Bearer <staff_access_jwt>
Content-Type: application/json

{"project":10,"name":"Doctor API","backend_service":201,"base_path":"/doctors"}
```

```http
POST /admin-api/routes
Authorization: Bearer <staff_access_jwt>
Content-Type: application/json

{"project":10,"name":"Specialties API","backend_service":201,"base_path":"/specialties"}
```

Both routes are forwarded to the same DoctorService host, with their **complete incoming path** appended (not only the resource-ID suffix). Because the public and internal paths are intentionally identical, this design does **not** support path rewrites.

Then separately configure the route's methods:

```http
POST /admin-api/route-methods
Authorization: Bearer <staff_access_jwt>
Content-Type: application/json

{"route":301,"method":"GET","auth_policy":"api_key_only"}
```

```http
POST /admin-api/route-methods
Authorization: Bearer <staff_access_jwt>
Content-Type: application/json

{"route":301,"method":"DELETE","auth_policy":"api_key_and_jwt"}
```

The `Route` API no longer has a `target_url` field. `BackendService.base_url` is an operator-approved origin with no path, query string or fragment. An empty base URL is allowed for an unconfigured service and returns 503 at forwarding time. A new Route with no RouteMethod records is not callable. The demo seeder populates the five supported methods automatically. Admin APIs support PATCH/PUT on services, routes, method policies and route permissions. All such updates are wrapped in primary-side database transactions; route-cache invalidation only runs following successful commit.

## Demo setup

```bash
python -m venv .venv
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py seed_demo --admin-email admin@example.com
python manage.py test authentication users routes gateway analytics client_auth
```

Or run the full multi-container environment:

```bash
cp .env.example .env
docker compose up --build -d
docker compose exec api-gateway-1 python manage.py createsuperuser
docker compose exec api-gateway-1 python manage.py seed_demo --admin-email admin@example.com
```

`seed_demo` creates three industry projects, nine backend services, **nine Routes** and **45 RouteMethod records** (GET/POST/PUT/PATCH/DELETE for each). It preserves existing route and method configurations when re-run. Healthcare prefixes: `/doctors`, `/appointments`, `/patients`; E-commerce: `/users`, `/products`, `/orders`; Banking: `/accounts`, `/transactions`, `/payments`.

The public gateway is normally `http://localhost:8000`. The downstream demo servers listen on private Compose networks. Only operator-approved backend origins can be used; never expose internal dummy backends directly to untrusted callers.

## Migration from method-specific Routes

**Back up MySQL/SQLite before applying these migrations.** The new migration collapses old GET/POST/PUT/PATCH/DELETE Route rows with the same project and base path into a single Route and creates RouteMethod records for enabled methods, keeping each former method's JWT policy. The historical migration also updated references in the old RequestLog table, which has since been removed by analytics migration 0006.

To avoid silent access widening, it intentionally fails if previously method-specific RoutePermission grants would become broader, or if old route records for one base path point to different destinations/backend services. Review and normalize those cases manually first. Project names and service names must also become unique as described above.

The APIKey migration intentionally fails if more than one key exists for an application; remove excess keys deliberately first. After migration, `POST /admin-api/keys` rotates the application's single key **in place**, invalidating the old secret immediately. Coordinate key rotation with the calling backend.

**Backend URL migration:** A further migration moves old `Route.target_url` configurations into a single `BackendService.base_url` per service. It refuses routes whose backend path differs from `Route.base_path`, routes that point to conflicting hosts within one service, and configured routes without an associated service. Resolve these incompatibilities before upgrading. No automatic rewrite or destination guessing occurs. Existing application JWT issuance, refresh, tenant-bound verification and method-specific authentication are unchanged.

These data transformations are not designed to be automatically reversed. Do not deploy an older gateway binary against the new schema.

## Client-application user JWT authentication (implemented)

ServLink platform `User` is **only** the Django administrator/staff identity. Each subscribing business has `ApplicationUser` records with `application_id`, a normalized email, and a password stored using Django's salted password hashing. `UNIQUE(application_id, email)` ensures isolation: Hospital A and Clinic B can have users with the same email without sharing credentials.

**Implementation boundary:** `client_auth` is one shared Django authentication module deployed with each gateway replica, using shared MySQL. It is not yet a separate Dockerized AuthService container. This demonstrates tenant-bound JWT issuance without spinning up an authentication container per business; a dedicated service is a future extraction if needed.

### 1. Staff provisions the application's users

```http
POST /admin-api/application-users
Authorization: Bearer <servlink_staff_access_jwt>
Content-Type: application/json

{"application":101,"email":"doctor@hospital-a.example","password":"<strong unique password>"}
```

The endpoint is staff-only and restricts creation to projects the logged-in administrator owns. It returns the user's ID, application ID, email and status **without** disclosing the password/hash. `GET /admin-api/application-users` lists owned application users. To deactivate a user:

```http
POST /admin-api/application-users/42/disable
Authorization: Bearer <servlink_staff_access_jwt>
```

Disabling a user causes access and refresh tokens to be rejected immediately.

### 2. New user self-registration (API key, no user JWT)

The client application's trusted backend (not a public browser holding an API key)
can register an end user under the Application resolved from its `X-API-Key`:

```http
POST /gateway/auth/register
X-API-Key: <hospital-a-server-api-key>
Content-Type: application/json

{"email":"patient@hospital-a.example","password":"<strong unique password>"}
```

Response (`201 Created`):

```json
{"id":42,"application":101,"email":"patient@hospital-a.example","is_active":true}
```

The endpoint derives `application_id` exclusively from the verified API key, never from request input. It accepts only email and password; users cannot select roles, scopes, `is_active`, or another application. Passwords are validated and securely hashed; `UNIQUE(application_id, email)` prevents duplicate accounts within one business but allows the same email under different businesses. Registration is throttled per application and email, and database uniqueness protects against races. **It does not issue a JWT at registration:** the new user calls login next.

**Demo security limitation:** Registrations become active immediately without confirming ownership of the email address. Add email verification, invitation/approval options, account recovery, more robust distributed edge rate limiting and anti-enumeration responses before allowing real healthcare or other sensitive client-user accounts. The API key must remain on the client application's trusted server, not be shared publicly.

### 3. Hospital A's backend logs in a user (API key required, no user JWT)

```http
POST /gateway/auth/login
X-API-Key: <hospital-a-server-api-key>
Content-Type: application/json

{"email":"doctor@hospital-a.example","password":"<correct password>"}
```

Response:

```json
{"access":"<client_access_jwt>","refresh":"<client_refresh_jwt>","token_type":"Bearer","expires_in":900}
```

The API key identifies the Application; user lookup is constrained to that application. No application ID provided by the caller is trusted as identity. Failed logins return a generic 401; repeated attempts are rate-limited by application/email using Redis. When Redis is unavailable for those checks, login/registration return 503 instead of bypassing the limit.

### 4. Refresh user access

```http
POST /gateway/auth/refresh
X-API-Key: <hospital-a-server-api-key>
Content-Type: application/json

{"refresh":"<client_refresh_jwt>"}
```

Returns a new access token; the provided refresh token remains valid until expiration or user deactivation. A refresh token cannot be used as an access token.

### 5. Call a JWT-protected backend route

```http
GET /gateway/doctors/456
X-API-Key: <hospital-a-server-api-key>
Authorization: Bearer <client_access_jwt>
```

The gateway validates signature (HS256 with independent `CLIENT_JWT_SIGNING_KEY`), expiry, token type, expected issuer `servlink-client-auth`, audience `servlink-gateway`, the JWT's `application_id` matching the API key, and the user still being active. Token lifetime: **15-minute access**, **7-day refresh**. Server-side secrets must not be embedded in a browser frontend; the application's trusted backend holds the API key and can relay the user's token.

The platform staff JWT uses a **different signing domain** and **cannot** authenticate JWT-protected client routes. The gateway strips incoming bearer/API-key credentials before forwarding and injects verified identity headers for the isolated dummy services.

**Security / limitations:** The shared module is an implementation of basic authentication, not an independently hosted or fully hardened identity provider. RBAC, JWT role/scope enforcement, business resource ownership, self-service enrollment, password recovery and refresh-token rotation/revocation are future enhancements. In particular, a valid client JWT proves who a user is, **not** that they are allowed to DELETE a doctor. Never expose real sensitive operations until authorization is implemented in the microservice or at the gateway. Use a strong independently generated `CLIENT_JWT_SIGNING_KEY` on **all** gateway replicas, enable TLS, and protect the private service network. Django's development signing-key fallback is not suitable for production.


## Validation

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test authentication users routes gateway analytics client_auth
docker compose config --quiet
```

The Docker demo is an infrastructure/routing demonstration rather than a finished production BaaS deployment.
