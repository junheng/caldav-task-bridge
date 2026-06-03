# CalDAV Task Bridge

Obsidian Core Vault ↔ CalDAV 双向任务同步桥接服务。

## 目标

将 Obsidian Core Vault 中的任务笔记同步到 CalDAV 服务器（Radicale），在手机日历/任务 App 中原生管理 Obsidian 任务。用户在手机上标记完成、改期、推迟等操作自动写回 vault。

## 架构

```
┌──────────────────┐     VTODO + VEVENT       ┌──────────────┐     CalDAV      ┌──────────────┐
│  Obsidian Vault   │ ──────────────────────▶  │   Radicale    │ ◀─────────────▶ │  手机日历 App  │
│  (任务笔记 .md)    │                           │   (NAS Docker) │                 │  (自带日历/    │
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

- **Radicale**：NAS 上运行的 CalDAV 服务器（已有 ntfy 在 Docker，复用 `ntfy_network` bridge 网络）
- **推送方向**（Vault → CalDAV）：定期扫描 vault 任务笔记，生成 VTODO + VEVENT，通过 CalDAV 协议 PUT 到 Radicale
- **拉取方向**（CalDAV → Vault）：定期检查 Radicale 中的变更，映射回 vault frontmatter，通过 FNS API 写回笔记

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

逾期任务（`due_date < today` 且 `task_status != 已完成`）的 VEVENT 固定在**今天**显示，确保日历上可见。

### CalDAV 存储结构

在 Radicale 中创建两个 collection：

- `/diomgis/tasks/` — VTODO 对象（供 Tasks.org / Reminders.app 使用）
- `/diomgis/core-vault/` — VEVENT 对象（供 Calendar.app 使用）

## 推送同步（Vault → CalDAV）：`push.py`

### 输入

- Vault 根路径（文件系统直接读取或通过 ob-cli）
- Radicale 连接信息（URL、用户名、密码）

### 执行流程

```
1. 扫描 vault 中所有 type/task 笔记
   - 方式 A：文件系统 glob **/Tasks/*.md，解析 frontmatter
   - 方式 B：调用 ob-cli.py search --tag type/task --format json --limit 200

2. 过滤：task_status != "已完成" 且 deleted != true

3. 对每个活跃任务：
   a. 计算 UID = md5(file_path)[:12]
   b. 构造 VTODO（icalendar Todo 对象）
   c. 查询 Radicale：GET /diomgis/tasks/?uid=task-{hash}
      - 存在 → CalDAV PUT 更新
      - 不存在 → CalDAV PUT 新建
   d. 如果 due_date 非空：
      同上对 /diomgis/core-vault/ 的 VEVENT

4. 对已完成任务（task_status == "已完成"）：
   更新对应的 VTODO STATUS=COMPLETED, VEVENT STATUS=CANCELLED

5. 输出日志：新增 X 条，更新 Y 条，完成 Z 条
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
1. 连接 Radicale，查询 /diomgis/tasks/ 和 /diomgis/core-vault/
2. 获取所有 VTODO/VEVENT，按 UID 匹配到 vault 笔记路径
3. 对比变更（以 Radicale 的 LAST-MODIFIED 时间戳为准）：
   a. STATUS 变为 COMPLETED（手机上点了完成）
      → 更新 frontmatter: task_status=已完成, done_date=today
   b. DUE 日期变更（手机上拖拽改期）
      → 更新 frontmatter: due_date=<new date>
   c. STATUS 变为 CANCELLED（手机端取消）
      → 更新 frontmatter: task_status=阻塞
   d. DESCRIPTION/PRIORITY 变更
      → 对应更新 frontmatter

4. 写回 vault：
   方式：POST 到 FNS REST API 更新笔记内容
   - 读取笔记当前内容（GET /api/file）
   - 修改 frontmatter 中的 YAML
   - 写回（确认 FNS API 的写接口）

5. 输出日志：同步了 X 条变更
```

### 冲突处理

简单策略：**Radicale 优先，但 Obsidian 为准**。
- 记录每次 push 的时间戳
- pull 时只处理 `LAST-MODIFIED > last_push_timestamp` 的变更
- 如果两边都改了（极少数情况），以 Radicale 变更为准，下次 push 时 Obsidian 的值会覆盖回去

### FNS API 写回

FNS 暴露 REST API（地址配置在 Obsidian 插件设置中）。写回流程：

```
GET  {FNS_API_URL}/api/file?vault=Core&path={path}&token={token}
     → 获取当前笔记内容

修改 YAML frontmatter
     
PUT/POST {FNS_API_URL}/api/file
     body: { vault: "Core", path: "...", content: "...", contentHash: "...", ... }
     → 通过 FNS 服务器同步到所有设备
```

如果 FNS 写 API 不可用，fallback：直接写文件系统（NAS 上挂载的 vault 路径），FNS 插件会检测文件变更并同步。

## Docker 部署

### 镜像

```dockerfile
FROM python:3.11-slim
COPY . /app
RUN pip install caldav icalendar requests pyyaml
WORKDIR /app
CMD ["python", "main.py"]
```

### NAS 配置

Radicale 已部署在同一 Docker 网络，本服务加入同一 bridge：

```yaml
services:
  caldav-task-bridge:
    image: ghcr.io/junheng/caldav-task-bridge:latest
    container_name: caldav-task-bridge
    restart: always
    environment:
      - RADICALE_URL=http://radicale:5232
      - RADICALE_USER=diomgis
      - RADICALE_PASSWORD=${RADICALE_PASSWORD}
      - VAULT_ROOT=/data/vault
      - FNS_API_URL=${FNS_API_URL}
      - FNS_API_TOKEN=${FNS_API_TOKEN}
      - PUSH_INTERVAL=900      # 每 15 分钟推送
      - PULL_INTERVAL=300      # 每 5 分钟拉取
    volumes:
      - /volume1/docker/caldav-bridge/data:/data
    networks:
      - ntfy_network
```

## 配置

环境变量，全部必填：

| 变量 | 说明 | 示例 |
|------|------|------|
| `RADICALE_URL` | Radicale CalDAV 地址 | `http://radicale:5232` |
| `RADICALE_USER` | CalDAV 用户名 | `diomgis` |
| `RADICALE_PASSWORD` | CalDAV 密码 | |
| `VAULT_ROOT` | Obsidian vault 根路径 | `/data/vault` 或 NAS 挂载路径 |
| `FNS_API_URL` | FNS 服务器地址 | `https://fns.sigmoid.cc:53691` |
| `FNS_API_TOKEN` | FNS API Token | |
| `PUSH_INTERVAL` | 推送间隔（秒） | `900` |
| `PULL_INTERVAL` | 拉取间隔（秒） | `300` |

## 目录结构

```
caldav-task-bridge/
├── README.md           # 本文件
├── Dockerfile
├── docker-compose.yml  # 包含 Radicale + bridge 的完整部署
├── main.py             # 入口：定时调度 push + pull
├── push.py             # Vault → CalDAV 推送模块
├── pull.py             # CalDAV → Vault 拉取模块
├── vault.py            # Vault 读写工具（ob-cli 调用 / FNS API / 文件系统）
├── caldav_client.py    # Radicale 交互封装
├── models.py           # 数据模型（Task, VtodoMapping, VeventMapping）
└── requirements.txt
```

## 验收标准

1. **推送**：NAS 上启动容器后，Radicale 中出现 vault 中所有活跃任务的 VTODO，有 due_date 的出现 VEVENT
2. **更新**：在 Obsidian 中修改 due_date → 15 分钟内 Radicale 中的对应事件更新
3. **完成**：在 Obsidian 中标记 task_status=已完成 → Radicale 中 VTODO 变为 COMPLETED
4. **手机标记完成**：在手机 Reminders/Tasks.org 中标记完成 → vault 笔记 frontmatter 更新为 task_status=已完成
5. **手机改期**：在手机日历中拖拽改期 → vault 笔记 frontmatter 更新 due_date
6. **多设备同步**：pull 写回后的变更，其他 Obsidian 设备通过 FNS 在 30 秒内看到
