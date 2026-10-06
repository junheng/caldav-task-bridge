# CalDAV Task Bridge

Obsidian Core Vault ↔ CalDAV 双向任务同步桥接服务。

## 目标

将 Obsidian Core Vault 中的任务笔记同步到 CalDAV 服务器（Radicale），在手机日历/任务 App 中原生管理 Obsidian 任务。用户在手机上标记完成、改期、推迟等操作自动写回 vault。

## 架构

```
┌──────────────────┐     VTODO + VEVENT       ┌──────────────┐     CalDAV      ┌──────────────┐
│  Obsidian Vault   │ ──────────────────────▶  │   Radicale    │ ◀─────────────▶ │  手机日历 App  │
│  (任务笔记 .md)    │                           │  CalDAV Store  │                 │  (自带日历/    │
│                    │ ◀────────────────────── │               │                 │   Reminders)  │
└────────┬───────────┘     FNS API 写回        └───────────────┘                 └──────────────┘
         │
         │ FNS 实时同步
         ▼
┌──────────────────┐
│  其他 Obsidian    │
│  设备             │
└──────────────────┘
```

- **Radicale**：CalDAV 服务器，负责存储 VTODO/VEVENT；当前项目只依赖其 CalDAV/WebDAV 接口，部署细节不纳入实现范围
- **推送方向**（Vault → CalDAV）：当前通过 FNS REST 扫描任务并低频 reconciliation；后续接入 FNS WebSocket 事件后改为只处理受影响路径
- **拉取方向**（CalDAV → Vault）：当前使用 WebDAV Sync `sync-token` 增量获取变化，再通过 FNS API 写回笔记 frontmatter；Radicale 变更通知作为后续触发增强

## 任务模型映射

Obsidian 任务是一个独立的 `.md` 文件，元数据存储在 YAML frontmatter 中：

```yaml
---
task_status: 待办          # 待办 | 进行中 | 已完成 | 阻塞
priority: 2                # 1（高）| 2（中）| 3（低），数值型
due_date: 2026-06-10
scheduled_date: 2026-06-05
assignee: "[[龚俊衡]]"
related_project: "[[项目名]]"
---
```

### frontmatter → VTODO

| Obsidian | CalDAV VTODO | 说明 |
|----------|-------------|------|
| `task_status: 待办` | `STATUS:NEEDS-ACTION` | |
| `task_status: 进行中` | `STATUS:IN-PROCESS` | |
| `task_status: 已完成` | `STATUS:COMPLETED` + `COMPLETED: <date>` | |
| `task_status: 阻塞` | `STATUS:NEEDS-ACTION` + `X-OBSIDIAN-TASK-STATUS:BLOCKED` | 阻塞保持任务身份；手机丢弃标记时仍保留本地阻塞状态 |
| `task_status: 已取消` / 有效 `cancelled_date` | 已有远端对象定向退出 | 不新建取消历史，不写完成日期；此为 bridge 兼容能力，不自动修改 Vault 模板规范 |
| `priority: 1` | `PRIORITY:1` | 高优 |
| `priority: 2` | `PRIORITY:5` | 中优 |
| `priority: 3` | `PRIORITY:9` | 低优 |
| `due_date` | `DUE;VALUE=DATE:<date>` | 截止日期 |
| `scheduled_date` | `DTSTART;VALUE=DATE:<date>` | 计划开始日期 |
| `title` (文件名) | `SUMMARY` | 任务标题 |
| `assignee` + `related_project` + 正文摘要 | `DESCRIPTION` | 摘要信息、去掉 frontmatter 后的笔记正文摘要、`obsidian://open` 链接；Obsidian `[[...]]` wikilink 只保留展示名 |
| 笔记链接 | `URL` | 可点击的 `obsidian://open` 链接 |
| 笔记链接 | `ATTACH;VALUE=URI` | 兼容不展示 `URL` 的客户端 |
| `tags` | `CATEGORIES` | 逗号分隔 |
| vault 相对路径 | `X-OBSIDIAN-PATH` | 用于 CalDAV → Vault 写回；UID hash 不能反推出路径 |

### 有截止日期的任务 → 额外生成 VEVENT

有 `due_date` 的任务同时生成一个 VEVENT，用于在日历时间线上可视化：

| VEVENT 字段 | 值 |
|------------|-----|
| `UID` | `event-{md5(path)[:12]}@core-vault` |
| `DTSTART;VALUE=DATE` | `due_date` |
| `DTEND;VALUE=DATE` | `due_date + 1` |
| `SUMMARY` | `📋 {title}`（逾期任务前缀 `🚨`） |
| `DESCRIPTION` | 正文摘要 + `obsidian://open?vault=Core&file={path}` |
| `URL` | 可点击的 `obsidian://open?vault=Core&file={path}` |
| `ATTACH;VALUE=URI` | 兼容不展示 `URL` 的客户端 |
| `STATUS` | `CONFIRMED`（活动任务）；完成、取消、参考资料与归档记录不保留日期投影 |
| `X-OBSIDIAN-PATH` | vault 相对路径 |

