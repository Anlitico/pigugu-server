# 固件 OTA 版本管理 + 在线更新 — 技术设计(TECH.md)

> 配套产品文档:[`docs/product-design/ota-prd.md`](../product-design/ota-prd.md)。
> 本文档版本锚点:服务端 `pigugu-server@4dde7f6`、固件 `pigugu-firmware@b0833c7`。行为不变式见 PRD §5,不再重复,本节给可执行方案。

## 1. Context(现状与问题)

### 1.1 设备侧已有但被关闭的 OTA 客户端

固件(xiaozhi-esp32 派生)自带一套完整 OTA 客户端,但**当前线上完全未启用**:

- `main/ota.cc`:`Ota` 类。`CheckVersion()`(ota.cc:81)向 `ota_url` POST 设备信息,解析返回 JSON 的 `activation/mqtt/websocket/server_time/firmware{version,url,force}`;`Upgrade()`(ota.cc:275)走 `esp_ota_begin(OTA_WITH_SEQUENTIAL_WRITES)→write→end→set_boot_partition`,要求 Content-Length 完整、拒 chunked(ota.cc:300-304);`MarkCurrentVersionValid()`(ota.cc:255)调用 `esp_ota_mark_app_valid_cancel_rollback()`;`IsNewVersionAvailable()` semver 比较 + `force` 覆盖(ota.cc:414-427)。`GetCheckVersionUrl()`(ota.cc:50)先读 NVS `wifi/ota_url`,空则回退 `CONFIG_OTA_URL`(Kconfig.projbuild 默认 `https://api.tenclass.net/xiaozhi/ota/`)。
- **跳过开关**:`IsCustomerProvisioningEnabled()`(application.cc:48-51,读 NVS `provisioning/use_cust_mqtt`,默认 true)为真时 `CheckNewVersion()`(application.cc:667-673)**直接跳过 OTA**,仅 `MarkCurrentVersionValid()`。真机日志即 "skip OTA/version check"。
- **触发点**:`Ota` 仅在 `ActivationTask()` 内构造一次(application.cc:576-578),该任务由 `HandleNetworkConnectedEvent()` 在 Starting/WifiConfiguring 态 spawn(application.cc:512-533)——即"上电联网后一次",非周期。
- 手动入口已有:MCP 工具 `self.upgrade_firmware`(mcp_server.cc:189-203)直调 `Ota::Upgrade`。
- 分区与安全:`partitions/v2/16m.csv`(nvs/otadata/phy_init,`ota_0`@0x10000 0x41F000≈4.12MiB、`ota_1` 同、assets spiffs,**无 factory**);`CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y`、`CONFIG_APP_ROLLBACK_ENABLE=y`;无 secure boot/签名/anti-rollback。版本 `PROJECT_VER 2.2.4`(CMakeLists.txt:12)。
- 网络:下载走自研 `HttpClient` over `esp_tls` + `esp_crt_bundle_attach`(esp_ssl.cc:28-37),**无客户端证书、无固定公钥**。MQTT 控制面 = AWS IoT mTLS(真机 `mqtts://agktqmnw53qnj-ats.iot.us-west-1.amazonaws.com:8883`,订阅 `pgg/dev/<mac>/c2d`),broker 凭据来自 provisioning(NVS `provisioning` 命名空间,由 App 侧引导注入)。
- 时钟:`CheckVersion()` 会解析 `server_time{timestamp,timezone_offset}` 并 `settimeofday`(ota.cc:196-219)——对 S3 presigned(SigV4 带时间戳)是必要前提。

### 1.2 服务端只有"配置下发桩",无版本/产物/灰度

