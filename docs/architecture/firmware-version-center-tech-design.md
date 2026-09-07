# App 固件版本中心 — 技术设计(TECH.md)

> 配套产品文档:[`../product-design/firmware-version-center-prd.md`](../product-design/firmware-version-center-prd.md)。
> 本文档版本锚点:服务端 `api` 侧基于 `pigugu-server@9baad2e`(OTA 核心已合入 feat/ota-server)、App 侧基于 `pigugu-app@1346063`(feat/ota-upgrade-ui)。行为不变式与文案见 PRD §5,不重复。
> 本功能**几乎全在 App**,服务端仅一处**可选扩展**(§3)。OTA 的 job/升级机制完全复用,不新增后端能力。

## 1. Context(现状锚点)

- **入口现状**:「我的」ProfileScreen 是 ListView 列表式,已有「设置」分组与行组件 `_buildListRow`(`lib/features/profile/presentation/profile_screen.dart`);**无固件入口**。设备固件更新的唯一入口 = 设备页横幅 → `FirmwareUpgradeScreen`(OTA 轮,feat/ota-upgrade-ui)。
- **数据现状(已具备,可直接复用)**:
  - `GET /devices` 每项带 `firmware` 摘要 `DeviceFirmwareSummary{current_version, update_available, target_version, status, progress_pct}`(服务端 device 列表 join 当前 job,`service.get_devices_for_user`;DTO `provisioning_dto.dart`)。
  - `GET /v1/device/{id}/firmware` 返回 `DeviceFirmwareDetail{current_version, update:{target_version, release_notes, published_at, force, status, progress_pct}}`(含最近 failed/rolled_back 终态;App DTO 已建)。
  - `POST /v1/device/{id}/firmware/upgrade` 触发/预约(App API 方法已建)。
  - App 前台轮询 provider `device_firmware_provider.dart`(3s、终态停、无自摆超时)+ 五段升级页 `firmware_upgrade_screen.dart`。
- **缺口**:没有"我的 → 固件版本"常驻入口与列表页;用户无法主动查看/检查。

## 2. 总体与边界

- **职责**:本页是**查看 + 触发 + 复用升级页**的薄壳;状态真相仍是服务端 job。不复制状态机、不新增后端表/端点(除 §3 可选字段)。
- 单设备/多设备都走同一列表逻辑;升级仍是单设备触发。

## 3. 服务端:一处可选扩展(列表内联更新说明)

「有可用更新」行的说明摘要需要 `release_notes`。两个方案:

- **A(推荐,改动最小)**:行内摘要只在进入页时对有更新的设备**逐台** `GET /firmware`(设备量个位数,延迟可接受),列表接口不动 → 服务端**零改动**。
- **B(避免 N+1)**:给 `GET /devices` 的 `firmware` 摘要扩展 `release_notes`(列表按行内版本查 release_notes)。服务端 `build_firmware_summary` 加一段查询,App DTO 加字段。适合将来设备变多。

> **建议首版走 A**;B 记 Follow-up(设备量上来或需要"整页一次出全说明"时再做)。

## 4. 接口契约(全部复用已有,无新端点)

| 用途 | 端点 | 返回(DTO 已有) |
|---|---|---|
| 页面初始(每台:当前版本/有无更新/活动进度) | `GET /devices` | `DeviceResponse.firmware`(摘要) |
| 行内"检查更新"、查看说明摘要 | `GET /v1/device/{id}/firmware` | `DeviceFirmwareDetail` |
| 立即升级 / 预约升级 | `POST /v1/device/{id}/firmware/upgrade` | 201(忽略 body)→ 随轮询/重拉更新 |

- 升级中行要**实时进度**:该设备进入活动态时启动既有 `device_firmware_provider(deviceId)` 3s 轮询;终态/离开页停表。
- App **无独立超时**;失败/超时以服务端 job 终态为准(与升级页同口径)。

## 5. App 实现

### 5.1 新增文件与改动(均在 `pigugu-app`)

- `lib/features/device/presentation/firmware_version_center_screen.dart`(新):版本中心页(§PRD 5.2/5.3)。
- `lib/features/profile/presentation/profile_screen.dart`(改):「设置」分组新增「固件版本」行(`_buildListRow` 已带 `onTap`,复用不改其签名),点击 `Navigator.push` 到版本中心页;返回后刷新 `deviceListProvider`(版本可能已变)。
- **入口解耦**:Profile 只 `import` 版本中心页的**入口类**并 push,**不**反向依赖 device 的 provider/状态;device 侧业务(取数/升级)全部封装在版本中心页与其 provider 内。返回刷新通过页面 `pop` 回传是否需要刷新,而非 Profile 直接 watch device provider。
- 复用:`DeviceResponse.firmware`/`DeviceFirmwareDetail`、`provisioningApiProvider`、`deviceListProvider`、`device_firmware_provider`、`FirmwareUpgradeScreen`。不新增 API 方法。

### 5.2 页面与行状态(实现 PRD §5.2 表)

