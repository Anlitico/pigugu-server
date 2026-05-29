# PostgreSQL Queries

Credentials are read from `.env` (`PGPASSWORD`, `PGHOST`, `PGUSER`, `PGDATABASE`).

## One-liner

```powershell
$env:PGPASSWORD = (Select-String '^PGPASSWORD=(.*)' .env).Matches.Groups[1].Value; kubectl run pg-q --rm -i --restart=Never --image=postgres:16-alpine --env "PGPASSWORD=$env:PGPASSWORD" -- psql -h pigugu-db.c1esma68egsk.us-west-1.rds.amazonaws.com -U pigugu -d pigugu -c "YOUR_SQL_HERE"
```

## CSV output

Add `--csv` before `-c`:
```powershell
... -- psql ... --csv -c "SELECT ..."
```

## Gotchas

- **NEVER use `-it`** — the PowerShell tool does not support TTY, use `-i` only
- **Keep SQL in a single line** — multi-line here-strings with variables cause hangs
- **Use single quotes** for SQL strings inside the query (e.g., `WHERE name='foo'`)
- Pod name must be unique each run; `--rm` auto-cleans after completion

## Common Queries

```sql
-- User conversation history
SELECT turn_number, role, left(content, 100), partial, created_at
FROM agent_conversations WHERE user_id='<user_id>' ORDER BY turn_number;

-- Recent conversations across all users
SELECT user_id, count(*) as turns, min(created_at), max(created_at)
FROM agent_conversations GROUP BY user_id ORDER BY max(created_at) DESC LIMIT 10;

-- Tool calls in conversations
SELECT turn_number, role, left(content, 60), tool_calls, tool_call_id, name
FROM agent_conversations WHERE user_id='<user_id>' AND (tool_calls IS NOT NULL OR role='tool')
ORDER BY turn_number;

-- Current alembic migration version
SELECT * FROM alembic_version;

-- List all tables
SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name;

-- Conversation counts by user
SELECT user_id, role, count(*) FROM agent_conversations GROUP BY user_id, role ORDER BY user_id, role;

-- Turn metrics (latency data)
SELECT * FROM metrics WHERE user_id='<user_id>' ORDER BY turn_number DESC LIMIT 10;

-- Roast scenarios
SELECT roast_id, game_mode, headline, teaser, left(prompt, 200), tags, status, created_at
FROM roast_scenarios ORDER BY created_at DESC;
```
