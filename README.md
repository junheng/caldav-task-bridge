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
| `task_status: 阻塞` | `STATUS:CANCELLED` | 阻塞视为取消，备注里写原因 |
| `priority: 1` | `PRIORITY:1` | 高优 |
| `priority: 2` | `PRIORITY:5` | 中优 |
| `priority: 3` | `PRIORITY:9` | 低优 |
| `due_date` | `DUE;VALUE=DATE:<date>` | 截止日期 |
| `scheduled_date` | `DTSTART;VALUE=DATE:<date>` | 计划开始日期 |
| `title` (文件名) | `SUMMARY` | 任务标题 |
| `assignee` + `related_project` + 正文摘要 | `DESCRIPTION` | 摘要信息、去掉 frontmatter 后的笔记正文摘要、`obsidian://open` 链接 |
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
| `STATUS` | `CONFIRMED`（未完成）/ `CANCELLED`（已完成） |
| `X-OBSIDIAN-PATH` | vault 相对路径 |

VEVENT 的 `DTSTART` 始终等于真实 `due_date`。逾期任务只在 `SUMMARY` 前加 `🚨`，不移动事件日期，避免 pull 把展示日期误写回 Obsidian。

`DESCRIPTION` 按 iCalendar 标准写入文本并由库自动折行；标准没有给 VTODO/VEVENT 描述设定很小的字数上限，但客户端展示和同步性能会有实际限制，因此本项目只写入正文摘要，默认最多 4000 字符。

### CalDAV 存储结构

在 Radicale 中创建两个 collection：

- `/diomgis/tasks/` — VTODO 对象（供 Tasks.org / Reminders.app 使用）
- `/diomgis/core-vault/` — VEVENT 对象（供 Calendar.app 使用）

## 同步触发与增量策略

### 调研结论

- CalDAV/WebDAV 标准支持 `DAV:sync-collection` REPORT 和 `DAV:sync-token`，客户端可以保存 collection 的 opaque token，后续只取新增、修改、删除的资源，而不是每次全量拉取。
- Radicale v3 文档显示其文件存储会维护 sync-token 缓存；同时 `[hook]` 支持 `rabbitmq`，可用于事件变更和删除通知。
- Python `caldav` 库已支持按 `sync_token` 拉取 collection 对象；如果服务端不支持或 token 失效，再做一次全量获取。
- FNS 服务同时提供 REST API 和 WebSocket 同步接口；REST API 的 `PATCH /api/note/frontmatter` 适合作为本项目写回 Obsidian 的接口。
- 上游受限的 REST 更新日志接口不再作为本项目依赖；FNS 增量来源统一收敛到 WebSocket。
- FNS WebSocket `/api/user/sync` 的 `NoteSync` 是当前选定的 Vault 增量来源。实测需要在 HTTP upgrade 请求上带 `X-Client: caldav-bridge`，并用原始帧 `Authorization|<token>` 鉴权，不能把 token JSON 字符串化。

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

2. 过滤：task_status != "已完成" 且 deleted != true

3. 对每个活跃任务：
   a. 计算 UID = md5(file_path)[:12]
   b. 构造 VTODO（icalendar Todo 对象）
      - 写入 UID、SUMMARY、STATUS、PRIORITY、DUE、DTSTART、DESCRIPTION、CATEGORIES
      - DESCRIPTION 包含去掉 frontmatter 后的笔记正文摘要；当前摘要上限为 4000 字符，过长会截断
      - 写入 URL 和 ATTACH;VALUE=URI，提供任务客户端可直接点击的 Obsidian 链接
      - 写入 X-OBSIDIAN-PATH，确保 pull 能定位原始笔记
   c. 查询 Radicale 中同 UID 对象
      - 存在 → CalDAV PUT 更新
      - 不存在 → CalDAV PUT 新建
   d. 如果 due_date 非空：
      同上对 /diomgis/core-vault/ 的 VEVENT

