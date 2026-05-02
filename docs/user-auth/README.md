# User Authentication — Technical Architecture

This folder contains the complete technical specification for the Pigugu user authentication system.

## Documents

| File | Contents |
|------|----------|
| [server-api.md](./server-api.md) | Server-side implementation: DB schema, API endpoints, service logic, security |
| [app-integration.md](./app-integration.md) | Flutter app integration: API client, token storage, BLoC, UI screens |

---

## Feature Scope (This Phase)

| Feature | Included |
|---------|----------|
| Register with email + password | ✅ |
| Login (returns JWT pair) | ✅ |
| Auto-refresh access token | ✅ |
| Logout (revoke token) | ✅ |
| Change password | ✅ |
| Email verification | ❌ Later |
| Forgot password / reset via email | ❌ Later |
| Google / Apple login | ❌ Later |

---

## System Overview

```
Flutter App
    │
    │  HTTPS / JSON
    ▼
FastAPI (pigugu-api)
    │              │
    ▼              ▼
PostgreSQL      Redis
(users table)   (refresh token allowlist)
```

### Token Strategy

- **Access Token**: Short-lived JWT (60 min). Stateless, verified by signature only.
- **Refresh Token**: Long-lived JWT (30 days). Stored in Redis as an allowlist.
  - Key: `refresh:{user_id}:{jti}` → Value: `"1"` with TTL 30 days
  - Logout = delete the Redis key → token is immediately invalid
  - Change password = delete all `refresh:{user_id}:*` keys

### display_name Policy

- Optional at registration.
- If not provided, server defaults to the part of the email before `@` (e.g. `jane@example.com` → `jane`).
- Can be updated via profile endpoint (future feature).
