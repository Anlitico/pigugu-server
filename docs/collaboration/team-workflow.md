# Team Collaboration & Deployment Guide

This document outlines the workflow for developing, contributing, and deploying code to the Pigugu Server.

## 1. Development Workflow

### Local Setup
1. **Clone the repository**:
   ```bash
   git clone https://github.com/Anlitico/pigugu-server.git
   cd pigugu-server
   ```
2. **Install dependencies**:
   ```bash
   pip install -e .
   ```
3. **Environment Variables**:
   Create a local `.env` file for development. Do **not** commit this file.

### Branching Policy
*   **main**: Protected branch. Direct pushes are disabled. This branch represents the stable production code.
*   **feature/* or fix/* **: Create a new branch for every task.
    ```bash
    git checkout -b feature/your-feature-name
    ```

## 2. Contributing Code (The PR Process)

1. **Commit your changes**: Follow conventional commit messages (e.g., `feat: add user login`, `fix: resolve db timeout`).
2. **Push to your branch**:
   ```bash
   git push origin feature/your-feature-name
   ```
3. **Open a Pull Request (PR)**:
   - Go to GitHub and open a PR from your branch to `main`.
   - Ensure the automated **Build and Push** check passes.
4. **Code Review**: At least one other team member should review the code.
5. **Merge**: Once approved, merge the PR. This will automatically trigger a new Docker build in the background.

## 3. Deployment (CI/CD)

We use a decoupled CI/CD strategy. Merging code **Builds** it, but you must manually **Deploy** it.

### Phase 1: Automated Build (After MR)
1. **Trigger**: Every time a PR is merged into `main`.
2. **Where to watch**: Go to the **Actions** tab in GitHub. You will see a workflow run named `Build and Push`.
3. **Status**: Green checkmark means the Docker images are successfully stored in Amazon ECR.

### Phase 2: Manual Deployment to EKS
Merging code does **not** update the server. You must trigger the final release:

1. **Navigate**: Go to the **Actions** tab.
2. **Select**: Click **"Deploy to Amazon EKS"** on the left sidebar.
3. **Trigger**: Click the **"Run workflow"** button on the right.
4. **Input**:
   - `image_tag`: Keep `latest` for the newest code.
5. **Monitor Logs**: 
   - Click on the running job to see the live console output.
   - You will see `kubectl apply` and `kubectl rollout status`.
   - **Success Criteria**: The log should end with `deployment "pigugu-api" successfully rolled out`.

## 4. How to Verify Success

### In AWS Console (GUI)
1. **Pod Refresh**: Go to **EKS > Clusters > pigugu-cluster > Resources > Pods**.
   - Check the **Age** column. New pods should show an age of "seconds" or "a few minutes".
   - Status must be **Running**.
2. **Load Balancer**: Go to **EC2 > Load Balancers**.
   - Ensure the associated ELB is **Active**.

### CLI Verification
```bash
# Check if pods are running with the new version
kubectl get pods

# Check application logs for startup errors
kubectl logs -l app=pigugu-api --tail 50
```

## 5. Troubleshooting
- **Build Fails**: Check `pyproject.toml` for missing dependencies.
- **Deploy Fails**: Ensure `DB_PASSWORD` is correctly set in GitHub Secrets.
- **Pods won't start**: Check `kubectl describe pod <pod-name>` for "ImagePullBackOff" (permissions) or "CrashLoopBackOff" (app error).
