# Obsidian 桥接协议（设计稿 · 文件信箱版）

> 一句话：**QFStudio 和 Obsidian 插件通过一个共享目录里的 JSON 文件信箱通信，不走网络。**
> 插件是 vault 的代理；QFStudio 写请求文件，插件写响应文件。

状态：设计稿，尚未实现。人的笔记界面（笔记页 / 画布 / 资源树）已于 2026-11-07 删除；
LLM 侧的五个笔记工具保留（`read_note` / `list_notes` / `search_notes` / `edit_note` /
`edit_note`（不给 `old` 就是追加）），它们是这次桥接要接上的那一端。

---

## 一、为什么不用网络（这是本设计的起点）

这台机器上 WSL 的网络是**不能当作可靠传输**的：

- WSL2 是 NAT，Windows 侧监听 `127.0.0.1` 的服务在 WSL 里**连不上**（那个地址是 WSL 自己）；
- 让插件监听 `0.0.0.0` 再连 Windows 的局域网 IP，要处理网关变化、防火墙、Clash 的 fake-ip
  （`198.18.x.x`）、代理环境变量把回环请求也送进代理等一堆问题 —— 每一条都够让"偶尔连不上"
  变成常态；
- 用户的原话："协议不要走网络，wsl 的网络很混乱。实际的情况比这复杂得多。"

而 **文件系统是通的**：vault 在 Windows 盘上，WSL 里就是 `/mnt/f/Notes`。双方看到的是
同一个目录、同一批文件。所以传输层用「目录 + 原子写文件」，而不是 TCP。

---

## 二、拓扑

```
WSL（QFStudio 后端）                       Windows（Obsidian 插件）
      │                                            │
      ├── 写 /mnt/f/Notes/.quizforge-bridge/inbox/req-<id>.json ──▶ 监听 inbox
      │                                            │  处理
      ◀── 轮询 /mnt/f/Notes/.quizforge-bridge/outbox/res-<id>.json ── 写响应
      │                                            │
      └── 读 bridge.json / heartbeat 判断在线
```

- **只有一个共享目录**：`<vault>/.quizforge-bridge/`。
- 不监听端口、不需要 IP、不需要发现协议；"谁在哪"由用户在设置里填的 vault 路径决定。

### 目录结构

```
<vault>/.quizforge-bridge/
  bridge.json          # 插件写：协议版本、能力、vault 名、启动时间、pid
  heartbeat            # 插件每秒 touch（内容 = ISO 时间）；QFStudio 用它判断在线
  inbox/               # QFStudio → 插件：req-<id>.json
  outbox/              # 插件 → QFStudio：res-<id>.json
  tmp/                 # 原子写的中间文件（写完 rename 进 inbox/outbox）
```

`.quizforge-bridge/` 放在 vault 根，不在 `.obsidian/` 里：那是 Obsidian 的配置目录，
插件升级/同步都可能动它；信箱是**工作目录**，越简单越好。

---

## 三、协议要素

### 3.1 原子写（两端都必须遵守）

```
写 tmp/xxxx.tmp      → flush → rename 到 inbox/req-<id>.json
```

- 同一目录内的 `rename` 是原子的（NTFS 与 drvfs 都成立），读者要么看不到，要么看到完整文件。
- 绝不"就地写目标文件"：读到半个 JSON 会让对端以为请求坏了。
- 文件编码 UTF-8、单行 JSON（不 pretty-print，省得按行解析）。

### 3.2 请求

```json
{
  "protocol": 1,
  "id": "6f0c1b7e-2a4d-4f2b-9c7a-1f3d5e8a9b21",
  "ts": 1794060000123,
  "op": "read",
  "params": { "path": "buffer/环形缓冲.md", "startChar": 0, "maxChars": 20000 },
  "expect": "5s"
}
```

- `id`：uuid4，唯一；响应文件名由它派生。
- `expect`：`1s` / `5s` / `10s` —— 提示插件这个操作用多久算超时（插件可据此优先处理）。
- `op` 与 `params` 见 §四。

### 3.3 响应

```json
{
  "protocol": 1,
  "id": "6f0c1b7e-…",
  "ok": true,
  "result": { "path": "buffer/环形缓冲.md", "title": "环形缓冲", "body": "…", "hash": "9f2c…", "chars": 137000 },
  "ts": 1794060000187
}
```

失败：

```json
{
  "protocol": 1,
  "id": "…",
  "ok": false,
  "error": { "code": "not_found", "message": "没有这篇笔记：buffer/环形缓冲.md" },
  "ts": …
}
```

### 3.4 在线判定

- 插件启动：建目录、清掉 `inbox/`、`outbox/`、`tmp/` 里超过 60 秒的陈旧文件、写 `bridge.json`、
  起一个 1 秒的 timer touch `heartbeat`；退出时删 `bridge.json`（heartbeat 自然过期）。
- QFStudio：`bridge.json` 在 **且** `heartbeat` 的 mtime 在 5 秒内 = 在线。
- 不在线就不发请求，直接返回明确错误（不排队、不等）：

  ```json
  { "error": "还没有连上 Obsidian：在设置里填 vault 路径，并在 Obsidian 里启用 QFStudio 桥接插件。" }
  ```

