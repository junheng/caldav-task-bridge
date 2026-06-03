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
| `assignee` + `related_project` | `DESCRIPTION` | 摘要信息 + `obsidian://open` 链接 |
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
| `DESCRIPTION` | `obsidian://open?vault=Core&file={path}` |
| `STATUS` | `CONFIRMED`（未完成）/ `CANCELLED`（已完成） |
| `X-OBSIDIAN-PATH` | vault 相对路径 |

逾期任务（`due_date < today` 且 `task_status != 已完成`）的 VEVENT 固定在**今天**显示，确保日历上可见。

### CalDAV 存储结构

在 Radicale 中创建两个 collection：

- `/diomgis/tasks/` — VTODO 对象（供 Tasks.org / Reminders.app 使用）
- `/diomgis/core-vault/` — VEVENT 对象（供 Calendar.app 使用）

## 同步触发与增量策略

### 调研结论

- CalDAV/WebDAV 标准支持 `DAV:sync-collection` REPORT 和 `DAV:sync-token`，客户端可以保存 collection 的 opaque token，后续只取新增、修改、删除的资源，而不是每次全量拉取。
- Radicale v3 文档显示其文件存储会维护 sync-token 缓存；同时 `[hook]` 支持 `rabbitmq`，可用于事件变更和删除通知。
- Python `caldav` 库已支持按 `sync_token` 拉取 collection 对象；如果服务端不支持或 token 失效，再做一次全量获取。
- FNS 服务公开资料显示同时提供 REST API 和 WebSocket 实时同步；REST API 已有 `PATCH /api/note/frontmatter`，适合作为本项目写回 Obsidian 的首选接口。

### 当前决策

1. **不直接写 vault 文件系统**。CalDAV → Vault 的写回只走 FNS API；FNS 写失败时记录错误并等待下次同步重试。
2. **CalDAV 拉取使用 sync-token 增量同步**。`PULL_INTERVAL` 只作为未配置事件触发时的 reconciliation 间隔，不再做每次全量轮询。
3. **Radicale 事件触发作为后续增强**。如果后续接入 RabbitMQ hook，本服务消费通知后立即执行一次 sync-token delta pull；通知本身只作为触发信号，真实差异仍以 CalDAV REPORT 结果为准。
4. **Vault 推送当前使用 FNS REST reconciliation**。后续接入 FNS WebSocket note 变更事件后，只重新解析受影响任务；当前按 `PUSH_INTERVAL` 低频扫描。
5. **必须持久化同步状态**：包括 `last_push_timestamp`、每个 CalDAV collection 的 `sync_token`、UID ↔ vault 相对路径映射、已知 ETag。状态文件默认放在本服务自己的 data 目录，不写入 vault。

## 推送同步（Vault → CalDAV）：`push.py`

### 输入

- FNS 连接信息（REST API 必需；WebSocket 事件流预留）
- Radicale 连接信息（URL、用户名、密码）

### 执行流程

