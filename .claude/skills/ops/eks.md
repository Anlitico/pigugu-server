# EKS Operations

## Pod Management

```bash
# Check pod status
kubectl get pods -l app=pigugu-agent
kubectl get pods -l app=pigugu-api

# Deployment status
kubectl get deployments pigugu-agent pigugu-api

# Restart agent
kubectl rollout restart deployment/pigugu-agent

# Wait for rollout
kubectl rollout status deployment/pigugu-agent --timeout=120s
```

## Logs

```bash
# Recent logs
kubectl logs deployment/pigugu-agent --tail=100

# Recent activity (filter noise)
kubectl logs deployment/pigugu-agent --since=5m | grep -v "config:_log_config"

# STT and LLM activity
kubectl logs deployment/pigugu-agent --since=5m | grep -E "\[STT\] Final|llm_node|Reply complete"

# Errors only
kubectl logs deployment/pigugu-agent --since=10m | grep -i error

# Follow live
kubectl logs deployment/pigugu-agent --tail=20 -f
```

## Deploy

```bash
# Build triggers automatically on push to main — but only for the packages
# whose paths changed, so a tag exists per package, not per commit.
# Deploy is manual; pass the commit sha to deploy that commit's build, or
# `latest` to take each repository's newest build (which may be different
# commits for api and agent). Both are handled: a service the tag was not
# published for falls back to its own repository's `:latest`.
gh workflow run ".github/workflows/deploy.yml" --repo Anlitico/pigugu-server -f image_tag=<sha|latest>
```