- `POST /v1/device/ota`(api/modules/device/router.py:283-308)只按 `Device-Id`/`Client-Id` 头返回 `{websocket:{url,token,version}}`,**从不回 firmware 段**;无鉴权(靠 mac 头识别,沿用现状)。
- 设备注册表 = Postgres `devices`(api/models/device.py:11-42):`hardware_id`(= MAC,唯一)、`thing_name`、`certificate_arn` 等,**无任何固件版本列**;WS hello 里的 `version` 是协议号非固件版。
- 控制面 = AWS IoT:`pgg/dev/{hw_id}/c2d` 下行(api/core/aws.py:21-30);d2c 经 Topic Rule webhook → `POST /v1/device/iot/webhook`(iot.py:424-509),按消息 `type` 处理 `device.online/connectivity.pong/device.register/device.heartbeat`(heartbeat 只刷 Redis 在线态,iot.py:505-507)。
- 无固件产物托管:S3 仅语音 WAV 桶 `pigugu-clickhouse-audio` + IRSA SA `pigugu-s3-sa`;ECR 只放服务镜像。

### 1.3 问题

线上改固件 = USB 刷机;服务端不知任何设备当前版本;无法定向/灰度/回滚;固件侧 OTA 客户端闲置、ota_url 默认指向第三方公共站(若盲开有拉错固件风险)。

## 2. Proposed changes

### 2.0 总体与边界

- **职责边界**:`api` 服务(已有 device 模块/鉴权/AWS/S3 boto3)拥有设备注册表 + OTA 版本/任务;`pigagent`(语音 WS)不动。固件只与 `api` 打交道。
- **通道选型(PRD 拍板)**:扩展 HTTP 检查,不上 AWS IoT Jobs。App 点升级 → 服务端写目标 → c2d `ota.check` 催设备立即检查(复用现成 c2d),离线靠下次上电/周期兜底。
- 版本权威:发布版 = 构建时注入的语义版本(见 2.4);服务端以 `firmware_versions.version` 为唯一权威目标。

### 2.1 服务端:数据模型(alembic,Postgres)

**新表 `firmware_versions`**
`id, board_target(default 'lichuang-dev'), version(唯一 per (board_target,version)), git_sha, file_key(S3 key), size_bytes, sha256, signature(BASE64 DER), release_notes, visibility('draft'|'released'|'retired'), released_by, released_at, created_at`。
约束:仅 `visibility='released'` 才参与"最新可用"计算;退役 = 置 `retired`(正在升级的已发布任务不受影响)。

**新表 `device_ota_jobs`**(单设备同时至多一个 active,由应用层保证)
`id, device_id FK, firmware_version_id FK, force(bool), requested_by(user_id|'script:<who>'), status('requested'|'notified'|'downloading'|'installing'|'rebooting'|'succeeded'|'failed'|'rolled_back'|'superseded'), progress_pct, status_detail, requested_at, updated_at, finished_at`。

**`devices` 加列**
`current_firmware_version, current_firmware_sha, last_firmware_report_at`(设备每次检查/上报更新;用于 App 展示与按版本统计)。

**唯一 active 任务语义**:`requested` 触发后,若设备在任务未终态时再次 check 命中,保持同任务推进;新触发被忽略(`409`,App 显示"正在升级")。

### 2.2 服务端:检查端点扩展(`POST /v1/device/ota`)

保持现有无鉴权头识别 + `websocket` 返回不变(兼容旧固件)。新增:
1. **版本上报**:解析新头 `Firmware-Version`(语义版)与 `Firmware-Git`(git sha,可选)——固件 `SetupHttp` 增发 → 写 `devices.current_firmware_version/sha/last_firmware_report_at`。旧固件无此头 → 列保持空。
2. **firmware 下发**(判定只认"有 active job",不做与设备端重复的双重比较):查该设备是否存在**待推进态** active job;存在则用**本次请求上报的 current**(非 DB 过期列)快速短路:job.force 为真,或上报 current < job 目标版本 → 返回 `firmware:{version, url:<S3 presigned 30min>, sha256, signature, force}` 并置 `job.status='notified'`;若上报 current 已 ≥ 目标且非 force → **不下发**:该 job 已被设备执行过(notified 及之后状态)则置 `succeeded(confirmed_by_check)`(设备的 d2c `succeeded` 上报走 MQTT→webhook,通常慢于本次直连 HTTP,已输掉竞态),否则(仍 `requested`,从未开始)置 `superseded(already_on_target)`;无 job → 不含 firmware 段(设备行为退化为今日)。真实"装不装"仍以设备端 `IsNewVersionAvailable` 为准,服务端只做同一次请求内的快速短路。
3. **server_time**:顺带返回 `server_time{timestamp,timezone_offset}` 供设备校到墙钟(TLS 证书日期窗口用;**S3 presigned GET 不依赖设备时钟**——SigV4 的日期/签名由服务端现签内嵌在 URL,设备只 GET 不重签,见 §5)。

