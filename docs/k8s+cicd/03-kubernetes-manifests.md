# Step 03: Kubernetes Manifests

This guide describes the current Kubernetes resource definitions for the Pigugu server.

## 1. Secrets Management
We use `k8s/secrets.yaml` with a template-based injection for sensitive data.

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: pigugu-secrets
type: Opaque
stringData:
  # The __DATABASE_URL__ placeholder is automatically replaced by GitHub Actions
  DATABASE_URL: "__DATABASE_URL__"
```
*Note: We use `stringData` for readability; K8s will automatically convert it to base64.*

## 2. API Deployment (`k8s/api.yaml`)
The API deployment is configured to be lightweight and resilient.

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: pigugu-api
spec:
  replicas: 2
  template:
    spec:
      containers:
      - name: api
        image: <aws-account-id>.dkr.ecr.us-west-1.amazonaws.com/pigugu-api:latest
        env:
        - name: DATABASE_URL
          valueFrom:
            secretKeyRef:
              name: pigugu-secrets
              key: DATABASE_URL
```
*Note: Redis and Firebase dependencies have been made optional in the code to ensure the container starts even if these services are not yet configured.*

## 3. API Service
We use a `LoadBalancer` type to expose the API to the public internet.

```yaml
apiVersion: v1
kind: Service
metadata:
  name: pigugu-api-service
spec:
  type: LoadBalancer
  ports:
    - port: 80
      targetPort: 8000
```

## 4. Agent Deployment (`k8s/agent.yaml`)
The Agent runs as a background worker and does not require a Service. It shares the same database configuration as the API.

## 5. Deployment Commands
While the CI/CD handles this automatically, you can check the status manually:

```bash
# Check Pod status
kubectl get pods

# Get the Public LoadBalancer URL
kubectl get svc pigugu-api-service
```
