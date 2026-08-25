---
name: cicd
description: CI/CD pipeline for pigugu-server — build, database migration, and deploy to EKS
---

# pigugu-server CI/CD Workflow

## Quick Reference

```bash
# All GitHub CLI commands MUST use proxy
export HTTP_PROXY=http://127.0.0.1:7897 HTTPS_PROXY=http://127.0.0.1:7897

# Check CI status
gh run list --repo Anlitico/pigugu-server --branch main --limit 5

# Trigger deploy (manual only)
gh workflow run "Deploy to Amazon EKS" --repo Anlitico/pigugu-server --ref main
```

## Workflow Phases

```
PR Merge → CI (auto) → User Confirmation → CD (manual)
                         ↑
                    ASK before deploying
```

### Phase 1: CI (Build) — Automatic

**Trigger**: Automatically on push/merge to `main`.

**What it does**:
1. Checkout → AWS login → ECR login
2. Build `pigugu-api` Docker image → push to ECR
3. Build `pigugu-agent` Docker image → push to ECR
4. Build `pigugu-tools` Docker image → push to ECR (ops scripts:
   `analyze_latency.py`, `migrate_metrics_format.py`)

**How to check**:
```bash
export HTTP_PROXY=http://127.0.0.1:7897 HTTPS_PROXY=http://127.0.0.1:7897
for i in $(seq 1 30); do
  result=$(gh run list --repo Anlitico/pigugu-server --branch main --limit 1 --json status,conclusion --jq '.[0] | "\(.status) \(.conclusion // \"\")"')
  st=$(echo "$result" | awk '{print $1}')
  echo "[$i] CI status=$st"
  if [ "$st" = "completed" ]; then break; fi
  sleep 20
done
```

**On failure**: Read the error from the failed run's logs, fix the issue, push a new commit.

### Phase 2: User Confirmation

**IMPORTANT**: After CI succeeds, you MUST ask the user for confirmation before triggering CD. Use the `AskUserQuestion` tool:

> CI build succeeded. Deploy to EKS? This will:
> - Run database migrations (if any)
> - Restart API and Agent pods
> - ~2-3 minutes of downtime

**Never deploy without explicit user approval.**

### Phase 3: CD (Deploy) — Manual

**Trigger**: Manual only via `workflow_dispatch`.

**What it does**:
1. **Run database migrations** (K8s Job):
   - Delete old `pigugu-db-migration` job
   - Build migration job from `k8s/migration-job.yaml` with current ECR image
   - `kubectl apply -f` → `kubectl wait --for=condition=complete --timeout=120s`
   - On success: print logs, delete job
   - **On failure**: print logs, delete job, **exit 1 — old pods keep running (safe)**
2. **Deploy to EKS**:
   - `kubectl apply -f` for secrets, api, agent, crawler-cronjob
   - `kubectl set image` for both deployments
   - `kubectl rollout restart` + `kubectl rollout status`

**How to deploy**:
```bash
export HTTP_PROXY=http://127.0.0.1:7897 HTTPS_PROXY=http://127.0.0.1:7897

# 1. Trigger deploy
gh workflow run "Deploy to Amazon EKS" --repo Anlitico/pigugu-server --ref main
sleep 5

# 2. Poll until complete
for i in $(seq 1 40); do
  result=$(gh run list --repo Anlitico/pigugu-server --workflow "Deploy to Amazon EKS" --branch main --limit 1 --json status,conclusion --jq '.[0] | "\(.status) \(.conclusion // \"\")"')
  st=$(echo "$result" | awk '{print $1}')
  conc=$(echo "$result" | awk '{print $2}')
  echo "[$i] CD status=$st conclusion=$conc"
  if [ "$st" = "completed" ]; then
    echo "DEPLOY: $conc"
    break
  fi
  sleep 15
done
```

**On failure**: Read the failed run's logs on GitHub Actions. Common issues:
- Migration failure: fix migration script, push commit, rebuild CI, redeploy
- Image pull failure: ECR permissions or image tag mismatch
- Pod crash: check `kubectl logs` for the new pod

## Database Migrations

Migrations run as part of CD (before pod restart). If a migration fails:
- Old pods **continue running** with the old code (safe rollback)
- The CD run fails — fix the migration and redeploy

**Migration files are in** `alembic/versions/`.

**Adding a new model** (like `FCMToken`):
1. Create the SQLAlchemy model file in `api/models/`
2. Register it in `api/models/__init__.py`
3. Generate migration: `alembic revision --autogenerate -m "add fcm_tokens table"`
4. The migration is included in the Docker image (`.cicd/Dockerfile.api` copies `alembic/`)

## Full End-to-End Flow

```
1. git checkout -b feature/xxx
2. Make changes, commit, test
3. git push → create PR → merge to main
4. CI auto-builds → poll until complete
5. ASK USER: "Deploy?"
6. If yes → trigger CD → poll until complete
7. Report result to user
```

## Infrastructure

| Resource | Identifier |
|----------|------------|
| GitHub Repo | `Anlitico/pigugu-server` |
| EKS Cluster | `pigugu-cluster` (us-west-1) |
| RDS (PostgreSQL) | `pigugu-db.c1esma68egsk.us-west-1.rds.amazonaws.com:5432` |
| ElastiCache (Redis) | `pigugu-redis.feoudk.ng.0001.usw1.cache.amazonaws.com:6379` |
| ECR API | `pigugu-api` |
| ECR Agent | `pigugu-agent` |
| ECR Tools | `pigugu-tools` |
| K8s Namespace | `default` |
| Proxy | Clash HTTP 127.0.0.1:7897 |
