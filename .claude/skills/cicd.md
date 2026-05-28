---
name: cicd
description: Code management, CI/CD pipeline, and deployment workflow for pigugu-server
---

# pigugu-server Code Management & CI/CD

## Project Structure (Monorepo)

```
pigugu-server/
├── api/                    # FastAPI backend (port 8000)
├── pigagent/               # LiveKit voice agent worker
├── .cicd/                  # Dockerfiles
│   ├── Dockerfile.api      #   → pigugu-api ECR image (includes alembic + migrations)
│   └── Dockerfile.agent    #   → pigugu-agent ECR image
├── k8s/                    # Kubernetes manifests
│   ├── api.yaml            #   Deployment + LoadBalancer Service (SSL on 443)
│   ├── agent.yaml          #   Deployment + ConfigMap
│   ├── secrets.yaml        #   Secret (placeholders injected by CI)
│   ├── migration-job.yaml  #   Job (runs alembic upgrade head in EKS)
│   └── crawler-cronjob.yaml #  CronJob (Trump social crawler, daily 10am)
├── alembic/                # Database migrations (asyncpg)
│   └── versions/           #   9 migration scripts
├── .github/workflows/
│   └── deploy.yml          #   Single workflow: build + deploy
└── alembic.ini             #   Migration config (DATABASE_URL overridden at runtime)
```

## Infrastructure (AWS us-west-1)

| Resource | Identifier |
|----------|------------|
| EKS Cluster | `pigugu-cluster` |
| RDS (PostgreSQL) | `pigugu-db.c1esma68egsk.us-west-1.rds.amazonaws.com:5432` |
| ElastiCache (Redis) | `pigugu-redis.feoudk.ng.0001.usw1.cache.amazonaws.com:6379` |
| ECR (API) | `pigugu-api` |
| ECR (Agent) | `pigugu-agent` |

- RDS is **not publicly accessible** — only reachable from within VPC (EKS, etc.)
- RDS SG (`sg-08f83ca426bf3b252`) only allows inbound 5432 from EKS node group SG (`sg-008101e1f1509c0b2`)
- K8s namespace: `default`
- K8s ServiceAccount: `pigugu-server-sa`

## Git Workflow

1. **Create feature branch** from `main`:
   ```
   git checkout -b feat/<feature-name>
   ```

2. **Make changes**, commit, push:
   ```
   git add <files>
   git commit -m "type: description"
   git push -u origin feat/<feature-name>
   ```

3. **Create PR** on GitHub: `feat/<feature-name>` → `main`
   - Merge triggers CI build automatically

4. **After merge to main**: CI builds Docker images, CD is **manual only**

## CI/CD Pipeline (`.github/workflows/deploy.yml`)

### Build Job (`build`) — Automatic on push to main

Triggered by: `git push` to `main` (PR merge)

1. Checkout → AWS login → ECR login
2. `docker build -f .cicd/Dockerfile.api .` → push to ECR (`pigugu-api:latest` + `:git-sha`)
3. `docker build -f .cicd/Dockerfile.agent .` → push to ECR (`pigugu-agent:latest` + `:git-sha`)

### Deploy Job (`deploy`) — Manual only (`workflow_dispatch`)

Triggered by: Manual dispatch with `image_tag` input (default: `latest`)

1. Checkout → AWS login → ECR login → `aws eks update-kubeconfig`
2. **Run database migrations** (K8s Job):
   - Delete old `pigugu-db-migration` job if exists
   - Replace `__IMAGE__` in `k8s/migration-job.yaml` with actual ECR image tag
   - `kubectl apply -f` → `kubectl wait --for=condition=complete --timeout=120s`
   - On failure: print pod logs, delete job, exit 1
   - On success: print logs, delete job
3. **Deploy to EKS**:
   - Inject secrets from GitHub Secrets into `k8s/secrets.yaml` placeholders (`__KEY__`)
   - `kubectl apply -f k8s/secrets.yaml`
   - `kubectl apply -f k8s/api.yaml`
   - `kubectl apply -f k8s/agent.yaml`
   - `kubectl apply -f k8s/crawler-cronjob.yaml`
   - `kubectl set image` for both deployments (uses `image_tag` input)
   - `kubectl rollout restart` + `kubectl rollout status` for both deployments

### How to Deploy

```bash
# Via GitHub CLI
gh workflow run ".github/workflows/deploy.yml" --repo Anlitico/pigugu-server -f image_tag=latest

# Or go to GitHub → Actions → Deploy to Amazon EKS → Run workflow
```

**Important notes:**
- Deploy is **always manual** — no automatic deploy on push
- Build must succeed first (images in ECR) before deploy can work
- The `image_tag` input should match an existing ECR image tag (use `latest` for most recent build, or a specific git SHA)
- Database migrations run before pod restarts — if migration fails, old pods keep running
- The migration job runs inside EKS, which has VPC access to the private RDS
- After migration succeeds, `rollout restart` triggers new pods pulling the new image

## Adding New Secrets

When a new API key or secret is needed in production:

1. **Add to GitHub Secrets**: Repo Settings → Secrets and variables → Actions → New repository secret
2. **Add to `deploy.yml`** `env:` block in the deploy job (line ~97-108):
   ```yaml
   NEW_KEY: ${{ secrets.NEW_KEY }}
   ```
3. **Add to Python injection list** in deploy step (the `for key in (...)` tuple):
   ```python
   "NEW_KEY",
   ```
4. **Add placeholder to `k8s/secrets.yaml`**:
   ```yaml
   NEW_KEY: "__NEW_KEY__"
   ```
5. **Reference in the relevant K8s Deployment** if needed:
   ```yaml
   - name: NEW_KEY
     valueFrom:
       secretKeyRef:
         name: pigugu-secrets
         key: NEW_KEY
   ```

## Database Migrations

- Tool: Alembic with asyncpg
- Migration files: `alembic/versions/` (9 migrations as of 2026-05)
- Run automatically during deploy via K8s Job
- To create a new migration:
  ```bash
  cd d:\projects\pigugu-server
  alembic revision --autogenerate -m "description_of_change"
  ```
- The migration is included in the Docker image (`.cicd/Dockerfile.api` copies `alembic/` and `alembic.ini`)
- Never run `alembic upgrade head` locally against production RDS — RDS is private, only reachable from EKS

## Docker Image Builds

- Images are built **from repo root** context (not from the subdirectory):
  ```bash
  docker build -t pigugu-api -f .cicd/Dockerfile.api .
  ```
- This gives Dockerfiles access to both `api/` and `alembic/` directories
- API image entrypoint: `alembic upgrade head && uvicorn main:app --host 0.0.0.0 --port 8000`
- Agent image: runs LiveKit worker that connects to LiveKit server

## Troubleshooting

### Deploy fails at migration step
- Check migration job logs: the CI step already prints them on failure
- If job can't pull image: check ECR permissions / image tag exists
- If migration fails: check the Alembic error in logs, fix migration script, rebuild, redeploy
- Pods continue running old version if migration fails (safe rollback)

### Build fails
- Check Dockerfile paths: must use `-f .cicd/Dockerfile.*` from repo root
- Common issue: new dependencies not in `pyproject.toml`

### RDS connection issues from local
- RDS is private — use a VPN or SSH tunnel through a bastion host to connect
- Never make RDS publicly accessible just for local debugging