签名 URL:api 服务角色需 `s3:GetObject` 于新桶(现有 `pigugu-s3-sa` 权限模型扩展),按请求现签,短时有效。

### 2.3 服务端:App/内部 API + d2c 上报 + 推送

端点一律走设备动作的**单数前缀 `/v1/device/{device_id}/…`**(与 `set-active`/`connectivity-check` 同 router;用户列表 `GET /v1/devices` 保持复数不动)。实现时以 router 实际挂载为准,若不符回改本文件两处路径定义。

- `GET /v1/device/{device_id}/firmware`(App 鉴权 + 归属校验):返回 `{current_version, update:{target_version, release_notes, published_at, force, status, progress_pct}}`;`update` 为 `null` = 已是最新。`status` 为 active job 状态,或(无 active job 且目标仍是当前最新时)最近一次 `failed/rolled_back` 终态,供 App 升级页显示失败/重试(实测修正,替代文档早期 `update.available` 草稿)。
- `POST /v1/device/{device_id}/firmware/upgrade`(请求体 `{firmware_version_id|'latest'}`):校验归属 + 版本 released → 建 job(`requested`)→ publish c2d `{msg_type:'ota.check'}`(与现网 `connectivity.ping` 同字段习惯,非 `type`)→ 返回 job。
  - **离线预约**:设备不在线时同样建 `requested` 任务(c2d 无应答即可);设备下次上电/联网 check 命中即被服务。App 侧以"预约"呈现(PRD §5.4)。
- 内部(测试/回滚):同 upgrade 端点加 `force=true` 与超管校验;**允许 target 指向 draft 版本**(测试机自测用),对外/App 仅 released。同一端点承载"强制到旧版本"(回滚)与同版重装。
- **解绑/删除清理**:扩展既有 `DELETE /v1/device/{device_id}`——删除设备时把其待推进态 job 置 `superseded`(历史保留),避免残留任务悬置。
- d2c webhook(iot.py:424-509)新增 `ota.report`:`{state, version?, progress?}` → 按 state 推 `device_ota_jobs.status/progress`(downloading/installing/rebooting/succeeded/failed/rolled_back)并同步 `devices.current_firmware_version`。沿用设备现有 d2c topic + Topic Rule,不新增常连通道。进度取 ≥10% 档上报,控 MQTT 频次。
- **服务端总超时**:自 job **最后一次状态/进度更新**起 `OTA_JOB_TIMEOUT_SECS`(默认 300)无推进 → 置 `failed(timeout)`(慢下载因 10% 档进度持续刷新而不会被误杀)。App 不自摆计时器,只渲染服务端状态。
- **推送回执**:job 到终态(`succeeded/failed/rolled_back`)时,由 api push 模块推给**该设备绑定用户注册的 App token**(token 经 `/device/fcm-token` 上报;同一手机多设备共用一个 token,首版单绑)。冷启动可达;App 站内同源渲染。
- 版本管理(上传/列表/退役):`api` 内新增内部端点(供脚本);admin 页面属 Phase2(独立仓 pigugu-admin)。

App 端行为映射与文案详见 PRD §5.4(状态机/五段升级页/重启盲窗/离场恢复),不在本节重复。

### 2.4 固件:开启检查 + 上报 + 升级 gating + 状态上报 + 验签