```
1. 取得需要同步的任务笔记
   - 启动时：通过 FNS REST 搜索/读取 type/task 笔记，建立 UID ↔ path 状态
   - 当前运行时：按 PUSH_INTERVAL 低频重新扫描，修复漏事件或状态丢失
   - 后续增强：消费 FNS WebSocket note 变更事件，只处理受影响路径

2. 过滤：task_status != "已完成" 且 deleted != true

3. 对每个活跃任务：
   a. 计算 UID = md5(file_path)[:12]
   b. 构造 VTODO（icalendar Todo 对象）
      - 写入 UID、SUMMARY、STATUS、PRIORITY、DUE、DTSTART、DESCRIPTION、CATEGORIES
      - 写入 X-OBSIDIAN-PATH，确保 pull 能定位原始笔记
   c. 查询 Radicale 中同 UID 对象
      - 存在 → CalDAV PUT 更新
      - 不存在 → CalDAV PUT 新建
   d. 如果 due_date 非空：
      同上对 /diomgis/core-vault/ 的 VEVENT

4. 对已完成任务（task_status == "已完成"）：
   更新对应的 VTODO STATUS=COMPLETED, VEVENT STATUS=CANCELLED

5. 更新本地同步状态：UID ↔ path、ETag、last_push_timestamp
6. 输出日志：新增 X 条，更新 Y 条，完成 Z 条
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
4. **持久化状态**：`SYNC_STATE_PATH` 保存每个 collection 的 sync-token、对象 ETag 和 UID/path 映射。只要该文件持久化，服务重启后仍能继续做增量同步和条件写。
5. **FNS-only 写回**：CalDAV → Vault 只调用 FNS frontmatter API。FNS 不可用时写回失败并重试，不直接改本地 vault 文件，避免绕过 FNS 造成多设备状态分叉。

已知边界：

- 如果手动只运行 `--once push`，服务会按 Obsidian 当前值推送；部署自动化应优先使用 `--once both` 或常驻模式。
- 如果 `SYNC_STATE_PATH` 丢失，服务需要通过一次全量同步重建 sync-token/ETag 状态；这期间无法识别“已知对象被手机端改过”的条件写冲突。

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
| `SYNC_STATE_PATH` | 可选，本服务同步状态文件路径 | `./data/state.json` |
| `PUSH_INTERVAL` | 可选，Vault → CalDAV reconciliation 间隔（秒） | `900` |
| `PULL_INTERVAL` | 可选，CalDAV → Vault reconciliation 间隔（秒） | `300` |

预留但当前 MVP 尚未消费：

| 变量 | 说明 | 示例 |
|------|------|------|
| `FNS_WS_URL` | FNS WebSocket 事件地址；后续用于近实时 Vault → CalDAV 触发 | `wss://fns.example.com/api/user/sync` |
| `RADICALE_RABBITMQ_URL` | Radicale hook 的 RabbitMQ 地址；后续用于近实时 CalDAV → Vault 触发 | `amqp://user:pass@rabbitmq:5672/` |
| `RADICALE_RABBITMQ_TOPIC` | Radicale hook 通知的 topic/routing key | `radicale-events` |

## 使用指南

本节面向负责部署的人或自动化 agent。当前项目提供 Dockerfile 和 Python 入口，但不提供 docker-compose、NAS 网络或 Radicale 部署方案。

### 前置条件

部署前确认这些条件已经满足：

- Radicale 已可通过 CalDAV/WebDAV URL 访问，且账号对 `/diomgis/tasks/` 和 `/diomgis/core-vault/` 有读写权限。
- FNS 服务已可通过 HTTP 访问，Token 具备读笔记和修改 frontmatter 的权限。
- FNS vault 名称与 Obsidian/FNS 中的 vault 名称一致，例如 `Core`。
- 任务笔记能被 FNS REST 搜索到。当前默认用 `TASK_SEARCH_KEYWORD=type/task` 搜索，然后再解析 frontmatter 判断是否为任务。
- 运行环境能持久化 `SYNC_STATE_PATH`，否则每次重启都会丢失 sync-token、ETag 和 UID/path 映射。

### 环境文件

建议用 env file 管理配置，例如 `.env`：

```bash
RADICALE_URL=http://radicale:5232
RADICALE_USER=diomgis
RADICALE_PASSWORD=change-me

FNS_API_URL=https://fns.example.com
FNS_API_TOKEN=change-me
FNS_VAULT=Core

SYNC_STATE_PATH=/app/data/state.json
PUSH_INTERVAL=900
PULL_INTERVAL=300
TASK_SEARCH_KEYWORD=type/task
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

### Docker 构建与镜像自检

```bash
docker build -t caldav-task-bridge:local .
docker run --rm caldav-task-bridge:local python -m unittest discover -s tests -v
docker run --rm caldav-task-bridge:local python -m compileall -q .
docker run --rm caldav-task-bridge:local python main.py --help
```

### 镜像发布

推送至 NAS 私有 registry：

```bash
docker build -t dkreg.sigmoid.cc:53691/caldav-task-bridge:latest .
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
- `data/state.json` 被创建，并包含 collection sync-token、ETag 和 UID/path 映射。

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
- 重启后重复同步：确认 `SYNC_STATE_PATH` 所在目录已挂载持久化卷。

## 目录结构

```
caldav-task-bridge/
├── README.md           # 本文件
├── Dockerfile
├── main.py             # 入口：定时调度 push + pull
├── push.py             # Vault → CalDAV 推送模块
├── pull.py             # CalDAV → Vault 拉取模块
├── vault.py            # Vault 读写工具（FNS REST）
├── caldav_client.py    # Radicale 交互封装
├── models.py           # 数据模型（Task, VtodoMapping, VeventMapping）
├── state.py            # sync-token、UID/path、ETag、last_push_timestamp 状态
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
