# ServLink — Multi-Tenant Backend-as-a-Service API Gateway

ServLink is a Django REST Framework gateway that lets business applications share domain-specific demo microservices (Healthcare, Banking, E-commerce). It uses API keys to identify applications, Redis token-bucket rate limiting, optional JWT verification on configured HTTP methods, safe GET caching, request analytics, and NGINX across three gateway replicas.

**Scope:** The domain backend containers remain dummy request-echo services. ServLink now includes a shared, application-isolated **client-user authentication component** in the Django gateway deployment. It authenticates application users and issues scoped-to-application JWTs; it does not yet provide roles/RBAC, actual patients/bookings/payments, or a separately deployed AuthService container.

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
| ApplicationUser | id, application, email, password_hash, is_active, token_version | `(application, email)` |\n| RequestLog | project, application, route, client_user, method, status, latency, etc. | analytics/history |

`BackendService → Route` is 1:N: one DoctorService can expose `/doctors` and `/specialties`, each with its own Route and backend URL. `Route → RouteMethod` is 1:N: one `/doctors` Route can expose GET (API key only) and DELETE (API key plus JWT) with independent policies. All routes in the same project must have distinct public base paths.

Existing `slug`, timestamp, description and key revocation fields are retained for compatibility even where omitted from the compact ER diagram. `Application → APIKey` is one-to-one: issuing a replacement key rotates the stored hash, invalidating the previous value.

**Important distinction:** RoutePermission grants application-level access to a whole route, not a user-level role. No matching RoutePermission rows means the application can access its project's routes by default; once any permissions exist for that application, only active grants allow access. RouteMethod controls HTTP method availability and basic JWT verification. Application-bound client-user JWTs are now implemented; role-based authorization (RBAC) and resource ownership checks are future enhancements.

## Gateway request flow

1. The `X-API-Key` is hashed and looked up in APIKey, identifying the Application and its Project. No caller-provided project/tenant ID is trusted for routing.
2. Match the most specific, full-segment `Route.base_path` in that project. `/doctors` matches `/doctors/456` but not `/doctors-extra`.
3. Look up `RouteMethod(route, request.method)`. If absent, return 405 without trying another prefix. If its `auth_policy` is `api_key_and_jwt`, verify a **client-user** bearer token signed by the shared client authentication component, and require the JWT application identity to match the validated API key. ServLink staff JWTs are not accepted for client routes.
4. Apply RoutePermission (if present), per-Application Redis token-bucket rate limit, safe GET cache rules and backend destination-origin allowlist.
5. Forward the request's original HTTP method, body and query string to `Route.target_url + unmatched path suffix`. Strip the raw API key, bearer token and spoofable identity headers; attach verified `X-GateScale-Application-ID` and (when authenticated) `X-GateScale-Client-User-ID`. Downstream services must be protected from direct untrusted network access.
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
| `GET/POST /admin-api/applications` | List or register business applications (creates first API key) |\n| `GET/POST /admin-api/application-users` | Staff lists or creates end-user accounts for owned applications |\n| `POST /admin-api/application-users/<id>/disable` | Staff disables an application user and immediately invalidates their tokens |
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

### 2. Hospital A's backend logs in a user (API key required, no user JWT)

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

The API key identifies the Application; user lookup is constrained to that application. No application ID provided by the caller is trusted as identity. Failed logins return a generic 401; repeated attempts are rate-limited by application/email using Redis (per-process fallback during outage).

### 3. Refresh user access

```http
POST /gateway/auth/refresh
X-API-Key: <hospital-a-server-api-key>
Content-Type: application/json

{"refresh":"<client_refresh_jwt>"}
```

Returns a new access token; the provided refresh token remains valid until expiration or user deactivation. A refresh token cannot be used as an access token.

### 4. Call a JWT-protected backend route

```http
GET /gateway/doctors/456
X-API-Key: <hospital-a-server-api-key>
Authorization: Bearer <client_access_jwt>
```

The gateway validates signature (HS256 with independent `CLIENT_JWT_SIGNING_KEY`), expiry, token type, expected issuer `servlink-client-auth`, audience `servlink-gateway`, the JWT's `application_id` matching the API key, and the user still being active. Token lifetime: **15-minute access**, **7-day refresh**. Server-side secrets must not be embedded in a browser frontend; the application's trusted backend holds the API key and can relay the user's token.

The platform staff JWT uses a **different signing domain** and **cannot** authenticate JWT-protected client routes. The gateway strips incoming bearer/API-key credentials before forwarding and injects verified identity headers for the isolated dummy services. RequestLog optionally records `client_user_id`.

**Security / limitations:** The shared module is an implementation of basic authentication, not an independently hosted or fully hardened identity provider. RBAC, JWT role/scope enforcement, business resource ownership, self-service enrollment, password recovery and refresh-token rotation/revocation are future enhancements. In particular, a valid client JWT proves who a user is, **not** that they are allowed to DELETE a doctor. Never expose real sensitive operations until authorization is implemented in the microservice or at the gateway. Use a strong independently generated `CLIENT_JWT_SIGNING_KEY` on **all** gateway replicas, enable TLS, and protect the private service network. Django's development signing-key fallback is not suitable for production.


## Validation

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test authentication users routes gateway analytics
docker compose config --quiet
```

The Docker demo is an infrastructure/routing demonstration rather than a finished production BaaS deployment.
