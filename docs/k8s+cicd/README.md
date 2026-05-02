# Pigugu Server EKS & CI/CD Documentation

Welcome to the deployment guides. Follow these steps in order to move your Pigugu server from local development to a scalable AWS EKS environment.

## Documentation Index

1. [**01-infrastructure-setup.md**](./01-infrastructure-setup.md)
   - Setting up AWS CLI, ECR, and the EKS cluster.
2. [**02-docker-optimization.md**](./02-docker-optimization.md)
   - Preparing production-ready Docker images.
3. [**03-kubernetes-manifests.md**](./03-kubernetes-manifests.md)
   - Defining deployments, services, and secrets.
4. [**04-cicd-pipeline.md**](./04-cicd-pipeline.md)
   - Automating everything with GitHub Actions.
5. [**05-deployment-walkthrough.md**](./05-deployment-walkthrough.md)
   - The final checklist and troubleshooting guide.

## Key Tools Used
- **AWS CLI**: Resource management.
- **eksctl**: EKS cluster management.
- **kubectl**: Kubernetes orchestration.
- **Helm**: (Optional) For installing database/cache if not using AWS managed services.
- **GitHub Actions**: CI/CD automation.

---
*Ready to start? Begin with [Step 01: Infrastructure Setup](./01-infrastructure-setup.md).*