4. 对已完成任务（task_status == "已完成"）：
   更新对应的 VTODO STATUS=COMPLETED, VEVENT STATUS=CANCELLED

5. 对 FNS 删除或 `deleted: true` 的任务：
   删除对应的 VTODO 和 VEVENT，并清理本地 UID/path 与 ETag 状态

6. 更新本地同步状态：UID ↔ path、ETag、last_push_timestamp、FNS WS cursor
7. 输出日志：新增 X 条，更新 Y 条，完成 Z 条，删除 T/E 条
```

### UID 约定

```
VTODO:  task-{md5(vault_relative_path)[:12]}@core-vault
VEVENT: event-{md5(vault_relative_path)[:12]}@core-vault
```

UID 是稳定标识符，基于笔记路径。重命名笔记 → UID 变化 → 旧事件待清理（后续版本改进）。

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
      → 更新 frontmatter: task_status=已完成, done_date=today
   b. DUE 日期变更（手机上拖拽改期）
      → 更新 frontmatter: due_date=<new date>
   c. STATUS 变为 CANCELLED（手机端取消）
      → 更新 frontmatter: task_status=阻塞
   d. DESCRIPTION/PRIORITY 变更
      → 对应更新 frontmatter

5. 写回 vault：
   - 首选：PATCH {FNS_API_URL}/api/note/frontmatter
   - Header: Authorization: {FNS_API_TOKEN}
   - Body: { vault, path, updates, remove? }
   - FNS 返回失败时不写文件系统，记录失败并等待重试

6. 更新本地 sync-token/ETag 状态
7. 输出日志：同步了 X 条变更
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
    "task_status": ["已完成"],
    "done_date": ["2026-06-03"]
  },
  "remove": []
}
```

说明：

- `PATCH /api/note/frontmatter` 是当前调研到的最贴合接口，避免读取整篇笔记再重写。
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
| `FNS_CLIENT_VERSION` | 可选，FNS `X-Client-Version` 值 | `0.1.6` |
| `FNS_USER_AGENT` | 可选，FNS 请求 User-Agent | `caldav-task-bridge/0.1.6` |
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
VERSION=0.1.6
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

先跑只读/低风险的方向验证，再跑完整同步：

```bash
docker run --rm --env-file .env -v "$PWD/data:/app/data" caldav-task-bridge:local python main.py --once push
docker run --rm --env-file .env -v "$PWD/data:/app/data" caldav-task-bridge:local python main.py --once pull
docker run --rm --env-file .env -v "$PWD/data:/app/data" caldav-task-bridge:local python main.py --once both
```

验证点：

- `push` 后 Radicale `/diomgis/tasks/` 出现 VTODO；有 `due_date` 的任务在 `/diomgis/core-vault/` 出现 VEVENT。
- `pull` 后，手机端完成/改期产生的 CalDAV 变化通过 FNS 更新 Obsidian frontmatter。
- `data/state.json` 被创建，并包含 FNS WS cursor、collection sync-token、ETag 和 UID/path 映射。

### 常驻运行

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
5. 先执行 `python main.py --once push`，确认 Radicale 中出现对象。
6. 再执行 `python main.py --once pull`，确认 FNS 写回正常。
7. 最后用常驻命令启动容器。

### 常见问题

- `Missing required environment variable`：env file 缺必填变量，或变量名拼写不一致。
- FNS 写回失败：确认 `FNS_API_TOKEN` 有 note/frontmatter 写权限；本服务不会改本地 vault 文件。
- Radicale `401/403`：确认 CalDAV 用户、密码和 collection 权限。
- `sync-token` 失效：服务会自动做一次全量 PROPFIND 重建状态。
- 重启后重复同步：确认 `SYNC_STATE_PATH` 所在目录已挂载持久化卷；否则服务会重新做首次 path 搜索并重建 FNS/CalDAV 游标。

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
