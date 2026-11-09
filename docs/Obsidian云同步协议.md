# Obsidian 云同步协议

> 状态：2026-11-09 实现（`obsidian-sync/` 插件 + QF 的 `notes` 表与 `vault` 降级路由）。
> 与 [`Obsidian桥接协议.md`](Obsidian桥接协议.md) 是两件不同的事：
> 那份讲 **QF ↔ 本地文件**（插件 `quizforge-bridge`，不走网络），
> 这份讲 **本地文件 ↔ 云端**（插件 `quizforge-sync`，走 Turso）。

## 一、为什么要有两份协议

一句话：**桌面有 Obsidian，手机没有**。

- 桌面：QF 直连 vault（文件是权威）。云端副本只是给别处用的镜像。
- 手机：没有文件可读，`notes` 表就是它看到的全部。LLM 读它、改它，随现有同步通道上传。

两个插件因此分工：

| 插件 | 负责 | 依赖 |
| --- | --- | --- |
| `quizforge-bridge` | QF ↔ 本地文件（文件信箱） | 无 |
| `quizforge-sync` | 本地文件 ↔ 云端（Turso） | 网络 + Turso token + 口令 |

它们**没有代码调用关系**，只通过"文件"与 vault 事件间接配合：bridge 写进来的改动，
sync 当普通编辑推上云；sync 拉下来的改动落成文件，bridge 读得到。

## 二、数据

### 2.1 本地表（每台机器都有）

```sql
CREATE TABLE notes (
  path       TEXT PRIMARY KEY,   -- vault 内相对路径，如 日记/2026-11-09.md
  body       TEXT NOT NULL,      -- **去掉 frontmatter 的正文**
  hash       TEXT NOT NULL,      -- sha256(body) 十六进制前 16 位
  mtime      REAL NOT NULL,      -- epoch 秒，排序用，不参与冲突判定
  size       INTEGER NOT NULL,   -- 正文字节数（UTF-8）
  updated_at DATETIME NOT NULL
);
```

它是同步表（`datapack.BOUNDARY["notes"] = "user"`），也是加密表
（`sync.PRIVATE_TABLES`）—— 笔记正文与对话同等对待，云端只见到密文。

### 2.2 云端行

复用现有 `qf_sync_rows` / `qf_sync_log`，不新建表：

- `tbl = 'notes'`
- `pk = JSON.stringify([path])`，例如 `["日记/2026-11-09.md"]`
- `at`：版本号（epoch 秒浮点），单调递增
- `origin`：设备 id（插件首次启动生成，如 `obsidian-1a2b3c4d`）
- `deleted`：1 = 墓碑
- `enc = 1`
- `payload = "v1:" + base64(nonce12 + 密文 + tag16)`

**payload 明文是一个 JSON 对象，键名就是列名**（QF 按列名落库）：

```json
{"path":"日记/2026-11-09.md","body":"# 今天\n\n写了点东西。","hash":"6e1256d23acb0f75",
 "mtime":1700000000.5,"size":28,"updated_at":"2026-11-09 12:34:56.789"}
```

`updated_at` 必须是 `YYYY-MM-DD HH:MM:SS[.mmm]`（UTC）—— QF 侧按 SQLAlchemy 的
DateTime 读它，格式不对会读成字符串而不是时间。

## 三、加密

与 `api/app/sync.py` 完全相同，**不要各自发挥**：

```text
口令（`config/settings.local.json` 的 `syncPassphrase`）
  → scrypt(口令, salt, N=2^15, r=8, p=1, dkLen=32) → key
salt 来自云端 qf_sync_meta 的 salt（base64），首次由先同步的一方写入 16 字节随机值
校验：check = base64(HMAC-SHA256(key, "quizforge-sync-v1"))
封装：v1: + base64(nonce(12) + AES-256-GCM 密文 + tag(16))
AAD ：quizforge-sync:v1:notes:<pk>      ← pk 是 JSON 数组字符串，不是路径
明文：紧凑 JSON（ensure_ascii=False, separators=(",",":")）
```

Node 侧两个坑：

1. `crypto.scryptSync` 必须显式 `maxmem: 64 * 1024 * 1024`（N=32768、r=8 约需
   33.5MB，默认上限 32MB 会抛 `ERR_CRYPTO_INVALID_SCRYPT_PARAMS`）。
2. `cipher.getAuthTag()` 是**单独**返回的，拼接顺序必须是
   `nonce + 密文 + tag`，与 Python `AESGCM.encrypt` 的输出对齐。