VEVENT 的 `DTSTART` 始终等于真实 `due_date`。逾期任务只在 `SUMMARY` 前加 `🚨`，不移动事件日期，避免 pull 把展示日期误写回 Obsidian。

`DESCRIPTION` 按 iCalendar 标准写入文本并由库自动折行；标准没有给 VTODO/VEVENT 描述设定很小的字数上限，但客户端展示和同步性能会有实际限制，因此本项目只写入正文摘要，默认最多 4000 字符。`assignee` 和 `related_project` 中的 Obsidian wikilink 会在 CalDAV 侧显示为普通文本，例如 `[[People/Alice|Alice]]` 显示为 `Alice`。

### CalDAV 存储结构

在 Radicale 中创建两个 collection：

- `/diomgis/tasks/` — VTODO 对象（供 Tasks.org / Reminders.app 使用）
- `/diomgis/core-vault/` — VEVENT 对象（供 Calendar.app 使用）

两个 collection 的 `supported-calendar-component-set` 应分别声明为仅 `VTODO` 和仅 `VEVENT`，与实际内容一致，便于 iOS 区分提醒事项列表和日历。当前线上已按此配置。

### iOS 使用

iOS 可以继续使用同一套 Radicale/FNS 同步架构。在「设置 → App → 提醒事项 → 提醒事项账户 → 添加账户 → 其他 → 添加 CalDAV 账户」中添加手机可访问的 Radicale HTTPS 地址，并启用「提醒事项」；同一账户启用「日历」后，可在日历中查看有截止日期的任务。不要使用仅 NAS 容器网络可访问的 `172.22.*` 地址。

`tasks` 中的 VTODO 用于完成任务、修改优先级和截止日期；`core-vault` 中的 VEVENT 是截止日期的日历投影，移动事件只写回 `due_date`。VEVENT 的 `CANCELLED` 表示日历事件取消，不能据此把任务改成「阻塞」，也不能从日历事件的状态判断任务是否完成。任务状态以 VTODO/Obsidian 为准。CalDAV 的 `PRIORITY:0` 表示未指定优先级，本项目映射为默认中优先级。

这是外部 CalDAV 账户，不会自动复制到 iCloud 提醒事项列表；部分仅 iCloud 支持的提醒事项功能不可用。当前手机操作写回通常等待最多一个 `PULL_INTERVAL`（默认 5 分钟），Obsidian 修改推送通常等待最多一个 `PUSH_INTERVAL`（默认 15 分钟），还需加上手机自身的账户获取间隔。

