# Step 04: CI/CD Pipeline with GitHub Actions

Our pipeline is split into two phases: **Automatic Build** and **Manual Deployment**.

## 1. Configure GitHub Secrets
Go to **Settings > Secrets and variables > Actions** and add:
- `AWS_ACCESS_KEY_ID`: Your AWS access key.
- `AWS_SECRET_ACCESS_KEY`: Your AWS secret key.
- `DB_PASSWORD`: The PostgreSQL password for RDS (used to inject into `secrets.yaml`).

## 2. CI/CD Workflow Logic

### Phase 1: Automatic Build & Push
Triggered on every `push` to the `main` branch.
- **Goal**: Build Docker images and push them to ECR.
- **Tags**: Every image is tagged with the `git commit sha` and `latest`.

### Phase 2: Manual Deployment
Triggered via **Workflow Dispatch** (Manual button in Actions tab).
- **Goal**: Update the EKS cluster with a specific image version.
- **Input**: You can specify an `image_tag` (default is `latest`).
- **Security**: The `DB_PASSWORD` is injected into `k8s/secrets.yaml` during this phase using `sed`.
- **Method**: Uses `kubectl set image` for a safe, non-destructive update of the running deployment.

## 3. How to Deploy Manually
1. Go to the **Actions** tab in GitHub.
2. Select the **"Deploy to Amazon EKS"** workflow.
3. Click the **"Run workflow"** dropdown button.
4. (Optional) Enter a specific image tag (e.g., a commit SHA) or leave it as `latest`.
5. Click **"Run workflow"**.

## 4. Deployment Monitoring
The pipeline uses `kubectl rollout status` to wait for the deployment to finish. You can monitor the progress directly in the GitHub Actions logs.

- **Success**: Pods are replaced using a Rolling Update strategy.
- **Rollback**: If a deployment fails (e.g., due to a crash), you can trigger a manual deployment with a previous known-good `image_tag` (commit SHA).
