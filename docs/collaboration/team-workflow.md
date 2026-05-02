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

We use a decoupled CI/CD strategy to ensure safe releases.

### Phase 1: Automated Build
Every time a PR is merged into `main`, GitHub Actions automatically:
- Builds new Docker images for API and Agent.
- Pushes images to Amazon ECR with the tags `latest` and the `commit-sha`.

### Phase 2: Manual Deployment to EKS
Merging code does **not** automatically update the live server. A manual step is required:

1. Navigate to the **Actions** tab in GitHub.
2. Select the **"Deploy to Amazon EKS"** workflow.
3. Click the **"Run workflow"** button.
4. **Image Tag**: Use `latest` for the most recent code, or a specific `commit-sha` for rollbacks.
5. Click **"Run workflow"**.

The deployment uses a **Rolling Update** strategy, ensuring zero downtime. You can monitor the progress in the workflow logs via `kubectl rollout status`.

## 4. Monitoring & Troubleshooting
- **Pod Status**: Check in AWS Console under EKS > Resources > Pods.
- **Logs**:
  ```bash
  kubectl logs -l app=pigugu-api --tail 100
  ```
- **Endpoint**: Access the API via the LoadBalancer URL found in the `05-deployment-walkthrough.md` document.
