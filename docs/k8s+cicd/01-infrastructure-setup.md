# Step 01: Infrastructure Setup

This guide covers the initial setup of AWS resources required for the Pigugu server in **us-west-1**.

## 1. Setup AWS CLI
Ensure you have the AWS CLI installed and configured:
```bash
aws configure
```

## 2. Create ECR Repositories
```bash
# API Repository
aws ecr create-repository --repository-name pigugu-api --region us-west-1

# Agent Repository
aws ecr create-repository --repository-name pigugu-agent --region us-west-1
```

## 3. Create EKS Cluster
We used `eksctl` with a cost-optimized configuration (Public nodes, no NAT Gateway).

```bash
eksctl create cluster \
  --name pigugu-cluster \
  --region us-west-1 \
  --version 1.35 \
  --nodegroup-name standard-nodes \
  --node-type t3.small \
  --nodes 2 \
  --vpc-nat-mode Disable
```
*Note: Using `t3.small` and disabling NAT Gateway keeps the setup within or close to the Free Tier.*

## 4. Database Setup (Amazon RDS)
We use Managed PostgreSQL for production stability.

### Create RDS Instance (PostgreSQL 18.3)
```bash
aws rds create-db-instance \
  --db-instance-identifier pigugu-db \
  --db-instance-class db.t3.micro \
  --engine postgres \
  --engine-version 18.3 \
  --allocated-storage 20 \
  --master-username pigugu \
  --master-user-password YOUR_SECURE_PASSWORD \
  --region us-west-1 \
  --no-publicly-accessible
```

### Security Group Configuration
1. Identify the EKS Cluster Security Group (e.g., `sg-eks-cluster`).
2. Identify the RDS Security Group (e.g., `sg-rds-db`).
3. **Important**: Add an Inbound Rule to the RDS Security Group:
   - **Type**: PostgreSQL (5432)
   - **Source**: EKS Cluster Security Group ID

## 5. Redis (Optional)
Currently, Redis is disabled to simplify initial deployment. For future scaling:
1. Create Amazon ElastiCache (Redis OSS).
2. Allow port 6379 from EKS nodes.

## 6. IAM Permissions for CI/CD
Store these in **GitHub Secrets**:
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