Apple 配置说明：[在 iPhone 提醒事项中添加或移除账户](https://support.apple.com/guide/iphone/add-or-remove-accounts-iph8739025dd/ios)。

## 生命周期维护与迁移

`lifecycle.py` 是 push、pull、预览共用的判定入口。有效 task_status 兼容历史任务；`type/reference` 覆盖路径提示。缺失／空／未知状态、非法日期、类型和终态冲突进入 REVIEW。`ARCHIVE_PREFIXES` 默认为已经核实的 `04 - Archives/`，可用逗号分隔配置；不匹配正文中的 Archives。

`python main.py --preview` 只向标准输出生成 JSON 报告：不 PATCH FNS，不 PUT/DELETE CalDAV，不保存 state，也不创建状态锁或推进游标。报告联合实际远端对象、旧映射和当前候选；包含来源证据、分类、版本、目标动作及独立的任务／对象计数。源无法读取或归属不明的对象不自动移除。

停止常驻 bridge 后，可以用 `python main.py --apply-preview <报告.json> --backup-dir <新备份目录>` 执行确认的清单。逐笔核对源 fingerprint 和所有远端 ETag，变更则跳过并保留 pending；备份 state、目标 ICS、预览和逐对象回执。`--approve-review <路径>` 只用于人类已明确确认的无任务身份记录，不能豁免非法字段或终态冲突。正常同步不会自动批准 REVIEW。

退出意图存入 state 的 lifecycle.retirements，分别记录 VTODO／VEVENT 进度。404 表示目标已不存在；412、网络失败或部分成功会重读重试。持久化终态阻止重启／版本重扫恢复旧任务。先在 Obsidian 明确恢复有效类型／状态并解决取消、完成证据后，停止常驻 bridge，执行 `python main.py --restore <笔记路径>` 解除退出保护；普通取消勾选不能自动恢复历史终态。

运行与维护共用 state 文件锁，禁止并发写入。FNS 未提供原子的 frontmatter compare-and-swap，因此 pull 在 PATCH 前再次读取源版本；这能发现已发生的并发编辑，但不能完全消除重读到 PATCH 之间的竞态。迁移窗口须隔离 bridge 写入，并避开用户同时编辑相关任务。

验证：`python -m unittest discover -s tests`。`python -m scripts.verify_lifecycle_caldav` 使用独立的临时测试集合和内存源 fixture，在真实 Radicale 上验证往返、412、退出和重扫；不写真实 Vault，结束时移除测试集合。手机显示与刷新仍需实际 iPhone 确认。

回滚：先停止有副作用的循环；保留新退出意图和回执，按备份恢复一致检查点。不得直接重启旧版本以免重新制造历史待办。代码回滚和数据恢复分开处理。

## 同步触发与增量策略

### 调研结论

- CalDAV/WebDAV 标准支持 `DAV:sync-collection` REPORT 和 `DAV:sync-token`，客户端可以保存 collection 的 opaque token，后续只取新增、修改、删除的资源，而不是每次全量拉取。
- Radicale v3 文档显示其文件存储会维护 sync-token 缓存；同时 `[hook]` 支持 `rabbitmq`，可用于事件变更和删除通知。
- Python `caldav` 库已支持按 `sync_token` 拉取 collection 对象；如果服务端不支持或 token 失效，再做一次全量获取。
- FNS 服务同时提供 REST API 和 WebSocket 同步接口；REST API 的 `PATCH /api/note/frontmatter` 适合作为本项目写回 Obsidian 的接口。
- 上游受限的 REST 更新日志接口不再作为本项目依赖；FNS 增量来源统一收敛到 WebSocket。
- FNS WebSocket `/api/user/sync` 的 `NoteSync` 是当前选定的 Vault 增量来源。实测需要在 HTTP upgrade 请求上带 `X-Client: caldav-bridge`，并用原始帧 `Authorization|<token>` 鉴权，不能把 token JSON 字符串化。
- 当前线上 FNS 3.6.1 采用分页下行：`NoteSyncEnd` 只给出 cursor 和计数，非空增量需要发送 `NoteSyncPageAck`（`pageIndex=-1`）请求首页，再在每个非末页的全部明细读完后确认该页。分页控制消息不能计入变更条数；请求 context 必须唯一，避免不同连接的下载缓存互相覆盖。等待业务帧有超时，WebSocket ping/pong 不能无限延长等待。只测试空增量无法验证这一协议。

### 当前决策

1. **不直接写 vault 文件系统**。CalDAV → Vault 的写回只走 FNS API；FNS 写失败时记录错误并等待下次同步重试。
2. **CalDAV 拉取使用 sync-token 增量同步**。`PULL_INTERVAL` 只作为未配置事件触发时的 reconciliation 间隔，不再做每次全量轮询。
3. **Radicale 事件触发作为后续增强**。如果后续接入 RabbitMQ hook，本服务消费通知后立即执行一次 sync-token delta pull；通知本身只作为触发信号，真实差异仍以 CalDAV REPORT 结果为准。
4. **Vault 推送使用 FNS WS `NoteSync` 增量候选发现**。首次运行先通过 `NoteSync` 记录服务端 `lastTime` cursor，再用 `TASK_PATH_KEYWORD` 做一次初始化 path 搜索；后续运行只拉取 cursor 之后的 note 变更。
5. **不做旧更新日志接口或文件扫描退化**。初始化之后，FNS 增量只走 WS `NoteSync`；WS 鉴权、scope、vault 或网络失败时本轮同步失败并等待下次重试。
6. **必须持久化同步状态**：包括 `last_push_timestamp`、FNS WS note sync cursor、待重试 note path、每个 CalDAV collection 的 `sync_token`、UID ↔ vault 相对路径映射、已知 ETag。状态文件默认放在本服务自己的 data 目录，不写入 vault。

## 推送同步（Vault → CalDAV）：`push.py`

### 输入

- FNS 连接信息（REST API + WebSocket）
- Radicale 连接信息（URL、用户名、密码）

### 执行流程

```
1. 取得需要同步的任务笔记
   - 首次运行：先发送空 `NoteSync` 获取服务端 `lastTime`，再通过 FNS REST `searchMode=path` + `TASK_PATH_KEYWORD` 搜索候选路径，逐条读取详情并用 `is_task_note()` 过滤
   - 后续运行：发送 `NoteSync`，只处理本地 cursor 之后的 `NoteSyncModify`、`NoteSyncMtime`、`NoteSyncRename`、`NoteSyncDelete` 消息
   - WS cursor 立即持久化；如果某个 note 暂时读失败，该 path 会进入 `pending_note_changes`，下一轮继续重试，避免 cursor 前进后漏同步

2. 按统一生命周期分类：ACTIVE / COMPLETED / CANCELLED / OUT_OF_SCOPE / REVIEW
   路径仅用于发现候选，正文标签和历史 checkbox 不证明任务身份

3. 对每个活跃任务：
   a. 计算 UID = md5(file_path)[:12]
   b. 构造 VTODO（icalendar Todo 对象）
      - 写入 UID、SUMMARY、STATUS、PRIORITY、DUE、DTSTART、DESCRIPTION、CATEGORIES
      - DESCRIPTION 包含去掉 frontmatter 后的笔记正文摘要；当前摘要上限为 4000 字符，过长会截断；`assignee` / `related_project` 的 wikilink 只保留展示名
      - 写入 URL 和 ATTACH;VALUE=URI，提供任务客户端可直接点击的 Obsidian 链接
      - 写入 X-OBSIDIAN-PATH，确保 pull 能定位原始笔记
   c. 查询 Radicale 中同 UID 对象
      - 存在 → CalDAV PUT 更新
      - 不存在 → CalDAV PUT 新建
   d. 如果 due_date 非空：
      同上对 /diomgis/core-vault/ 的 VEVENT

4. 对已完成任务（task_status == "已完成"）：
   仅更新已存在的 VTODO 为 COMPLETED，保留真实 done_date；定向删除 VEVENT，不全量回填完成历史

5. 对已取消、reference、已核实归档或 deleted: true 的记录：
   持久化退出意图，核对实际 href/UID/来源及最新 ETag 后删除两类对象；保留历史映射与防重建记录
   FNS 删除信号还须由 code=430 的源读取结果确认；源读失败或未知路径不自动删除

6. 更新本地同步状态：UID ↔ path、ETag、last_push_timestamp、FNS WS cursor
7. 输出日志：新增 X 条，更新 Y 条，完成 Z 条，删除 T/E 条
```

### UID 约定

```
VTODO:  task-{md5(vault_relative_path)[:12]}@core-vault
VEVENT: event-{md5(vault_relative_path)[:12]}@core-vault
```

新任务 UID 基于路径。已记录内容 hash、旧源经 FNS 确认为不存在、且关联唯一的重命名沿用旧 UID；未证实或同时编辑导致 hash 无法匹配的重命名进入 REVIEW，不盲目新建重复对象。

## 拉取同步（CalDAV → Vault）：`pull.py`

### 执行流程

```
1. 连接 Radicale，读取 `/diomgis/tasks/` 和 `/diomgis/core-vault/`
2. 每个 collection 优先执行 `DAV:sync-collection` REPORT：
   - 首次运行或 sync-token 丢失：全量读取，并保存返回的 sync-token
   - 后续运行：带上上次 sync-token，只获取变更/删除的 href
   - token 失效：做一次全量重建，再保存新的 sync-token
3. 获取变化对象内容，按以下顺序匹配 vault 笔记路径：
   - 本地状态中的 UID ↔ path 映射
   - CalDAV 对象上的 `X-OBSIDIAN-PATH`
   - DESCRIPTION 中的 `obsidian://open` 链接
4. 对比变更：
   a. STATUS 变为 COMPLETED（手机上点了完成）
      → 对仍为 ACTIVE 的源写 task_status=已完成；已有 done_date 不改，缺失时使用 COMPLETED 日期或当天
   b. DUE 日期变更（手机上拖拽改期）
      → 更新 frontmatter: due_date=<new date>
   c. 新策略 VTODO STATUS=CANCELLED
      → 写 task_status=已取消 和 cancelled_date，不写 done_date；旧 CANCELLED 只有本地明确阻塞时保持阻塞，否则 REVIEW
   d. VEVENT STATUS=CANCELLED
      → 只退出日期投影，保留任务状态及原截止日期
   e. PRIORITY 变更
      → 写回优先级；DESCRIPTION 不作为 frontmatter 的状态依据

5. 写回 vault：
   - 首选：PATCH {FNS_API_URL}/api/note/frontmatter
   - Header: Authorization: {FNS_API_TOKEN}
   - Body: { vault, path, updates, remove? }
   - FNS 返回失败时不写文件系统，记录失败并等待重试

6. 写回前强制 FNS 重读身份和终态；失败对象先持久化 pending_pull，再推进 sync-token
   完成、取消、reference、归档、退出意图和 REVIEW 源不接受旧对象恢复或改期
7. 更新本地 ETag 状态并报告变更、保护与失败数
```

### 冲突处理

简单策略：**手机端明确操作优先，Obsidian reconciliation 收敛**。
- 记录每次 push 的时间戳
- pull 时跳过本服务刚刚 push 造成的回声更新
- 手机端对 `STATUS`、`DUE`、`PRIORITY` 的明确修改通过 FNS 写回 Obsidian
- 如果两边同时改同一字段，先接受 CalDAV 侧变更；下一次 Vault → CalDAV reconciliation 会把 Obsidian 当前值重新推到 Radicale，形成最终收敛

### 最终一致性保证

当前服务通过以下机制保证 CalDAV 与 Obsidian note frontmatter 最终收敛：

1. **pull 优先**：`--once both` 和常驻循环都先执行 CalDAV → Vault pull，再考虑 Vault → CalDAV push。这样手机端刚产生的 CalDAV 变更会先写回 FNS，不会被下一次 push 直接覆盖。
2. **pull 有变更则推迟 push**：如果 pull 发现 CalDAV 有新增、修改、删除或无法匹配的对象，本轮跳过/推迟 push，给 FNS 写回和状态持久化留出一个 reconciliation 周期。
3. **条件写 CalDAV**：push 更新已知对象时携带上次保存的 ETag (`If-Match`)；如果手机端已修改同一对象导致 ETag 变化，Radicale 返回 412 时本服务跳过该对象，等待下一轮 pull 先收敛。
4. **FNS 写回有明确 client**：所有 FNS REST 和 WS 请求都带 `X-Client: caldav-bridge` / `X-Client-Name: caldav-bridge`；部署侧可以在 FNS 日志中直接识别本服务请求。
5. **持久化状态**：`SYNC_STATE_PATH` 保存 FNS WS note cursor、待重试 note path、每个 CalDAV collection 的 sync-token、对象 ETag 和 UID/path 映射。只要该文件持久化，服务重启后仍能继续做增量同步和条件写。
6. **FNS-only 写回**：CalDAV → Vault 只调用 FNS frontmatter API。FNS 不可用时写回失败并重试，不直接改本地 vault 文件，避免绕过 FNS 造成多设备状态分叉。
7. **CalDAV 映射版本化**：当 VTODO/VEVENT 生成规则升级时，服务会自动做一次任务扫描并重写 CalDAV 对象，确保已有任务也获得新的字段，例如可点击的 `URL`。

已知边界：

- 如果手动只运行 `--once push`，服务会按 Obsidian 当前值推送；部署自动化应优先使用 `--once both` 或常驻模式。
- 如果 `SYNC_STATE_PATH` 丢失，服务需要通过一次初始化扫描重建 FNS WS cursor、CalDAV sync-token 和 ETag 状态；这期间无法识别“已知对象被手机端改过”的条件写冲突。

### FNS API 写回

FNS 暴露 REST API（地址配置在 Obsidian 插件设置中）。本项目只通过 FNS API 写回 Obsidian，不做文件系统写入兜底。

```
PATCH {FNS_API_URL}/api/note/frontmatter
Header:
  Authorization: {FNS_API_TOKEN}

Body:
{
  "vault": "Core",
  "path": "Tasks/example.md",
  "updates": {
    "task_status": "已完成",
    "done_date": "2026-06-03"
  },
  "remove": []
}
```

说明：

- `PATCH /api/note/frontmatter` 是当前调研到的最贴合接口，避免读取整篇笔记再重写。
- 标量字段按标量写入，避免把 `due_date`、`priority`、`task_status` 等字段写成 YAML 数组；只有 `tags` 这类真正多值字段才写数组。
- 如果该接口在目标 FNS 版本不可用，视为配置/版本错误；服务记录错误并停止该条写回，不直接修改 vault 文件。
- FNS 变更会由 FNS 服务实时同步到其他 Obsidian 设备。

## 非目标

- 暂不处理 docker-compose、NAS 网络、Radicale 部署方式。
- 暂不实现重命名后的旧 UID 自动清理；依赖后续状态表和 orphan cleanup 改进。
- 暂不直接写 vault 文件系统。

## 配置

环境变量：

| 变量 | 说明 | 示例 |
|------|------|------|
| `RADICALE_URL` | Radicale CalDAV 地址 | `http://radicale:5232` |
| `RADICALE_USER` | CalDAV 用户名 | `diomgis` |
| `RADICALE_PASSWORD` | CalDAV 密码 | |
| `FNS_API_URL` | FNS 服务器地址 | `https://fns.sigmoid.cc:53691` |
| `FNS_API_TOKEN` | FNS API Token | |
| `FNS_VAULT` | FNS/Obsidian vault 名称 | `Core` |
| `FNS_WS_URL` | 可选，FNS WebSocket 地址；不配置时由 `FNS_API_URL` 推导为 `/api/user/sync` | `wss://fns.example.com/api/user/sync` |
| `FNS_CLIENT_TYPE` | 可选，FNS `X-Client` 值；token scope 需允许该 client | `caldav-bridge` |
| `FNS_CLIENT_NAME` | 可选，FNS `X-Client-Name` 值 | `caldav-bridge` |
| `FNS_CLIENT_VERSION` | 可选，FNS `X-Client-Version` 值 | `0.1.8` |
| `FNS_USER_AGENT` | 可选，FNS 请求 User-Agent | `caldav-task-bridge/0.1.8` |
| `TASK_PATH_KEYWORD` | 可选，初始化扫描时用于 FNS path 搜索的关键词 | `Tasks` |
| `SYNC_STATE_PATH` | 可选，本服务同步状态文件路径 | `./data/state.json` |
| `PUSH_INTERVAL` | 可选，Vault → CalDAV reconciliation 间隔（秒） | `900` |
| `PULL_INTERVAL` | 可选，CalDAV → Vault reconciliation 间隔（秒） | `300` |

预留但当前 MVP 尚未消费：

| 变量 | 说明 | 示例 |
|------|------|------|
| `RADICALE_RABBITMQ_URL` | Radicale hook 的 RabbitMQ 地址；后续用于近实时 CalDAV → Vault 触发 | `amqp://user:pass@rabbitmq:5672/` |
| `RADICALE_RABBITMQ_TOPIC` | Radicale hook 通知的 topic/routing key | `radicale-events` |

## 使用指南

本节面向负责部署的人或自动化 agent。当前项目提供 Dockerfile 和 Python 入口，但不提供 docker-compose、NAS 网络或 Radicale 部署方案。

### 前置条件

部署前确认这些条件已经满足：

- Radicale 已可通过 CalDAV/WebDAV URL 访问，且账号对 `/diomgis/tasks/` 和 `/diomgis/core-vault/` 有读写权限。
- FNS 服务已可通过 HTTP 访问，`FNS_API_TOKEN` 具备读笔记和修改 frontmatter 的权限。
- FNS token 具备 REST note 读写权限，以及 WS `NoteSync` 读取权限。当前推荐 scope 至少覆盖 `p:rest,ws c:caldav-bridge f:*`。
- 普通 FNS note 读写请求和 WS upgrade 请求都携带 `X-Client: caldav-bridge` 和 `X-Client-Name: caldav-bridge`。
- FNS vault 名称与 Obsidian/FNS 中的 vault 名称一致，例如 `Core`。
- 任务笔记集中在 `Tasks/` 路径下，或可通过 `TASK_PATH_KEYWORD` 的 path 搜索命中。当前不依赖 FNS content/FTS 搜索；启动扫描会先用 path 搜索缩小候选路径，再逐条读取详情并用 `is_task_note()` 过滤。
- 运行环境能持久化 `SYNC_STATE_PATH`，否则每次重启都会丢失 FNS WS cursor、CalDAV sync-token、ETag 和 UID/path 映射。

### 环境文件

建议从 `.env.example` 复制本地 `.env` 并填入真实配置：

```bash
cp .env.example .env
```

不要把包含真实密码或 token 的 `.env` 提交进仓库。

### 本地 Python 验证

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q .
.venv/bin/python main.py --help
```

### 真实凭据 Smoke Test

发布镜像前必须用真实 FNS/Radicale token 跑只读 smoke test。该脚本不会写 Obsidian，也不会写 CalDAV；它只验证 FNS path 搜索、FNS WS `NoteSync`、CalDAV collection 可读，以及任务发现是否能读到真实任务。

```bash
.venv/bin/python scripts/smoke_test.py --env-file .env
```

如果只想验证 API 连通性，临时跳过任务详情发现：

```bash
.venv/bin/python scripts/smoke_test.py --env-file .env --skip-discovery
```

### Docker 构建与镜像自检

```bash
docker build -t caldav-task-bridge:local .
docker run --rm caldav-task-bridge:local python -m unittest discover -s tests -v
docker run --rm caldav-task-bridge:local python -m compileall -q .
docker run --rm caldav-task-bridge:local python main.py --help
docker run --rm --env-file .env caldav-task-bridge:local python scripts/smoke_test.py --skip-discovery
```

### 镜像发布

推荐发布时同时推送语义版本、短 SHA 和 `latest`：

```bash
VERSION=0.1.8
SHA=$(git rev-parse --short HEAD)

docker build --network=host \
  -t dkreg.sigmoid.cc:53691/caldav-task-bridge:${VERSION} \
  -t dkreg.sigmoid.cc:53691/caldav-task-bridge:${SHA} \
  -t dkreg.sigmoid.cc:53691/caldav-task-bridge:latest .

docker run --rm dkreg.sigmoid.cc:53691/caldav-task-bridge:${VERSION} python -m unittest discover -s tests -v
docker run --rm dkreg.sigmoid.cc:53691/caldav-task-bridge:${VERSION} python -m compileall -q .
docker run --rm --env-file .env dkreg.sigmoid.cc:53691/caldav-task-bridge:${VERSION} python scripts/smoke_test.py --skip-discovery

docker push dkreg.sigmoid.cc:53691/caldav-task-bridge:${VERSION}
docker push dkreg.sigmoid.cc:53691/caldav-task-bridge:${SHA}
docker push dkreg.sigmoid.cc:53691/caldav-task-bridge:latest
```

Registry: `dkreg.sigmoid.cc:53691`（与 ntfy、miniflux 等同一 registry）。

如果构建环境通过宿主机代理访问 PyPI，且 Docker/Podman 构建容器连不到 `127.0.0.1` 代理，可改用：

```bash
docker build --network=host -t caldav-task-bridge:local .
```

### 单次同步验证

先执行上面的只读 smoke test，再以 pull 优先的顺序做一次完整同步：

```bash
docker run --rm --env-file .env -v "$PWD/data:/app/data" caldav-task-bridge:local python main.py --once both
```

`--once both` 会写入 FNS/CalDAV，并非只读检查。若 pull 发现手机端变更，本次会跳过 push，后续再执行一轮或由常驻服务继续收敛。不要在常驻服务运行时用另一个进程共享同一状态文件运行同步。只有明确确认没有未拉取的手机端变更时，才单独使用 `--once push` 做方向诊断。

验证点：

- `push` 后 Radicale `/diomgis/tasks/` 出现 VTODO；有 `due_date` 的任务在 `/diomgis/core-vault/` 出现 VEVENT。
- `pull` 后，手机端完成/改期产生的 CalDAV 变化通过 FNS 更新 Obsidian frontmatter。
- `data/state.json` 被创建，并包含 FNS WS cursor、collection sync-token、ETag 和 UID/path 映射。

### 常驻运行

2026-10-06 已验证的线上修复镜像：`dkreg.sigmoid.cc:53691/caldav-task-bridge:ios-fix-20261006-r2`。包含 FNS 3.6.1 分页确认、业务帧等待超时、单条 YAML/日期异常隔离、VEVENT 状态与任务状态分离及 `PRIORITY:0` 默认映射。该次维护通过 35 项测试和临时任务真实双向写回测试，并完成 65 个 VTODO、52 个 VEVENT 的字段一致性核验。历史清理前的状态与 ICS 保存在 NAS 的 bridge data/ops 维护备份目录中。

```bash
docker run -d \
  --name caldav-task-bridge \
  --restart unless-stopped \
  --env-file .env \
  -v "$PWD/data:/app/data" \
  caldav-task-bridge:local
```

查看日志：

```bash
docker logs -f caldav-task-bridge
```

停止：

```bash
docker stop caldav-task-bridge
docker rm caldav-task-bridge
```

### Agent 执行顺序

自动化 agent 可以按以下顺序执行：

1. 读取 `.env`，确认必填环境变量非空。
2. 执行 `docker build -t caldav-task-bridge:local .`；如 pip 因宿主代理失败，再执行 `docker build --network=host -t caldav-task-bridge:local .`。
3. 执行容器内测试：`python -m unittest discover -s tests -v` 和 `python -m compileall -q .`。
4. 创建并挂载持久化目录，例如 `./data:/app/data`。
5. 在没有其他 bridge 进程共享状态文件时，执行 `python main.py --once both`，先拉取手机端变更再推送。
6. 如果本轮因手机端变更跳过 push，等待下一轮收敛后确认 Radicale 对象和 FNS 写回正常。
7. 最后用常驻命令启动容器。

### 常见问题

- `Missing required environment variable`：env file 缺必填变量，或变量名拼写不一致。
- FNS 写回失败：确认 `FNS_API_TOKEN` 有 note/frontmatter 写权限；本服务不会改本地 vault 文件。
- Radicale `401/403`：确认 CalDAV 用户、密码和 collection 权限。
- `sync-token` 失效：服务会自动做一次全量 PROPFIND 重建状态。
- 重启后重复同步：确认 `SYNC_STATE_PATH` 所在目录已挂载持久化卷；否则服务会重新做首次 path 搜索并重建 FNS/CalDAV 游标。
- 进程在运行但任务长期未更新：检查 `state.json` 的 `last_push_timestamp`、`fns.pending_note_changes` 和日志中的 `push sync failed`。FNS 增量包括普通笔记和模板；无法解析的 YAML 或任务日期会保留待重试，不能阻断后续任务。接口连通性 smoke test 成功并不代表同步队列已收敛。
- 长期失败恢复后的校准：先停止常驻 bridge、备份状态和待清理的 ICS；只清理已通过 FNS 确认为不存在、且 UID 与 `X-OBSIDIAN-PATH` 匹配的 bridge 对象，删除使用当前 ETag 条件。随后先 pull 接受手机端变更，再通过初始化任务扫描完整重建投影，最后重启常驻服务。不要把鉴权、网络或 YAML 解析失败视为笔记已经删除，也不要在两个进程中并发修改同一份状态文件。

### Synology NAS 检查权限持久化

本项目线上使用 NAS 账号 `gong.junheng` 和 `/usr/local/bin/docker`。如检查时 Docker socket 拒绝访问、`sudo -n` 要求密码，可由管理员在 NAS 终端执行以下命令，配置仅容器查询所需的权限：

```bash
sudo sh -c 'printf "%s\n" "gong.junheng ALL=(root) NOPASSWD: /usr/local/bin/docker ps *, /usr/local/bin/docker inspect *, /usr/local/bin/docker logs *" > /etc/sudoers.d/caldav-bridge-audit && chmod 0440 /etc/sudoers.d/caldav-bridge-audit'
sudo -n /usr/local/bin/docker ps --format '{{.Names}} {{.Status}}'
```

规则文件保留在系统目录中，普通重启无需重新配置。为应对 DSM 升级重置系统配置，可在 DSM「控制面板 → 任务计划」新增「触发的任务 → 用户定义的脚本」，用户设为 `root`，事件设为「开机」，脚本填写：

```bash
sh -c 'printf "%s\n" "gong.junheng ALL=(root) NOPASSWD: /usr/local/bin/docker ps *, /usr/local/bin/docker inspect *, /usr/local/bin/docker logs *" > /etc/sudoers.d/caldav-bridge-audit && chmod 0440 /etc/sudoers.d/caldav-bridge-audit'
```

DSM 升级后仍应验证开机任务及 `sudo -n` 的结果；该规则不授予容器启动、停止、执行命令或部署权限。

如已授权 agent 自行完成容器部署、重启和维护，可由管理员一次性安装 Docker/Compose 管理权限。将仓库中的 `scripts/install_nas_docker_permissions.sh` 上传到 NAS 的 `/volume1/docker/caldav-bridge/ops/` 后执行：

```bash
sudo sh /volume1/docker/caldav-bridge/ops/install_nas_docker_permissions.sh
```

安装器写入 `/etc/sudoers.d/caldav-bridge-maintenance`，允许 `gong.junheng` 免密执行 `/usr/local/bin/docker` 和 `/usr/local/bin/docker-compose`。同时安装 root 所有、仅 root 可修改的 `/usr/local/etc/rc.d/S99-caldav-bridge-permissions.sh`，由已核实的 DSM `pkg-rclocal.service` 在开机时恢复规则。旧的只读审计规则及开机任务可以保留，两者使用不同规则文件。该授权覆盖 Docker/Compose 管理，后续其他系统管理命令仍按其实际权限执行。DSM 大版本升级后应验证该开机脚本是否仍被保留和执行。

## 目录结构

```
caldav-task-bridge/
├── README.md           # 本文件
├── Dockerfile
├── main.py             # 入口：定时调度 push + pull
├── push.py             # Vault → CalDAV 推送模块
├── pull.py             # CalDAV → Vault 拉取模块
├── vault.py            # Vault 读写工具（FNS REST）
├── fns_ws.py           # FNS WebSocket NoteSync 增量客户端
├── caldav_client.py    # Radicale 交互封装
├── models.py           # 数据模型（Task, VtodoMapping, VeventMapping）
├── state.py            # FNS WS cursor、CalDAV sync-token、UID/path、ETag 状态
└── requirements.txt
```

## 验收标准

1. **推送**：启动 bridge 后，Radicale 中出现 vault 中所有活跃任务的 VTODO，有 due_date 的出现 VEVENT
2. **更新**：在 Obsidian 中修改 due_date → 在 `PUSH_INTERVAL` 内 Radicale 对应事件更新；后续接入 FNS WebSocket 后应近实时更新
3. **完成**：在 Obsidian 中标记 task_status=已完成 → Radicale 中 VTODO 变为 COMPLETED
4. **手机标记完成**：在手机 Reminders/Tasks.org 中标记完成 → 通过 FNS API 将 vault 笔记 frontmatter 更新为 task_status=已完成
5. **手机改期**：在手机日历中拖拽改期 → 通过 FNS API 将 vault 笔记 frontmatter 更新 due_date
6. **多设备同步**：pull 写回后的变更，其他 Obsidian 设备通过 FNS 在 30 秒内看到
7. **无文件系统写回**：断开或禁用 FNS API 后，CalDAV → Vault 写回失败并记录错误，不修改本地 vault 文件

## 调研依据

- WebDAV Sync 标准：RFC 6578 `sync-collection` / `sync-token`，https://www.rfc-editor.org/rfc/rfc6578
- Radicale v3 文档：sync-token 缓存、storage hook、`[hook]` RabbitMQ 通知，https://radicale.org/v3.html
- FNS REST API：`GET /api/note`、`POST /api/note`、`PATCH /api/note/frontmatter`，https://github.com/haierkeys/fast-note-sync-service/blob/master/docs/REST_API.md
- FNS 服务 README：REST API、WebSocket 实时同步、MCP/SSE 信息，https://github.com/haierkeys/fast-note-sync-service
- FastNodeSync-CLI：基于 FNS WebSocket 的 headless 双向同步客户端参考，https://github.com/Go1c/FastNodeSync-CLI
