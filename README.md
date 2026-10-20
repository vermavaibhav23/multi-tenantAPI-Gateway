# ServLink — Multi-Tenant Backend-as-a-Service API Gateway

ServLink is a Django REST Framework gateway that lets business applications share domain-specific demo microservices (Healthcare, Banking, E-commerce). It uses API keys to identify applications, Redis token-bucket rate limiting, optional JWT verification on configured HTTP methods, safe GET caching, request analytics, and NGINX across three gateway replicas.

**Scope:** The Docker backend services are intentionally dummy request-echo servers. They do not implement real patients, bookings, payments or a client-user identity provider.

## Data model (routing schema)

| Table | Key fields | Uniqueness |
|---|---|---|
| Project | id, owner (ServLink staff User), name, slug | `name`; `slug` |
| Application | id, project, name, slug, plan, is_active | `(project, slug)` |
| APIKey | id, application (1:1), key_hash, expires_at, revoked_at | `key_hash`; `application` |
| BackendService | id, project, name, slug, is_active | `(project, name)`; `(project, slug)` |
| Route | id, project, backend_service, base_path, target_url, is_active | `(project, base_path)` |
| RouteMethod | id, route, method, auth_policy | `(route, method)` |
| RoutePermission | id, application, route, is_active | `(application, route)` |
| RequestLog | project, application, route, method, status, latency, etc. | analytics/history |

`BackendService → Route` is 1:N: one DoctorService can expose `/doctors` and `/specialties`, each with its own Route and backend URL. `Route → RouteMethod` is 1:N: one `/doctors` Route can expose GET (API key only) and DELETE (API key plus JWT) with independent policies. All routes in the same project must have distinct public base paths.

Existing `slug`, timestamp, description and key revocation fields are retained for compatibility even where omitted from the compact ER diagram. `Application → APIKey` is one-to-one: issuing a replacement key rotates the stored hash, invalidating the previous value.

**Important distinction:** RoutePermission grants application-level access to a whole route, not a user-level role. No matching RoutePermission rows means the application can access its project's routes by default; once any permissions exist for that application, only active grants allow access. RouteMethod controls HTTP method availability and basic JWT verification. Tenant-scoped client-user JWTs and RBAC are future enhancements.

## Gateway request flow

1. The `X-API-Key` is hashed and looked up in APIKey, identifying the Application and its Project. No caller-provided project/tenant ID is trusted for routing.
2. Match the most specific, full-segment `Route.base_path` in that project. `/doctors` matches `/doctors/456` but not `/doctors-extra`.
3. Look up `RouteMethod(route, request.method)`. If absent, return 405 without trying another prefix. If its `auth_policy` is `api_key_and_jwt`, verify a bearer token.
4. Apply RoutePermission (if present), per-Application Redis token-bucket rate limit, safe GET cache rules and backend destination-origin allowlist.
5. Forward the request's original HTTP method, body and query string to `Route.target_url + unmatched path suffix`. Remove caller-supplied identity headers and attach verified `X-GateScale-Application-ID`.
6. Return the downstream status/body and record RequestLog analytics.

Example: `GET /gateway/doctors/456` with Hospital A's API key and a Route mapping `/doctors` to `http://healthcare-doctor-service:8000/doctors` is sent to `http://healthcare-doctor-service:8000/doctors/456`.

## Administrator REST endpoints

Admin APIs use a valid ServLink staff JWT, obtained from `POST /admin-api/auth/login`. Administrator accounts are not end users of client applications.

| Endpoint | Purpose |
|---|---|
| `POST /admin-api/auth/login` | Staff login; returns access + refresh JWTs |
| `POST /admin-api/auth/refresh` | Refresh staff access JWT |
| `POST /admin-api/auth/register` | Staff-only creation of a nonstaff Django user |
| `GET/POST /admin-api/projects` | List or create projects |
| `GET/POST /admin-api/services` | List or register BackendServices |
| `GET/POST /admin-api/routes` | List or create project base-path Route mappings |
| `GET/POST /admin-api/route-methods` | List or create HTTP method + JWT policy per Route |
| `GET/POST /admin-api/applications` | List or register business applications (creates first API key) |
| `POST /admin-api/keys` | Rotate application's only API key, returning the new secret once |
| `POST /admin-api/keys/<id>/revoke` | Revoke application API key |
| `GET /admin-api/dashboard` | Owner-scoped project summary |
| `GET /admin-api/analytics/summary` | Owner-scoped request statistics (optional `application_id`) |

Example creation (use IDs returned by your server):

```http
POST /admin-api/routes
Authorization: Bearer <staff_access_jwt>
Content-Type: application/json

{"project":10,"name":"Doctor API","backend_service":201,"base_path":"/doctors","target_url":"http://healthcare-doctor-service:8000/doctors"}
```

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

A new Route with no RouteMethod records is not callable. The demo seeder populates the five supported methods automatically. Admin APIs expose create/list, not general update/delete for all entities; Django admin or a future management API is needed to edit existing policies.

## Demo setup

```bash
python -m venv .venv
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py seed_demo --admin-email admin@example.com
python manage.py test authentication users routes gateway analytics
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

**Back up MySQL/SQLite before applying these migrations.** The new migration collapses old GET/POST/PUT/PATCH/DELETE Route rows with the same project and base path into a single Route and creates RouteMethod records for enabled methods, keeping each former method's JWT policy. It redirects RequestLog references to the surviving Route.

To avoid silent access widening, it intentionally fails if previously method-specific RoutePermission grants would become broader, or if old route records for one base path point to different destinations/backend services. Review and normalize those cases manually first. Project names and service names must also become unique as described above.

The APIKey migration intentionally fails if more than one key exists for an application; remove excess keys deliberately first. After migration, `POST /admin-api/keys` rotates the application's single key **in place**, invalidating the old secret immediately. Coordinate key rotation with the calling backend.

These data transformations are not designed to be automatically reversed. Do not deploy an older gateway binary against the new schema.

## Authentication and future enhancements

Staff JWTs are issued using Django SimpleJWT for ServLink's platform `User` accounts. The current `api_key_and_jwt` policy verifies those platform-issued tokens; it **does not authenticate or authorize Hospital A's doctors or patients**. For real end-user operations, a future shared AuthService would issue application-bound customer JWTs and the gateway would compare the client JWT's tenant/application context and required scopes. Resource ownership checks would remain inside business microservices. Do not expose real sensitive operations relying on the current platform JWT check alone.

Current token-bucket limits: normal 100 and premium 1,000 requests per 60 seconds (continuous refill). Redis failures use a per-process fallback, which is not globally consistent. GET response caching is permitted only for explicitly approved non-sensitive backend hosts and cacheable responses. Analytics remain MySQL-backed.

## Validation

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test authentication users routes gateway analytics
docker compose config --quiet
```

The Docker demo is an infrastructure/routing demonstration rather than a finished production BaaS deployment.
