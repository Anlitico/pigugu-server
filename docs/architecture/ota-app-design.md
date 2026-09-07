# 固件 OTA — App 侧实现方案(App 仓)

> 范围:**pigugu-app**(Flutter),基线 `main@15a8f94`。产品行为见 `ota-prd.md §5.4`,服务端契约/后端见 `ota-tech-design.md`,本文档只讲 App 怎么落地。标注 **[依赖]** 的项需服务端 OTA 端点先就位。
>
> **视觉(2026-09-07 暂定)**:交互原型 [`docs/product-design/ota-upgrade-prototype.html`](../product-design/ota-upgrade-prototype.html)(入口 A/B/C × 卡内状态 + 升级页五段/盲窗/异常)。tokens 对齐 App 主题:**Indigo `#6366F1→#4F46E5`**、浅 Slate 底 `#F8FAFC/#F1F5F9`、白卡 `#E2E8F0` 描边、文字 Slate(`#0F172A/#475569/#94A3B8`)、语义 **Emerald `#10B981` / Amber `#F59E0B` / Red `#EF4444`**;版本号/序列号用 JetBrains Mono。Flutter 侧按此 tokens 建常量平移。

## 1. Context(现状锚点,main@15a8f94)

- **设备只有一个展示面**:`DeviceScreen`(4-tab 的 tab0,`lib/features/main/presentation/main_screen.dart:96-101`)→ 空态/单卡 `_buildFullDeviceCard`(`device_screen.dart:106,217-245`)→ `PiguguIdCard`(`lib/features/device/presentation/widgets/pigugu_id_card.dart:6-23`);无整卡点击、无设备详情页。动作区在 `pigugu_id_card_sections.dart:318-359`(Wake Up / Set Active / Re-pair / Unbind),`buildInfoGrid` 渲染名字/mac/class/**ONLINE·OFFLINE**(`:215-280`)。在线态来自列表。
- **数据源**:`deviceListProvider`(`device_providers.dart:56`)→ `GET /devices`(在 `provisioning_api.dart:50-52`);`DeviceResponse{id,hardwareId,deviceName,isOnline,...}`(`provisioning_dto.dart:66-88`)。**无 firmware/版本字段**。设备身份 = `DeviceResponse.id`(服务端 UUID),由它调设备动作接口。
- **API 层**:`ProvisioningApi`(Dio,base `/v1`;`AppConfig.apiBaseUrl=api.pigugu.net/v1`,api_client.dart:17-19)。设备动作端点全部在**单数 `/device/{deviceId}/...`** 前缀:`set-active`(:75)/`connectivity-check`(:81)/`unbind`(:88);token 相关 `/device/fcm-token`(:92)。列表是复数 `/devices`。
- **FCM**:`fcm_service.dart`,channel `pigugu_default`;token 随 `onAuthenticated()` 自动上报(`/device/fcm-token`,fcm_service.dart:209-224)。前台 `onMessage`→本地通知(:190-194);后台/冷启动 handler 只 log(**无点击路由**,:197-205)。`subscribeToDevice/unsubscribeFromDevice`(:228-238)存在但从未被调用。
- **导航/状态**:go_router 只注册 `/`,`/login`,`/register`(app_router.dart:56-69);业务页普遍 `Navigator.push(MaterialPageRoute)`。Riverpod 2;`AutoDisposeNotifier/StateNotifierProvider` 模式;全仓**无 REST 轮询先例**(最近的轮询/推送是 provisioning WS 与 roast WS 事件)。l10n:模板 `app_en.arb` + 翻译 `app_zh.arb`,`flutter generate: true`;用法 `AppLocalizations.of(context)!.key`。部分 device 页用硬编码中文绕过 l10n。

## 2. 接口契约(服务端,待实现,与后端技术文档对齐)

端点放进**服务端 device router(与 set-active/connectivity-check 同前缀)**:

| 方法 | 路径 | 用途 | 返回 |
|---|---|---|---|
| GET | `/device/{deviceId}/firmware` | 升级页详情 | `DeviceFirmwareDetail` |
| POST | `/device/{deviceId}/firmware/upgrade` | 触发升级(body `{firmware_version_id?}` 缺省=最新) | job(`status:'requested'`) |

- **[依赖] 列表瘦身**:`GET /devices` 每项带可选 `firmware:{current_version, update_available, target_version, status, progress_pct}`(服务端 join job,单 poll 源,设备卡直接可显示"有更新")。字段缺失/为 null = 无可用更新(向后兼容)。
- DTO(与上面字段一一对应):`DeviceFirmwareSummary`(卡)、`DeviceFirmwareDetail = Summary + release_notes, published_at, force`。
- 状态机与 `device_ota_jobs.status` 对应:`requested/notified/downloading/installing/rebooting/succeeded/failed/rolled_back`(superseded 视为无任务)。

## 3. 数据层(新增)

- `lib/features/device/data/device_firmware_dto.dart`(新):上述两 DTO + `fromJson`,含 `phase` 派生。
- `lib/features/device/data/device_api.dart`(新,或并入 provisioning_api——建议单独以 feature 边界):
  `getFirmware(id)`、`upgradeFirmware(id, {versionId})`。复用 `AuthInterceptor`,base 同 `/v1`。
- `lib/features/device/providers/device_firmware_provider.dart`(新):
  `AutoDisposeNotifier<DeviceFirmwareJob>` 持有 **唯一真相 = 服务端 job 状态**;`Timer.periodic(3s)` 仅当 status 为活动态(requested→rebooting)轮询 `getFirmware`,终态(succeeded/failed/rolled_back)或 superseded 即停表;`dispose()` 取消表。`deviceListProvider` 完成/失败后 `fetchDevices()` 刷新卡的 current_version。**App 无独立超时**(服务端 `OTA_JOB_TIMEOUT_SECS` 判定)。**轮询仅在前台运行**:升级页离屏/App 退后台即停表(OS 限制),结果由 FCM 推送 + 下次打开时重新拉取同步。

## 4. UI

### 4.1 设备卡固件区(新增 `PiguguFirmwareSection`,`pigugu_id_card_sections.dart` 动作区上方挂入)

| 卡状态(firmware.summary) | 呈现 |
|---|---|
| `update_available=true` 且设备在线 | 高亮横幅:版本号+短说明 + `[立即升级] [稍后]` |
| `update_available=true` 但设备离线 | 降级按钮 `[预约升级]`,文案"设备下次联网后自动升级" |
| job 活动态 | 细进度条 + "正在升级 xx%"(可点回升级页) |
| 无可用 / succeeded | 不展示(或收起副标"已是最新 Vx") |

入口:升级页由卡内按钮/进度条 `Navigator.push(FirmwareUpgradeScreen(deviceId))` 打开;卡片整卡不做跳转。

### 4.2 升级页 `lib/features/device/presentation/firmware_upgrade_screen.dart`(新,全屏)

- 数据:watch `deviceFirmwareProvider(deviceId)`;进入即触发 `startUpgrade`(若已 activity 则直接进状态)。
- 五段动画与文案(**以 PRD §5.4 为准**,这里实现):①连接设备(不定转圈)②下载(Linear 进度,每 10% 档)③安装/重启(**不定进度 + 强调卡**:"设备会重启一次,期间会短暂断网,属正常;请保持设备通电")④自检(不定转圈)⑤完成(✓ + 新版本)。
- **重启盲窗**:rebooting 段收不到进度不判失败,不定进度持续到显式终态。
- 失败/回滚态:原因 + `[重试]`(重新触发)+ 关闭回卡。
- 预约态(设备离线且 `requested`):提示"已预约,设备下次联网后自动升级",可关闭。
- ①②顶部常驻引导条:**"请保持网络通畅;即使退出本页,更新也会继续完成,结束后会通知你。"**
- 顶部物理返回/关闭可用(不打断升级;仅退回卡片,状态由服务端推进 + 推送回执)。

### 4.3 FCM 回执路由(小改)

- 服务端 job 终态推送 payload 约定:`{type:'firmware', device_id, status, version}`(见后端技术文档 §2.3)。
- 前台 `onMessage` 已能弹本地通知;补 **tap→前台设备卡**(`DeviceScreen` 是 tab0):通知点击/`onMessageOpenedApp`/`getInitialMessage` 统一走到 `switchToDeviceTab`(main_screen 暴露)或重开 App 后 `deviceFirmwareProvider` 自愈(唯一真相在服务端,天然一致)。
- **[P1 可选]** `subscribeToDevice/unsubscribeFromDevice` 若未来做设备级推送再启用;本版不引入。

## 5. job.status → UI phase 映射(实现此表,别在 App 复制状态机)

| job.status | phase | UI |
|---|---|---|
| —(无任务/已最新) | none | 卡不展示 |
| requested(设备在线) | connecting | ① 正在连接设备 |
| requested(设备离线) | reserved | 预约态提示 |
| notified/downloading | downloading | ② 进度条(progress_pct) |
| installing | installing | ③ 重启卡(不定进度) |
| rebooting | rebooting | ③ 重启盲窗(不定进度) |
| succeeded | done | ⑤ 完成 ✓(回卡刷新 current_version) |
| failed | failed | ⚠ 原因 + 重试 |
| rolled_back | rolledBack | ⚠ 已回滚 Vx |
| superseded | none | 视为无任务 |

## 6. i18n(新增 arb key,双语文案)

示例(zh):`fwUpdateAvailable`「固件更新可用」、`fwUpgradeNow`「立即升级」、`fwLater`「稍后」、`fwReserve`「预约升级」、`fwReserved`「已预约,设备下次联网后自动升级」、`fwRestartGuide`「设备会重启一次,期间会短暂断网,属正常;请保持设备通电」、`fwKeepGuide`「请保持网络通畅…」、`fwDownloading`「正在下载更新包」、`fwInstalling`「正在安装」、`fwVerifying`「设备正在自检」、`fwDone`「更新完成,已是最新」、`fwFailed`「升级失败」、`fwRolledBack`「已回滚到」、`fwRetry`「重试」、`fwLatest`「已是最新」。**en + zh 两个 arb 都加**,跑 `flutter gen-l10n`。文案定稿另以 PRD §5.4 为准。

## 7. 测试与验证

- 单测:DTO fromJson;`DeviceFirmwareNotifier` 状态映射/轮询启停(Provider override 假 Api,模拟 requested→downloading→rebooting→succeeded 及 failed/rolled_back/离线 requested);盲窗(rebooting 长时间无进度更新不切失败)。
- widget:卡固件横幅 3 态渲染;升级页五段文案切换;重试按钮。
- 手动链路(依赖服务端 + 真机,见后端技术文档 §3):卡见可用更新→升级→盲窗→完成→回卡版本刷新→杀 App 后收 FCM;设备离线预约→下次联网自动完成。

## 8. 依赖与顺序

- 前置:服务端 firmware 端点 + `/devices` firmware 摘要(后端技术文档 §2.2/2.3);FCM token 上报已存在。
- App 可先在 `MOCK_API` 模式对 DTO/状态机自测,再与服务端联调。
- 分支:`pigugu-app` 新分支 `feat/ota-upgrade-ui`(从 main@15a8f94)。

## 9. 待定(实现前与用户确认)

1. 卡横幅视觉形态(色/位置)与"立即升级"是否要二次确认弹窗;
2. FCM 点击跳设备 tab 是否 P1(P0=仅本地通知展示);
3. 升级页是否需要"查看更新说明"(依赖 detail.release_notes 渲染);
4. 重试语义:重新触发新 job 是否允许(服务端唯一 active,需先 supersede/fail)。