对拍方式：`node obsidian-sync/selftest.mjs` 输出固定向量，
`api/tests/test_plugin_codec.py` 用 Python 复算并逐字节比对。

## 四、同步规则

### 4.1 推（本地 → 云）

1. 遍历 `vault.getMarkdownFiles()`，跳过 `.obsidian/`、`.trash/`、`.quizforge-bridge/`、
   `.git/` 与其它隐藏目录。
2. `splitFront(raw).body` → `hash = sha16(body)`，与 `synced[path].hash` 相同则跳过。
3. 有变化：`at = max(now, lastAt + 0.001)`，写 `qf_sync_rows`（upsert）与
   `qf_sync_log`（增量），每批 40 条语句。
4. 本地删了（`synced` 里有、文件列表里没有）：推 `deleted=1` 的墓碑，
   并在 `synced` 里记 `__deleted__`，防止下一轮把文件当"新笔记"再推上去。

### 4.2 拉（云 → 本地）

```sql
SELECT pk, at, deleted, enc, payload FROM qf_sync_rows
 WHERE tbl = 'notes' AND at > ? ORDER BY at LIMIT 100
```

游标是本地 `cursor`（上次应用的最大 `at`），分页直到不足一页。

每条按 `path` 落地：

| 本地状态 | 动作 |
| --- | --- |
| 文件不存在 | 新建，写入正文 |
| 存在且 `localHash == remote.hash` | 什么都不做 |
| 存在，`synced[path].hash == localHash`（本地没改过） | 只换正文，**保留本地 frontmatter** |
| 存在，本地也改过 | 写 `xxx.conflict-YYYY-MM-DD.md`，**不覆盖** |

写文件一律走 `app.vault.process`（Obsidian 会串行化，避免与编辑器打架）。
写完立刻把 `path → {hash, at}` 记入 `synced`，所以自己的写入不会触发回推。

### 4.3 删除

- 云端墓碑到达：本地文件若没改过（`synced[path].hash == localHash`）→ `vault.trash`；
  若删除到达前用户又改过 → **保留文件**，只在 `synced` 里记 `__deleted__`（用户后来
  再编辑会让 hash 变化，于是重新推上去，等于"删除后重建"）。
- 首版不做"云端批量清空"的特殊处理：墓碑逐条走。

## 五、首次全量

- 第一台设备：云端为空 → 一轮把 vault 里所有 `.md` 推上去（按 60 秒一轮、每轮推完
  能推的部分；几百篇通常一轮内完成）。
- 第二台设备：先拉。云端有、本地没有 → 新建；两边都有但内容不同 → 冲突副本
  （**保守，不猜谁新**）。
- 盐与口令：先到的一方写 `salt`/`check`，后来者必须用同一口令，否则明确报错。

## 六、触发与失败

- 启动后 3 秒第一轮；之后每 60 秒（可配，最小 15 秒）。
- vault `modify`/`create`/`delete` → 2 秒 debounce 再跑一轮。
- 网络失败只记日志、状态栏显示，下一轮重试；不阻塞 Obsidian 使用。
- 同一时刻只跑一轮（`busy` 标志），期间来的触发合并成一次。

## 七、口径清单（三处必须一致）

| 项 | 规则 |
| --- | --- |
| `sha16(text)` | `sha256(utf8)` 十六进制前 16 位 |
| frontmatter 切分 | 正则 `^---\r?\n([\s\S]*?)\r?\n---\r?\n?`，**不做 YAML 解析**（坏头当正文） |
| 路径 | 正斜杠、无前导 `/`、拒绝 `..`、拒绝 `.quizforge-bridge/` |

实现位置：`obsidian-bridge/main.js`、`obsidian-sync/codec.js`、`api/app/notecloud.py`。

## 八、QF 侧的降级语义

`api/app/vault.py` 每个入口先判桥：

- 桥在（`bridge.json` + 心跳 ≤ 90s）→ 走信箱，桌面行为不变；
- 桥不在 → 转 `notecloud`，读写 `notes` 表。

**边界**：降级时读到的是上次同步的版本，不是实时文件。桌面端永远优先信箱；
`ping` 与 `open_note` 不降级（"检测连接"要如实回答，云端副本打不开 Obsidian 窗口）。

手机端的 HTTP 入口是 `api/app/routers/notes.py`（`/api/notes/*`：库清单 / 目录树 /
读一篇 / 搜索 / 新建 / 写正文），它们只是这一层的同形包装，不另做判断。

桌面端 QF **永远不写** `notes` 表 —— 那边文件是权威，两边都写必然互相覆盖。
