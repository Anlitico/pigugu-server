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
- **Tags**: Each built image is tagged with the `git commit sha` and `latest`. Builds are path-filtered per package, so a given commit usually publishes only the images whose sources it touched — a tag exists per package, not per commit.

### Phase 2: Manual Deployment
Triggered via **Workflow Dispatch** (Manual button in Actions tab).
- **Goal**: Update the EKS cluster with a specific image version.
- **Input**: You can specify an `image_tag` (default is `latest`).
- **Security**: The `DB_PASSWORD` is injected into `k8s/secrets.yaml` during this phase using `sed`.
- **Method**: Resolves each deployment's image: the requested tag where ECR published it for that commit; otherwise that repository's `:latest`; otherwise (that repository has no `:latest` either) the image it is already running. The resolved image is injected into the manifests via `__IMAGE__`, applied, and both deployments are restarted.

## 3. How to Deploy Manually
1. Go to the **Actions** tab in GitHub.
2. Select the **"Deploy to Amazon EKS"** workflow.
3. Click the **"Run workflow"** dropdown button.
4. (Optional) Enter a specific image tag (e.g., a commit SHA) or leave it as `latest`.
5. Click **"Run workflow"**.

## 4. Deployment Monitoring
The pipeline uses `kubectl rollout status` to wait for the deployment to finish. You can monitor the progress directly in the GitHub Actions logs.

- **Success**: Pods are replaced using a Rolling Update strategy.
- **Rollback**: If a deployment fails (e.g., due to a crash), you can trigger a manual deployment with a previous known-good `image_tag` (commit SHA). Two caveats:
  - It is only a *partial* rollback when that SHA published an image for just one package: the other service falls back to its own `:latest`, i.e. moves forward rather than back — dispatch a SHA that built both when you need them to move together.
  - Rolling the **api** back past a migration does not work. The database is already on the newer revision and the migration job runs the rolled-back image, whose `alembic upgrade heads` cannot locate that revision: the job exits non-zero and the run fails before anything is applied. Old pods keep running (safe), but you are *not* rolled back — undoing the schema takes a new commit.
