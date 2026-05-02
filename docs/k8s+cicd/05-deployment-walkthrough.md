# Step 05: Deployment Walkthrough

Follow this checklist for a successful deployment to the current EKS environment.

## Phase 1: Local Preparation
1. Ensure your `pyproject.toml` includes all dependencies (e.g., `email-validator` is required for Pydantic).
2. Commit your changes to `main` and push. This will trigger the **Build and Push** workflow.
3. Verify the build is green in the GitHub Actions tab.

## Phase 2: Manual Trigger
1. Go to GitHub Actions and manually run the **"Deploy to Amazon EKS"** workflow.
2. Use the `latest` tag for the initial deployment.
3. Observe the logs to see the `__DATABASE_URL__` injection and `kubectl set image` execution.

## Phase 3: Verification in EKS
1. **Check Pods**: `kubectl get pods`. Look for `Running` status.
2. **Check Logs**: If a Pod is restarting, check the logs:
   ```bash
   kubectl logs -l app=pigugu-api
   ```
3. **Check Service**:
   ```bash
   kubectl get svc pigugu-api-service
   ```
   Access the `EXTERNAL-IP` to verify the API is reachable.

### Agent Pod Checks
The agent is a background LiveKit worker, so it does not have a public Service or URL.

```bash
kubectl rollout status deployment/pigugu-agent
kubectl get pods -l app=pigugu-agent
kubectl logs -l app=pigugu-agent --tail=100
```

For `kubectl get pods`, a healthy first result is `READY` showing `1/1`, `STATUS` showing `Running`, and `RESTARTS` staying at `0` or not increasing. In the logs, look for agent startup messages showing configuration loading and the LiveKit worker starting.

Common bad states:
- `ImagePullBackOff`: Kubernetes cannot pull the ECR image. Check the image tag and ECR access.
- `CrashLoopBackOff`: The container starts and then exits repeatedly. Check logs for missing secrets, config errors, or Python import errors.
- `Pending`: Kubernetes has not scheduled the pod yet. Check node capacity and resource requests.
- Increasing `RESTARTS`: The pod is unstable even if it currently says `Running`.

## Phase 4: Database Initialisation
Since we are using a fresh RDS instance, you may need to run migrations:
```bash
# Example: Run alembic migrations via a temporary pod or from a local machine with VPN access to the VPC
alembic upgrade head
```

## Troubleshooting (Lessons Learned)
- **ImportError: email-validator**: Ensure `email-validator` is in `pyproject.toml`.
- **Firebase Initialization Error**: We have made Firebase optional; it will now log a warning instead of crashing if the credentials file is missing.
- **RDS Connectivity**: If pods cannot connect to the DB, verify that the RDS Security Group allows inbound traffic from the EKS nodes on port 5432.
- **Pod OOM/Pending**: Since we use `t3.small` nodes, ensure your resource limits in YAML are not too high.