### 3.5 超时与清理

- QFStudio 每 50ms 看一次 `outbox/res-<id>.json`；到 `expect` 上限还没出现 → 删请求文件、
  返回 `timeout`（插件可能正在忙；**不自动重试写操作**）。
- 插件处理完写响应后删请求；QFStudio 读完响应后删响应。
- 双方启动时都清陈旧文件（> 60 秒），避免断电/崩溃留下的垃圾越积越多。

### 3.6 幂等

- 请求 `id` 唯一。插件处理前先看 `outbox/res-<id>.json` 是否存在：存在就跳过（重放安全）。
- 写操作（append/replace/write）在响应里回 `hash`，QFStudio 收到的 hash 与预期不符视为
  冲突，让模型重读，不自动覆盖。

### 3.7 并发

- 信箱是**单通道**：一次只处理一个请求（插件串行）。单用户场景下够用，也省掉锁。
- 若将来要并发：按 `id` 分目录即可，协议不变。

---

## 四、操作集（v1）

| op | params | result | 说明 |
|---|---|---|---|
| `ping` | — | `{protocol, pluginVersion, capabilities}` | 连通性/版本 |
| `tree` | `{dir?}` | 目录树（只含笔记与画布，不含附件） | 对应 `list_notes` |
| `read` | `{path, startChar?, maxChars?}` | 见 §4.1 | 对应 `read_note` |
| `search` | `{query, limit?, folder?}` | `{items:[…], count}` | 对应 `search_notes` |
| `write` | `{path, body, baseHash?}` | `{path, hash, chars}` | 整篇写入（工具层已经是"读出→改→整篇写回"） |
| `append` | `{path, text, baseHash?}` | `{path, hash, addedChars}` | 末尾追加 |
| `replace` | `{path, old, new, all?, baseHash?}` | `{path, hash, replaced}` | 逐字替换/删除 |
| `create` | `{path?, folder?, title?, body?}` | `{path}` | 新建（给 `path` 就是完整相对路径，父目录一并建） |
| `open` | `{path, line?}` | `{ok}` | 在 Obsidian 里打开并定位 |
| `trash` | `{path}` | `{ok}` | 移到 Obsidian 回收站 |

### 4.1 `read` 的返回

```json
{
  "lib": "Math",
  "path": "buffer/环形缓冲.md",
  "kind": "note",
  "title": "环形缓冲",
  "body": "……正文……",
  "chars": 137000,
  "startChar": 0,
  "truncated": true,
  "nextChar": 20000,
  "hash": "9f2c…",
  "mtime": "2026-11-07T22:15:00",
  "tags": ["buffer"],
  "meta": { "tags": ["buffer"], "aliases": ["ring buffer"] },
  "outline": [{ "level": 2, "text": "为什么", "line": 12 }],
  "backlinks": [{ "path": "Math/索引.md", "title": "索引" }],
  "outlinks": [{ "target": "内存屏障", "resolved": "concurrency/内存屏障.md" }]
}
```

形状与现有 `notelib.read_note` 对齐（工具层不用改字段名）；正文按**字区间**切。
`hash` = 正文 sha256 前 16 位，用于写入冲突判定。

### 4.2 `search` 的表达式

语法沿用现有那一套（`api/app/notes.py` 的 `parse_query` / `search_row`）：

```
term / "短语" / #标签 / title:词 / path:词 / tag:词
-原子 / not / AND / OR / 括号 / orderby … / limit N
```

插件必须跑通 `api/tests/test_notes.py` 里那批 `parse_query` 用例（纯函数、与存储无关），
保证"同一个表达式在两边命中同一批"。**不允许**插件另发明一套语法。

### 4.3 写入的三条纪律

1. `baseHash` 不匹配 → `conflict`，附当前 `hash` 与 `mtime`；**不覆盖**。
2. `replace` 的 `old` 必须逐字匹配；出现多次且没给 `all` → `conflict` + `occurrences`
   （最多 5 处上下文）。与现有 `edit_note` 完全同语义。
3. 写入走 Obsidian 的 `vault.process(path, fn)`，frontmatter 原样字节保留。

---

## 五、配置：用户只填一样东西

设置 → Obsidian：

| 字段 | 例 | 说明 |
|---|---|---|
| **vault 路径** | `F:\Notes` 或 `/mnt/f/Notes` | 填哪个都行，后端归一化 |

归一化规则：

- `F:\Notes` / `F:/Notes` → `/mnt/f/Notes`（WSL 视角）；
- 已经是 `/mnt/...` 的原样用；
- 相对路径拒绝（必须绝对）；
- `~` 展开；
- 结尾斜杠去掉。

路径只做两件事：读 `bridge.json`、定位信箱。**不直接读笔记正文** —— 没有 Obsidian 就没有笔记
（用户："如果没有配合 obsidian，笔记功能就无效"）。

保存即生效（写进 `app_settings.data.obsidian.vaultPath`，与 AI / 搜索配置同一套）。

---

## 六、与 QFStudio 的接入