1. **ota_url 收口**:默认 `CONFIG_OTA_URL` 改指 pigugu(`https://api.pigugu.net/v1/device/ota`)。**fail-closed**:`CheckNewVersion()` 开跑前校验 URL host 属于 pigugu 白名单,否则跳过(防误连第三方公共 ota 拉错固件,见 2.5)。
2. **去掉跳过**:`CheckNewVersion()` 不再因 `use_cust_mqtt` 而整体跳过;改为**总是执行版本检查**,但消费端只取 `firmware` 与 `server_time`;`websocket`/`mqtt` 段继续受 customer-provisioning 语义约束(现有 ota.cc:151-194 已按 `use_cust_mqtt` 决定是否持久化 mqtt;WS 配置以 provisioning 为准,检查返回的同 URL 写入幂等无害——实现时核对当前 WS 配置真实来源,若来自 provisioning 则此处忽略 websocket 段)。
3. **触发节奏**:上电联网后一次(现有位置)+ 收到 c2d `{msg_type:'ota.check'}` 立即一次 + idle 期周期兜底(新 Kconfig `CONFIG_OTA_CHECK_PERIOD_H` 默认 6,0=关;低功耗态无 WiFi 则仅在有网窗口生效)。
4. **升级编排 gating**:命中新版本不立即升级,先进 `defer_upgrade` 标志;**仅当状态 Idle(无会话/无 TTS)** 才启动(复用 application.cc 状态机)。启动后播提示音、屏显进度(现有 `UpgradeFirmware` 编排 application.cc:1423 + progress 回调)。成功 → `esp_restart`;中途失败 → 继续跑旧版、d2c 报 `failed`。
5. **重启自检 + 确认**:新槽位启动后现有逻辑在联网+服务可达后 `MarkCurrentVersionValid()`(即自检确认点);崩溃/超时未确认 → IDF rollback 自动回旧槽(已启用)。确认后再 d2c 报 `succeeded`。
6. **验签**:OTA 响应携带 `sha256`+`signature`;下载流边写边累计 sha256,完成后:sha 不符 或 ECDSA-P256 验签(内置公钥,mbedtls)不过 → `esp_ota_abort`、不 set_boot、d2c 报 `failed`。内置公钥为编译时常量(脚本生成 PEM→头文件),不烧 eFuse。
7. **版本上报头**:`SetupHttp()` 增发 `Firmware-Version`(= `esp_app_get_description()->version`)、`Firmware-Git`(= git_info)。

### 2.5 构建/发布产物与版本注入

- **App 镜像(OTA 用)** = esp-idf 产出的 app 分区镜像(如 `build/xiaozhi.bin`),**非** USB 烧录的合并包(合并包只用于本节"一次性引导"的线下刷机)。
- **版本注入**:根 `CMakeLists.txt` 支持 `-DPIGUGU_RELEASE_VERSION=<semver>`(也接受同名环境变量)覆盖 `PROJECT_VER`;**必须用 `-D`**——只有缓存项变化才会触发 CMake 重配置,纯环境变量改动不会,复用的 `build/` 会沿用上一次的版本号。发布脚本以发布语义版(首版 **2.3.0**,须 > 现存 2.2.4)构建,同时记录 git sha。
- **发布脚本**(固件仓 `scripts/release_ota.py`,参照既有 release.py):板型参数化构建 → 计算 sha256 → 私钥 ECDSA-P256 签名(openssl,私钥只存构建机/CI,不入库)并**用固件内置公钥回验** → 仅只读预检版本注册表(同版本异字节/比已发布版本更旧则拒绝)→ 上传 S3 `fw/{board}/{version}/{git}.bin`(已有同 key 且字节不同的对象则拒绝覆盖;字节相同则跳过,幂等)→ 调 api 建 `firmware_versions`(visibility=draft)→ 定向 force 测试 → 置 released。
- **一次性引导**:OTA 使能版(2.3.0)以 USB 5 分区布局刷入存量设备一次(现有 flash SOP);此后升级全走 OTA。

