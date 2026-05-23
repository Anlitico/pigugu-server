# Step 02: Docker Optimization for Production

To ensure fast deployment and security in EKS, we should optimize our Docker images.

## 1. Multi-Stage Build Strategy
Instead of installing development tools in the final image, we use a builder stage.

### Optimized Dockerfile.api (Example)
```dockerfile
# Stage 1: Build dependencies
FROM python:3.11-slim AS builder

WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir wheel && \
    pip wheel --no-cache-dir --wheel-dir /app/wheels -e .

# Stage 2: Final image
FROM python:3.11-slim

WORKDIR /app
COPY --from=builder /app/wheels /wheels
RUN pip install --no-cache-dir /wheels/*

COPY api/ ./api/
COPY alembic/ ./alembic/
COPY alembic.ini .

# Security: Run as non-root user
RUN useradd -m pigugu
USER pigugu

EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

## 2. Handling Secret Files
In local dev, you use `firebase-credentials.json`. In EKS, **do not bake this into the image**.
We will mount this file using Kubernetes Secrets.

## 3. Environment Variables
Ensure your application reads configuration from environment variables:
- `DATABASE_URL`
- `REDIS_URL`
- `FIREBASE_CONFIG_PATH`

## 4. Local Verification
Before pushing to ECR, verify the production image locally:
```bash
docker build -t pigugu-api:prod -f Dockerfile.api .
docker run --env-file .env pigugu-api:prod
```