现有的五个工具只调 `notelib`。接入方式是**同形替换**：

```python
# api/app/vault.py —— 笔记的唯一入口（只有 Obsidian 一条路，不降级）
def libraries() -> list[Library]
def library(name: str) -> Library
def read_note(lib, rel) -> dict
def tree(lib) -> dict
def search(query, *, lib_name="", limit=50) -> list[dict]
def write_body(lib, rel, body, *, why) -> dict
def create_note(lib, folder, title, kind="note") -> dict
class NoteError / NoteNotFound      # 与 notelib 同名，工具的 except 不用改
```

- 工具层把 `from . import notelib` 换成 `from . import vault as notelib`，**业务代码一行不改**。
- `vault` 需要 db（读设置）：工具函数都收了 `db`，在入口 `vault.configure(db)` 一次即可。
- `notelib` 仍留着：它是 bridge 不可用时的**规格参照**（返回形状、表达式语义），也是
  `pipeline/note_suggest.py` 的实现依赖；工具层不再直接用它。

错误映射（工具层看到的是人话）：

| 情形 | 工具返回 |
|---|---|
| 没填 vault 路径 | `{"error": "还没配 Obsidian：设置 → Obsidian，填 vault 路径"}` |
| bridge.json 不在 / heartbeat 过期 | `{"error": "Obsidian 没在跑，或桥接插件没启用"}` |
| 超时 | `{"error": "Obsidian 没在 5 秒内回应（它可能正忙，或插件没在监听）"}` |
| 路径越界 | `{"error": "路径越界（必须在 vault 内）"}` |
| 写入冲突 | `{"error": "文件在读取后被改过，请重读再改", "hash": "…"}` |

---

## 七、安全与边界

- [ ] 路径双重校验：相对路径、拒绝 `..` / 绝对路径 / 盘符 / `\0`；`resolve()` 后仍在 vault 内。
- [ ] 默认不读不写 `.obsidian/`、`.trash/`、`.quizforge-bridge/`（信箱自身除外）。
- [ ] 返回给模型的正文**原样当文本**，不执行、不渲染其中的 HTML/脚本。
- [ ] 信箱目录里只有 JSON 与 heartbeat，不含 token —— 文件信箱**没有密钥**这一层，
      安全边界就是"能读这个目录的人"，与能读笔记的人是同一批。
- [ ] 写入不额外留副本：Obsidian 自己的 file recovery 就是历史。
- [ ] 错误信息只给 vault 相对路径，不给绝对路径。

---

## 八、已知的边界情况（实现时要处理）

1. **Obsidian 没开** → heartbeat 过期 → 明确报错，不排队。
2. **vault 在同步盘（OneDrive/iCloud）** → 信箱文件会被同步服务碰到；所以：文件小、处理完立刻删、
   陈旧文件 60 秒清一次；`bridge.json` 里带 `hostPid`，pid 不匹配视为残留。
3. **drvfs 的元数据** → `rename` 原子性够用；**不要**依赖文件锁（`fcntl` 在 drvfs 上不可靠）。
4. **`fs.watch` 漏事件**（Windows 上偶发）→ 插件除 watch 外，每 200ms 扫一次 inbox 兜底。
5. **时钟** → 不用绝对时间判定顺序，只用 `id` 与文件出现与否；`heartbeat` 的 mtime 只用来判"最近 5 秒"。
6. **巨大正文** → 单文件上限 1MB；超过就分块（`startChar` 已经有这个能力）。
7. **vault 改名/移动** → 设置里的路径失效 → 报"找不到 vault"，让用户重填。
8. **同一 vault 开两个 Obsidian 窗口** → 两个插件实例会抢信箱；用 `bridge.json` 里的 pid +
   heartbeat 只认**最新**那个（旧的 5 秒内没 heartbeat 就自然失效）。

---

## 九、实现顺序

1. 插件：建信箱、写 `bridge.json` + heartbeat、处理 `ping` / `read` / `tree`。
2. QFStudio：`api/app/vault.py`（配置归一化 + 信箱客户端）+ 工具层换入口。
3. `search`（表达式语义两边对齐，跑同一组用例）。
4. `write` / `append` / `replace`（`baseHash` 冲突）。
5. `open` / `trash`（锦上添花）。

---

## 十、明确不做

- 不做网络传输（HTTP / WebSocket / 命名管道都不做）—— 见 §一。
- 不做本地文件降级：**本插件**不做 —— 没有 Obsidian，它就没有文件可读。自 2026-11-09 起，
  QF 侧在桥不可用时会退到 `notes` 云副本，那是**另一条链路**（见
  [`Obsidian云同步协议.md`](Obsidian云同步协议.md)）；插件 A 的行为一行不变。
- 不把 vault 内容写进 QFStudio 的数据库或快照：**本插件不写**。云副本（`notes` 表）
  由另一个插件 `quizforge-sync` 与 QF 的同步通道维护，同样见云同步协议。
- 不做双向同步、不做后台索引：每次请求现读，需要搜索时由插件用它的 `metadataCache` 回答。
  云同步是**另一个插件**的职责，与本协议无关。
