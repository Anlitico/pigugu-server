---
name: ops
description: Infrastructure operations for pigugu-server — PostgreSQL, Redis, EKS, LiveKit
---

# Operations

```
pigugu-server infra (us-west-1)
├── PostgreSQL   pigugu-db.c1esma68egsk.us-west-1.rds.amazonaws.com:5432
├── Redis        pigugu-redis.feoudk.ng.0001.usw1.cache.amazonaws.com:6379
├── EKS          pigugu-cluster
├── ECR API      537557168531.dkr.ecr.us-west-1.amazonaws.com/pigugu-api
├── ECR Agent    537557168531.dkr.ecr.us-west-1.amazonaws.com/pigugu-agent
└── LiveKit      shrump-test-jbnvclwi.livekit.cloud
```

RDS and Redis are in private subnet — only reachable from within VPC (EKS pods).

## Modules

| Command | Module | What it does |
|---|---|---|
| `/ops:pg` | [pg](ops/pg.md) | PostgreSQL queries — run arbitrary SQL against RDS |
| `/ops:redis` | [redis](ops/redis.md) | Redis queries — check keys, get values |
| `/ops:eks` | [eks](ops/eks.md) | EKS operations — pods, logs, restart, deploy |
| `/ops:livekit` | [livekit](ops/livekit.md) | LiveKit — list rooms, participants, dispatch agents |

## File Layout

```
.claude/skills/
├── ops.md          ← this file (navigation hub)
└── ops/
    ├── pg.md           ← PostgreSQL queries
    ├── redis.md        ← Redis queries
    ├── eks.md          ← EKS pod/logs/deploy
    └── livekit.md      ← LiveKit operations
```

## K8s Secrets

All secrets are in `pigugu-secrets`. DB credentials are also in `.env` (gitignored) for convenience — used by the pg module.

```bash
kubectl get secret pigugu-secrets -o jsonpath="{.data.DATABASE_URL}" | base64 -d
kubectl get secret pigugu-secrets -o jsonpath="{.data.DB_PASSWORD}" | base64 -d
kubectl get secret pigugu-secrets -o jsonpath="{.data.REDIS_URL}" | base64 -d
kubectl get secret pigugu-secrets -o jsonpath="{.data.LIVEKIT_API_KEY}" | base64 -d
kubectl get secret pigugu-secrets -o jsonpath="{.data.LIVEKIT_API_SECRET}" | base64 -d
```
