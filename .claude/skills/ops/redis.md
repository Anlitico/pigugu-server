# Redis Queries

Redis is at `pigugu-redis.feoudk.ng.0001.usw1.cache.amazonaws.com:6379` (private subnet).

## One-liner

```powershell
kubectl run redis-check --rm -i --restart=Never --image=redis:7-alpine -- redis-cli -h pigugu-redis.feoudk.ng.0001.usw1.cache.amazonaws.com -p 6379 <COMMAND> <args>
```

## Common Operations

```bash
# List keys matching pattern
kubectl run redis-check --rm -i --restart=Never --image=redis:7-alpine \
  -- redis-cli -h pigugu-redis.feoudk.ng.0001.usw1.cache.amazonaws.com -p 6379 \
  KEYS "ctx:<user_id>:*"

# Check key exists
kubectl run redis-check --rm -i --restart=Never --image=redis:7-alpine \
  -- redis-cli -h pigugu-redis.feoudk.ng.0001.usw1.cache.amazonaws.com -p 6379 \
  EXISTS "ctx:<user_id>:turns"

# Get value
kubectl run redis-check --rm -i --restart=Never --image=redis:7-alpine \
  -- redis-cli -h pigugu-redis.feoudk.ng.0001.usw1.cache.amazonaws.com -p 6379 \
  GET "ctx:<user_id>:summary"

# All keys
kubectl run redis-check --rm -i --restart=Never --image=redis:7-alpine \
  -- redis-cli -h pigugu-redis.feoudk.ng.0001.usw1.cache.amazonaws.com -p 6379 \
  KEYS "*"
```

## Key Patterns

All keys are under `ctx:{user_id}:` namespace:
- `ctx:{user_id}:turns` — conversation turns list
- `ctx:{user_id}:summaries` — L2/L3/L4 compression summaries
- `ctx:{user_id}:compressing` — compression lock flag
- `ctx:{user_id}:game_state` — active game state