### 2.6 App(Flutter)

- **信息源单一**:升级过程只渲染 `device_ota_jobs.status/progress_pct`(经 `GET /v1/device/{device_id}/firmware`);App 端无独立升级计时器。**前台**升级页按 ~3s 轮询;**切后台/杀进程即停轮询**(OS 限制),结果由 FCM 推送 + 下次打开同步,终态感知 ≤5s(前台)。
- 页面/状态:设备卡片固件摘要 → 有可用更新横幅(版本/说明/[立即升级])→ 升级页五段(连接/下载%/重启盲窗不定进度/自检/完成);设备离线时"立即升级"降级为"预约升级"。文案与动画逐字稿见 PRD §5.4,此处不重复。
- **重启盲窗**:设备写入→重启→回连间会断 MQTT/网络数十秒,期间收不到 `ota.report`;App 以"设备重启中"不定进度容纳,不收进度不判失败,仅显式终态才离开升级页。
- **离场恢复**:冷启动/回前台 → 拉真实 job 状态续上对应界面;不会停在假的"升级中"。退出/杀进程不影响升级(服务端状态 + 设备执行)。
- 回执:`firebase_messaging` + `flutter_local_notifications` 收 api 推送的完成/失败;首次触发升级前引导授权系统通知(否则完成推送收不到,站内兜底)。
- 已接入基础(App 现况):`firebase_messaging`、`flutter_local_notifications`(pubspec.yaml)、`fcm_service.dart`、服务端 `/device/fcm-token`。
- **App 落地的文件级方案/状态映射/文案 key 见 `ota-app-design.md`**(以 pigugu-app main@15a8f94 为基线);本节约束它、不重复。

## 3. Testing and validation

按 PRD §5 不变式映射(单元/集成/真机三段)。

**服务端(api 仓,pytest,stub boto3/S3)**
- 上报:check 带/不带 `Firmware-Version` → `devices.current_firmware_version` 更新/留空。
- 下发判定:存在待推进 active job 且(force,或**本次请求上报 current < 目标**)→ 返回 firmware{url:presigned,sha,sig} 且 job→notified;无 job → 无 firmware 段;上报 current ≥ 目标且非 force → 不下发,已下发给设备的 job 置 `succeeded(confirmed_by_check)`,未开始(仍 requested)的置 `superseded(already_on_target)`。
- 任务幂等:active 未终态再触发 → 409。
- webhook `ota.report` 全状态迁移合法化(乱序/重复不炸);succeeded/rolled_back → 同步 devices 版本。
- 归属校验:非本人设备 upgrade 拒绝;内部 upgrade 允许 draft target + force。
- 离线预约:设备不在线建 `requested` 任务 → 下次 check 命中即服务(不依赖 c2d 应答)。
- 总超时:自最后一次状态/进度更新起 `OTA_JOB_TIMEOUT_SECS`(默认 300)无推进 → `failed(timeout)`;终态时触发一次推送(可 mock FCM)。
- 解绑/删除设备 → 其待推进 job 置 `superseded`。

**固件(host 测试,扩 tests/ 既有 harness)**
- 决策纯逻辑:Idle/会话中的升级 defer 决策、版本比较 + force 语义、URL host 白名单 fail-closed。
- sha256 累计 + 验签:好包通过、篡改包/错签拒绝(用测试密钥在 host 侧跑同一段 mbedtls/openssl 逻辑)。

