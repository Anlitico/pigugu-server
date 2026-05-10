# Trump Social Media Crawler 合并计划

## 目标

把 `localDocs/CRAWLER.md` 描述的 Trump 社媒爬虫合并进当前 PiguguServer 项目，但先不执行代码迁移。合并后的形态应该是一个独立定时任务，每天抓取 Truth Social 和 X/Twitter 数据，直接写入项目数据库，供后续 agent 查询和使用。

同事建议里的关键约束：

- 爬虫保持独立定时任务，不塞进现有 API 请求链路。
- 数据库结构先设计清楚，review 后再发起 Alembic 迁移。
- 使用项目现有 SQLAlchemy model 和 Alembic 迁移体系。

## 当前项目相关现状

项目数据库层在 `app/core/database.py`，使用 async SQLAlchemy session。现有 model 都放在 `app/models/`，并通过 `app/models/__init__.py` 注册给 Alembic。Alembic 的 `env.py` 已经加载 `app.models`，所以新增表的 model 也应该放进 `app/models/` 并更新 `__all__`。

当前依赖里已经有 `httpx`、`feedparser`、`apscheduler`、`sqlalchemy[asyncio]`、`asyncpg` 和 `alembic`。爬虫提到的 `curl_cffi` 目前不在 `pyproject.toml`，合并代码时需要新增这个依赖。

## 推荐架构

第一版保持简单：

- 新增一个 crawler package，例如 `app/jobs/trump_social_crawler/`。
- 爬虫入口做成 CLI module，例如 `python -m app.jobs.trump_social_crawler --date 2026-05-04`。
- 生产环境用 Kubernetes CronJob 或现有部署环境的定时任务每天运行一次。
- 爬虫任务直接使用 `AsyncSessionLocal` 写数据库。
- 不新增对外 API，除非 agent 后续需要 HTTP 查询接口。

这样可以复用现有数据库连接、配置和迁移体系，同时避免把爬虫生命周期绑到 FastAPI 服务进程里。

## 数据库设计草案

新增表建议命名为 `trump_social_posts`。它只表达「抓到的一条社媒帖子」，不要混入 agent 处理状态，避免后续用途扩张时表结构变复杂。

建议字段：

- `id`: UUID 主键，和现有 model 风格一致。
- `platform`: 字符串，限定业务值为 `truthsocial` 或 `x`。
- `post_id`: 平台原始帖子 ID。
- `content`: 帖子正文 HTML 或 RSS 内容。
- `url`: 原帖 URL。
- `created_at`: 帖子发布时间。
- `crawled_at`: 爬虫抓取时间。
- `replies_count`: Truth Social 回复数，X 可为空。
- `reblogs_count`: Truth Social re-truth 数，X 可为空。
- `favourites_count`: Truth Social like 数，X 可为空。
- `upvotes_count`: Truth Social upvote 数，X 可为空。
- `media_attachments`: JSONB，保存图片、视频和元信息。
- `tags`: JSONB，保存 hashtag 数组。
- `mentions`: JSONB，保存 mention 数组。
- `raw_payload`: JSONB，保存原始平台数据，方便后续 agent 或调试使用。
- `inserted_at`: 数据入库时间，server default `now()`。
- `updated_at`: 数据更新时间，用于重复抓取同一帖子时刷新 engagement metrics。

约束和索引：

- 唯一约束：`platform + post_id`。
- 索引：`platform + created_at`，用于按平台和日期查询。
- 索引：`created_at`，用于 agent 拉取某天或最近内容。

已确认的设计点：

- `content` 第一版只存现有爬虫产出的内容，不拆成 `content_html` 和 `content_text`。
- 不单独建 `trump_social_media` 表，`media_attachments` 用 JSONB 存。
- `replies_count`、`reblogs_count`、`favourites_count`、`upvotes_count` 这些互动计数保留为独立列，不放进 JSONB。这样后续可以直接按点赞数、回复数或转发数排序和筛选。

## 代码合并步骤

第一步，整理爬虫代码边界：

- 把现有 `crawl.py` 迁入 `app/jobs/trump_social_crawler/`。
- 拆出 `fetch_truthsocial.py`、`fetch_x.py`、`schemas.py`、`repository.py` 和 `__main__.py`。
- 保留 `--platform` 和 `--date` 参数。
- 输出从写 JSON 文件改成写数据库。

第二步，新增数据库 model：

- 新建 `app/models/trump_social_post.py`。
- 在 `app/models/__init__.py` 导入并加入 `__all__`。
- 使用现有 `Base`、`Mapped`、`mapped_column` 风格。

第三步，新增 Alembic 迁移：

- 等 DB schema 被 review 通过后再执行 `alembic revision --autogenerate`。
- 迁移文件只包含 `trump_social_posts` 表、唯一约束和索引。
- 不在 review 前实际发起迁移。

第四步，写入逻辑：

- 每次抓到帖子后按 `platform + post_id` upsert。
- 已存在的帖子更新 engagement metrics、`media_attachments`、`tags`、`mentions`、`raw_payload` 和 `updated_at`。
- 不存在的帖子插入完整记录。
- 单次任务完成后 commit；失败时依赖 session rollback。

第五步，调度：

- 本地开发使用手动命令运行。
- 生产建议用 Kubernetes CronJob，每天固定时间运行一次。
- CronJob 使用和 API 相同的 database secret。
- 任务超时、重启策略和日志保留在 K8s 层配置。

## Agent 使用方式预留

第一版不直接改 agent。先把数据稳定入库，后续再根据 agent 的实际查询方式补查询层。

后续可选两种方式：

- 直接在 agent 内通过数据库 session 查询 `trump_social_posts`。
- 在 FastAPI 里加内部接口，例如按日期或最近 N 条读取帖子。

如果 agent 和 API 会部署成独立容器，推荐优先考虑 FastAPI 内部接口，减少 agent 直接持有数据库访问逻辑。

## 验证计划

合并代码后再做这些验证：

- 本地跑指定日期 backfill，确认同一天重复运行不会插入重复数据。
- 验证 Truth Social 的计数字段能更新。
- 验证 X/Twitter RSS 字段允许 engagement metrics 为空。
- 验证 Alembic autogenerate 只生成预期表和索引。
- 验证 CronJob 失败时不会留下半写入状态。

## 推荐执行顺序

1. 先 review 本文 DB schema。
2. 确认表名、字段和索引。
3. 迁入爬虫代码，但先保持只支持手动运行。
4. 新增 model。
5. 发起 Alembic migration。
6. 改写爬虫输出为数据库 upsert。
7. 本地验证。
8. 加生产 CronJob。
9. 等数据稳定后，再接 agent 查询逻辑。

## 暂不做

- 不把爬虫塞进 FastAPI startup。
- 不在第一版新增公网 API。
- 不把 JSON 文件输出作为数据库失败时的备用路径。
- 不提前设计复杂的 agent 消费状态表。
- 不在 schema review 前执行 Alembic migration。
