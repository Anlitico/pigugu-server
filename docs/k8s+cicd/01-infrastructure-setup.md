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

## 7. Portal Website (S3 + CloudFront)
The product portal is a static site served from `www.pigugu.net`, deployed from
`web/site/` in this repository. Source content and infrastructure are kept in
separate directories so that `aws s3 sync web/site/` never publishes the
template itself.

### Deploy the stack
One stack in **us-east-1** creates the private S3 origin, the CloudFront
distribution, the ACM certificate and the Route53 records:

```bash
aws cloudformation deploy \
  --stack-name pigugu-portal \
  --template-file web/infra/portal.yaml \
  --region us-east-1
```

us-east-1 is required: CloudFront only accepts viewer certificates from that
region. The S3 bucket is created alongside it, which is harmless — CloudFront
reads the bucket over its regional endpoint.

`pigugu.net` (apex) is an alias of the same distribution and a CloudFront
Function issues a `301` to `www.pigugu.net`, so the bare domain works too.
TLS is a single certificate covering both names.

### Publishing content
Publishing is its own manually-dispatched workflow,
`.github/workflows/deploy-portal.yml`, which syncs `web/site/` and invalidates
the cache. It is intentionally not part of `deploy.yml`: the portal is content,
so a copy change should not require running DB migrations and rolling the
api/agent pods, and a backend failure should not block the publish.

To publish manually, either dispatch **Deploy Portal** from the Actions tab, or
run the same steps locally:

```bash
aws s3 sync web/site/ s3://pigugu-web/ --delete --region us-east-1

DISTRIBUTION_ID=$(aws cloudformation describe-stacks \
  --stack-name pigugu-portal --region us-east-1 \
  --query "Stacks[0].Outputs[?OutputKey=='DistributionId'].OutputValue" \
  --output text)
aws cloudfront create-invalidation \
  --distribution-id "$DISTRIBUTION_ID" --paths "/" "/index.html" --region us-east-1
```

Pass `--region us-east-1` explicitly when running these by hand. `deploy.yml`
defaults its environment to us-west-1, and S3 answers a cross-region request
with a `301` redirect that `aws s3 sync` reports as a failure. The workflow
itself sets `AWS_REGION: us-east-1`, so it needs no per-command flags.

### IAM permissions
The CI user needs, in addition to the EKS/ECR permissions above:

- `s3:ListBucket`, `s3:PutObject`, `s3:DeleteObject` on `pigugu-web`
- `cloudfront:CreateInvalidation` on the portal distribution
- `cloudformation:DescribeStacks` on `pigugu-portal`