**真机回归清单**(PRD §6 前提 + 防语音回归)
1. USB 刷 OTA 使能 2.3.0 → 服务端 `devices.current_firmware_version` 出现 2.3.0;
2. 发 2.3.1 → App 见可用更新;点升级 → idle 提示音 → 进度 → 重启 → 上报成功 → App"已是最新";版本列更新;全程对话/唤醒行为无回归;
3. 会话中点升级 → 不打断,当前轮结束后才执行;
4. 下载中途断 WiFi → failed、旧版继续跑、可重试;
5. force 定向回 2.3.0 → 成功(降级路径);
6. 篡改 S3 对象(改字节)→ 设备验签拒绝、保持旧版、日志可见原因;
7. 模拟新固件启动即崩(不确认)→ bootloader 自动回滚旧槽;
8. 离线设备置目标 → 下次上电联网自动补上;
9. presigned 过期(改设备时钟超前)→ 重试重新 check 拿到新 URL 成功(时钟由 server_time 校正);
10. App 链路:点升级 → 设备重启盲窗期间 App 显示"重启中"不误判失败 → 升级中杀 App → 完成后收 FCM 推送;重开 App 状态续接;通知未授权仍可站内看到终态。

## 4. Parallelization

设计审批后,三个仓库可按**先锁接口契约**再并行:
- **契约清单**(先定):版本上报头名(`Firmware-Version`/`Firmware-Git`)、OTA 响应 firmware JSON 字段、d2c `ota.report` payload、REST 路由与 body。
- **并行组**:A=api(模型+端点+webhook+presigned,server 仓),B=固件(开检/gating/验签/上报,固件仓),C=App(状态页+触发)。三者互不依赖同一 checkout,可各用独立分支(server `feat/ota-server`、firmware `feat/ota-device`、app `feat/ota`)并行开发。
- **串行依赖**:发布脚本 `release_ota.py` 依赖 A 的内部上传 API 与固件产物约定 → C/A 之后;真机端到端验证必须 B+A 合并联调后执行(第 3 节清单)。集成测试前先打桩对齐契约。

## 5. Risks and mitigations

| 风险 | 影响 | 缓解 |
|------|------|------|
| ota_url 误指第三方公共站导致拉错固件 | 设备被刷入无关 xiaozhi 固件 | 默认值改 pigugu + URL host 白名单 fail-closed;未过白名单一律跳过检查 |
| idle 期设备未保活 WiFi/MQTT | c2d 催检收不到,升级无法秒级启动 | 真机确认待机保活(见 PRD §5.2 前提说明);不可达则接受退化为上电检查 + idle 周期轮询 |
| 设备时钟偏差 | TLS 证书日期窗口校验失败(SigV4 **不受影响**:presigned 的日期/签名由服务端现签内嵌,设备只 GET 不重签) | server_time 校到墙钟即可;仅在证书临近有效期时注意 |
| 签名私钥丢失 | 无法发布 | 私钥离线多副本;发布机/CI 保管 |
| 把"一直被跳过"的 OTA 路径首次点亮引入回归 | 意外行为/语音回归 | 本版消费端只取 firmware+server_time;WS/MQTT 仍以 provisioning 权威;真机清单全量回归 |
| 下载中断电/断网、写入损坏 | 卡在中间态 | 未 set_boot 前继续旧版可重试;esp_ota_end 校验;新槽启动自检失败由 rollback 兜底 |
| 首次 OTA 使能需 USB 一次 | 存量设备要线下 | 明确 2.3.0 为引导版,排期一次性刷机;此后再无 USB |
| 升级不兼容当前服务端(协议漂移) | 设备升级后连不上 | 版本说明约束 + 先测后发;force 可救;真机清单含会话回归 |
| 升级中重复触发 / App 并发 | 双任务 | 单设备单 active job,409 |
| 固件端验签公钥轮换 | 换钥需先于旧钥作废同步 | 公钥内置编译期常量;轮换时先发"含新公钥"的版本再启用新私钥(列入 Follow-ups) |

## 6. Follow-ups

- AWS IoT Jobs / 后台批量灰度 UI(pigugu-admin,按设备组/百分比 + 暂停续跑);
- 自动更新(无人点也推,带静默窗口);
- CDN/CloudFront 承载下载、断点续传(Range)、delta(bsdiff)压缩;
- 固件 telemetry 与 CH metrics 按 `firmware_version` 维度 join(配合卡顿归因);
- 多板型发布矩阵;量产前做 Secure Boot + anti-rollback(eFuse,一次性决策)评审。