- 页面 watch `deviceListProvider`(设备集合/在线态/活动概要);进入页先 `fetchDevices()` 刷新一次。
- **初始水合(列表缺终态与说明)**:`GET /devices` 摘要不含 failed/rolled_back 终态与 release_notes。进页后对 `update_available=true` 的设备各补一次 `getDeviceFirmware(device.id)`,用返回的 detail **覆盖该行状态与说明**(使"上次失败/已回滚"与新版说明首屏即达)。设备量个位数,无并发批处理问题。
- **单真源仲裁**:行级以**最近一次 `GET /firmware` detail 为准**渲染;列表摘要只用于"有哪些设备、在线与否、有无活动任务"与页初始占位。避免两真源瞬时不一致(如刚失败但列表仍显示"可升级")。
- 每台设备一行,行内按 detail/summary 渲染;`status`/`progress` 语义:

| status | 行 UI | 交互 |
|---|---|---|
| —(无任务) 且 `update_available=false` | 已是最新(Vx) | 检查更新 |
| — 且 `update_available=true` | 高亮新版 Vx(说明取自水合 detail) | 立即升级(在线)/ 预约升级(离线) |
| requested/notified/downloading/installing/rebooting(`isActiveJob`) | 进度条 + 阶段 | 点击进 `FirmwareUpgradeScreen` |
| (detail 透出)failed/rolled_back | 失败/已回滚 + 重试 | 重试 = 重新 upgrade |

- 行内「检查更新」= `getDeviceFirmware(device.id)` → 刷新该行 detail(说明/状态);短暂 loading;失败行内提示可重试(10s 超时给失败)。
- 升级动作复用:在线 `FirmwareUpgradeScreen`(进入即自动触发,已在页内实现);离线进升级页 → reserve 分支「预约升级」。
- 活动态行的实时进度:进入升级页后由 `device_firmware_provider(deviceId)` 的 3s 轮询承担;本页不自行开新轮询。

### 5.3 i18n

- ProfileScreen 已走 l10n → 新增行文案与页面文案加 arb key(`app_en.arb`/`app_zh.arb`,`flutter gen-l10n`),与 Profile 一致;与升级页内部硬编码 zh 的口径差异在实现时统一(升级页文案保持不动)。

### 5.4 状态映射/轮询复用

- 页面级:列表来自 `deviceListProvider`;活动态行的实时进度用 `device_firmware_provider(deviceId)` 子页拉(进入升级页即自然切换)。页面自身不做新轮询循环。

## 6. 测试与验证

- 单测:DTO 已覆盖;新增 summary→行状态派生(纯函数)case(活动集/无任务/失败透出)。
- widget:版本中心页行状态渲染(已最新/可升级在线/离线预约/升级中/失败)、点击跳升级页、检查更新 loading→结果。
- 手动(依赖真机/已部署服务端,同 OTA 回归):
  1. 最新设备 → 页显"已是最新",检查更新仍最新;
  2. 发布新版 → 页行高亮 + 说明;立即升级 → 五段升级页完成;
  3. 设备离线 → 预约升级 → 上线自动补升 + 推送;
  4. 升级中进出本页 → 进度延续/恢复;
  5. 多设备 → 各行独立、操作只作用目标设备。

## 7. 依赖与顺序

- 前置(已提交):OTA api 端点 + `GET /devices` 摘要 + App `device_firmware` 基础设施(feat/ota-server、feat/ota-upgrade-ui 两个 commit)。
- 分支:`pigugu-app` 在 `feat/ota-upgrade-ui` 之上续作(或新分支 `feat/fw-version-center`);服务端如走 §3-B 则单独小 commit。
- 与主 OTA 的 PR:本功能独立 PR,不含 OTA 核心改动。

## 8. 风险与 Follow-ups

| 项 | 说明/缓解 |
|---|---|
| 列表无终态/无说明,首屏取不到失败态 | 初始水合:对可升级设备进页即 `GET /firmware`,以 detail 覆盖行(§5.2);设备量小可接受 |
| 行状态两真源瞬时不一致 | 仲裁已定:行级以最近 detail 为准,列表仅作设备集合/在线/活动(§5.2) |
| 未 OTA 使能旧固件判定 | 服务端无判别信号;本版走兜底(按"可升级候选"展示、不响应则超时/重试),识别信号留 PRD §10 待定 |
| Profile 跨 feature 依赖 device 页面 | 已限定:Profile 只 import 版本中心页入口类并 push,不 watch device provider(§5.1) |
| 升级中行进度需"离开页仍继续" | 升级本就后台执行(服务端为真相);本页仅渲染,离开即停前台轮询,回执靠 FCM + 重开同步 |
| 多设备并发检查更新 | 设备量小、用户手动触发;不并发批处理;行内各自 loading |
| release_notes 为空 | 行内说明回退为只显版本号(PRD 待定 1) |
| 列表接口扩展 release_notes(§3-B) | 设备量增长或"整页一次性说明"需求出现时再做 |

**Follow-ups**:列表摘要带 release_notes(B);"最近检查/上次升级时间"展示(依赖服务端字段);多设备排序策略;版本目录浏览(需新 API,另行需求)。
