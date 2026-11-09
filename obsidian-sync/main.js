'use strict';

/**
 * QFStudio Sync —— 把 vault 与 QFStudio 的云端笔记副本对齐。
 *
 * 这是**第二个**插件，与 `quizforge-bridge` 没有任何调用关系：
 *
 * * `quizforge-bridge`：QF ↔ 本地文件（文件信箱，不走网络）；
 * * `quizforge-sync`（本插件）：本地文件 ↔ 云端（Turso，端到端加密）。
 *
 * 两者只通过"文件"与 vault 事件间接配合：本插件看见 bridge 写进来的改动，
 * 就当普通编辑推上云；反过来，拉下来的改动落成文件，bridge 也读得到。
 *
 * 云端的身份是 `notes` 表的一行（`tbl='notes'`、`pk='["路径"]'`），加密格式
 * 与 `api/app/sync.py` 逐字节一致（见 `docs/Obsidian云同步协议.md`）。
 * `sha16` / `splitFront` / 加密封装 / payload 字段名，一处都不能自作主张 ——
 * 分叉的后果不是报错，而是 QF 那边解出来是乱码或永远判成"改过"。
 */

const { Plugin, PluginSettingTab, Setting, Notice, TFile, requestUrl } = require('obsidian');

// #region codec
// 口径与加解密：**与 `api/app/sync.py` 逐字节一致**的那一层。
//
// 为什么整段塞在 main.js 里、而不是单独一个 `codec.js`：Obsidian 加载插件时
// 用 `require(<插件目录>/main.js)`，模块内部的相对 `require('./codec.js')` 会
// 按 Obsidian 自己的模块根解析 —— 实测报 "Failed to load plugin"。社区插件的
// 通行做法就是只管一个 main.js。
//
// `selftest.mjs` 按这两个标记把本段**原样截出来**执行（不是抄一份），所以
// `api/tests/test_plugin_codec.py` 验的仍然是这里真正跑的那份代码。
const {
  createCipheriv,
  createDecipheriv,
  createHash,
  createHmac,
  randomBytes,
  scryptSync,
} = require('crypto');

const TBL = 'notes';
const AAD_PREFIX = 'quizforge-sync:v1:';
const KEY_CHECK_TEXT = 'quizforge-sync-v1';

/** 与 `obsidian-bridge/main.js`、`notecloud.py` 同一个算法：sha256(utf8) 前 16 位。 */
function sha16(text) {
  return createHash('sha256').update(String(text || ''), 'utf8').digest('hex').slice(0, 16);
}

/** 与 bridge 的 `splitFront` 同一条正则（不做 YAML 解析，坏头也当正文）。 */
const FRONT_RE = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/;

function splitFront(text) {
  const raw = String(text || '');
  const match = FRONT_RE.exec(raw);
  if (!match) return { head: '', body: raw };
  return { head: raw.slice(0, match[0].length), body: raw.slice(match[0].length) };
}

/** 库内相对路径：归一斜杠、挡越界（与 bridge 的 `fileOf` 同判据）。 */
function cleanPath(rel) {
  const text = String(rel || '').replace(/\\/g, '/').replace(/^\/+/, '');
  const parts = text.split('/').filter((part) => part && part !== '.');
  if (!parts.length) throw new Error('路径不能为空');
  if (parts.some((part) => part === '..')) throw new Error('路径越界（必须在库内）');
  return parts.join('/');
}

/** SQLite/Python 认的时间串：`YYYY-MM-DD HH:MM:SS.mmm`（UTC）。 */
function sqlTime(date = new Date()) {
  return date.toISOString().replace('T', ' ').replace('Z', '');
}

function deriveKey(passphrase, salt) {
  return scryptSync(String(passphrase), Buffer.from(salt), 32, {
    N: 32768,
    r: 8,
    p: 1,
    // N=32768、r=8 需要约 33.5MB，Node 默认上限 32MB，会抛
    // ERR_CRYPTO_INVALID_SCRYPT_PARAMS —— 必须显式抬高
    maxmem: 64 * 1024 * 1024,
  });
}

function keyCheck(key) {
  return createHmac('sha256', key).update(KEY_CHECK_TEXT).digest('base64');
}

function aadFor(pkText) {
  return Buffer.from(AAD_PREFIX + TBL + ':' + pkText, 'utf8');
}

/** `v1:` + base64(nonce12 + 密文 + tag16)。nonce 可注入，只为对拍。 */
function sealWithNonce(key, pkText, data, nonce) {
  const raw = Buffer.from(JSON.stringify(data), 'utf8');
  const cipher = createCipheriv('aes-256-gcm', key, nonce);
  cipher.setAAD(aadFor(pkText));
  const body = Buffer.concat([cipher.update(raw), cipher.final()]);
  return 'v1:' + Buffer.from(Buffer.concat([nonce, body, cipher.getAuthTag()])).toString('base64');
}

function seal(key, pkText, data) {
  return sealWithNonce(key, pkText, data, randomBytes(12));
}

function unseal(key, pkText, payload) {
  const raw = String(payload || '');
  const blob = Buffer.from(raw.startsWith('v1:') ? raw.slice(3) : raw, 'base64');
  if (blob.length < 28) throw new Error('密文太短');
  const nonce = blob.subarray(0, 12);
  const tag = blob.subarray(blob.length - 16);
  const body = blob.subarray(12, blob.length - 16);
  const decipher = createDecipheriv('aes-256-gcm', key, nonce);
  decipher.setAAD(aadFor(pkText));
  decipher.setAuthTag(tag);
  return JSON.parse(Buffer.concat([decipher.update(body), decipher.final()]).toString('utf8'));
}

function endpointFor(url) {
  let text = String(url || '').trim().replace(/\/+$/, '');
  if (!text) throw new Error('还没填 Turso 地址');
  if (text.startsWith('libsql://')) text = 'https://' + text.slice('libsql://'.length);
  else if (!/^https?:\/\//i.test(text)) text = 'https://' + text;
  if (!/\/v2\/pipeline$/i.test(text)) text += '/v2/pipeline';
  return text;
}
// #endregion codec

const SCHEMA_VERSION = '1';

/** 不同步的目录：插件配置、回收站、bridge 信箱、git。 */
const SKIP = ['.obsidian/', '.trash/', '.quizforge-bridge/', '.git/'];

/** 已同步到 `__deleted__` 的路径：用来区分"没同步过"和"同步过、后来删了"。 */
const DELETED = '__deleted__';

const PUSH_BATCH = 40; // 一个 pipeline 里的语句数（rows + log 各一条 = 每个文件 2 条）
const PULL_PAGE = 100;

//: 本地同步记录的版本。**改它 = 强制重推**：旧版本写下的 `synced` 可能是错的
//: （0.1.0 那版在远端确认之前就标了"已同步"，导致推 0 但云端一行没有）。
const SETTINGS_VERSION = 2;

const DEFAULTS = {
  url: '',
  token: '',
  passphrase: '',
  device: '',
  interval: 60,
  cursor: 0,
  synced: {},
  v: SETTINGS_VERSION,
};

// ============================================================ Turso（Hrana over HTTP）

function argOf(value) {
  if (value === null || value === undefined) return { type: 'null' };
  if (typeof value === 'boolean') return { type: 'integer', value: value ? '1' : '0' };
  if (typeof value === 'number') {
    // Hrana 的两种数字编码**不一样**：integer 的 value 是字符串（i64 超出 JS
    // 安全整数，必须用字符串传），float 的 value 是 JSON 数字。写成字符串会被
    // sqld 拒绝："JSON parse error: invalid type: string \"1794230811.385\""。
    return Number.isInteger(value)
      ? { type: 'integer', value: String(value) }
      : { type: 'float', value };
  }
  return { type: 'text', value: String(value) };
}

function cellOf(cell) {
  if (!cell || cell.type === 'null') return null;
  return cell.value;
}

function textOf(cell) {
  const value = cellOf(cell);
  return value === null ? '' : String(value);
}

function numOf(cell) {
  const value = cellOf(cell);
  return value === null ? 0 : Number(value);
}

class Turso {
  constructor(url, token) {
    this.endpoint = endpointFor(url);
    this.token = String(token || '').trim();
  }

  async call(requests) {
    const response = await requestUrl({
      url: this.endpoint,
      method: 'POST',
      headers: {
        Authorization: 'Bearer ' + this.token,
        'Content-Type': 'application/json',
      },
      // Hrana 的 pipeline **必须以 close 收尾**（与 `sync.py` 的 TursoClient 同）：
      // 少了它，服务端会返回一个空的 results，报错会变成莫名其妙的"没有返回结果"。
      body: JSON.stringify({ requests: [...requests, { type: 'close' }] }),
      throw: false,
    });
    if (response.status >= 400) {
      const detail = (response.text || '').slice(0, 300);
      throw new Error(`Turso HTTP ${response.status}：${detail}`);
    }
    let data = null;
    try {
      data = response.json;
    } catch (err) {
      throw new Error('Turso 返回的不是 JSON（地址或 token 可能不对）');
    }
    const results = (data && data.results) || [];
    for (const one of results) {
      if (one && one.type === 'error') {
        throw new Error('远端执行失败：' + ((one.error && one.error.message) || '未知原因'));
      }
    }
    return results;
  }

  async exec(sql, args = []) {
    const results = await this.call([
      { type: 'execute', stmt: { sql, args: args.map(argOf) } },
    ]);
    const one = results[0];
    if (!one || one.type !== 'ok') throw new Error('远端没有返回结果');
    const response = one.response || {};
    if (response.type !== 'execute') throw new Error('远端返回了非 execute 结果');
    return response.result || { cols: [], rows: [] };
  }

  /** 多条语句：分块发，避免一个 pipeline 过大被网关截断。 */
  async batch(statements) {
    let start = 0;
    while (start < statements.length) {
      const chunk = statements.slice(start, start + PUSH_BATCH);
      const results = await this.call(
        chunk.map((item) => ({
          type: 'execute',
          stmt: { sql: item.sql, args: (item.args || []).map(argOf) },
        }))
      );
      // 结果条数对不上 = 服务端没有逐条执行（早先少了 close 帧时就是空数组）。
      // 不检查的话 `batch` 会"成功"返回，而一行都没写进去 —— 更糟的是调用方
      // 会把 `synced` 标成已同步，之后再也不会重试。
      if (results.length < chunk.length) {
        throw new Error(`远端只回了 ${results.length} 条结果（发了 ${chunk.length} 条）`);
      }
      start += PUSH_BATCH;
    }
  }
}

// ============================================================ 插件

module.exports = class QFStudioSync extends Plugin {
  async onload() {
    await this.loadSettings();
    this.addSettingTab(new SyncSettingTab(this.app, this));

    this.statusEl = this.addStatusBarItem();
    this.busy = false;
    this.pending = false;
    this.timer = null;
    this.debounce = null;

    // vault 事件：自己写回去的那一笔会被 `synced` 的 hash 认出来，不会回环
    this.registerEvent(this.app.vault.on('modify', () => this.schedule()));
    this.registerEvent(this.app.vault.on('create', () => this.schedule()));
    this.registerEvent(this.app.vault.on('delete', () => this.schedule()));

    this.restartTimer();
    // 布局就绪再跑第一轮：太早读不到文件列表
    this.app.workspace.onLayoutReady(() => setTimeout(() => this.tick('启动'), 3000));
    this.setStatus('待命');
  }

  onunload() {
    if (this.timer) clearInterval(this.timer);
    if (this.debounce) clearTimeout(this.debounce);
  }

  async loadSettings() {
    const saved = (await this.loadData()) || {};
    this.settings = Object.assign({}, DEFAULTS, saved);
    this.settings.synced = Object.assign({}, saved.synced || {});
    if (!this.settings.device) {
      this.settings.device = 'obsidian-' + randomBytes(4).toString('hex');
    }
    if (Number(saved.v || 0) < SETTINGS_VERSION) {
      // 旧版本的同步记录不可信：0.1.0 在远端确认之前就写 `synced`，
      // 结果"推 0 / 云端 0 行"卡死。清空重推一次，代价只是把 97 篇重发一遍。
      this.settings.synced = {};
      this.settings.cursor = 0;
      this.settings.v = SETTINGS_VERSION;
    }
    await this.saveSettings();
  }

  async saveSettings() {
    await this.saveData(this.settings);
  }

  restartTimer() {
    if (this.timer) clearInterval(this.timer);
    const seconds = Math.max(15, Number(this.settings.interval) || 60);
    this.timer = setInterval(() => this.tick('定时'), seconds * 1000);
  }

  /** vault 一变就排一轮，但 2 秒内的连续保存只跑一次（编辑器每次按键都会 modify）。 */
  schedule() {
    if (this.debounce) clearTimeout(this.debounce);
    this.debounce = setTimeout(() => {
      this.debounce = null;
      this.tick('文件变化');
    }, 2000);
  }

  configured() {
    return Boolean(this.settings.url && this.settings.token && this.settings.passphrase);
  }

  setStatus(text, bad = false) {
    this.statusText = text;
    if (this.statusEl) this.statusEl.setText('QF 同步：' + text);
    if (bad && text !== this.lastBad) {
      this.lastBad = text;
      new Notice('QF 同步：' + text, 6000);
    }
  }

  async tick(reason) {
    if (!this.configured()) {
      this.setStatus('未配置');
      return;
    }
    if (this.busy) {
      this.pending = true;
      return;
    }
    this.busy = true;
    try {
      const result = await this.syncOnce();
      // 带上"库内 N 篇"：只写增量的话，稳定之后永远是"推 0 / 拉 0"，
      // 看不出到底扫到了没有（实测被问过一次"它显示推0拉0"）。
      this.setStatus(
        `✓ 推 ${result.pushed} / 拉 ${result.pulled} · 库内 ${result.scanned} 篇（${reason}）`
      );
      this.lastBad = '';
    } catch (err) {
      const message = String((err && err.message) || err);
      console.error('[qf-sync] 同步失败', err);
      this.setStatus('失败：' + message.slice(0, 80), true);
    } finally {
      this.busy = false;
      if (this.pending) {
        this.pending = false;
        this.tick('排队');
      }
    }
  }

  async syncOnce() {
    const remote = new Turso(this.settings.url, this.settings.token);
    const key = await this.ensureKey(remote);
    const pulled = await this.pull(remote, key);
    const { pushed, scanned } = await this.push(remote, key);
    // 留在 data.json 里：状态栏只显示最近一轮，翻旧账（"上一轮到底推了没"）靠它
    this.settings.lastRun = { at: sqlTime(), pulled, pushed, scanned };
    await this.saveSettings();
    return { pulled, pushed, scanned };
  }

  /** 读远端盐、派生 key、核对 check；远端还没有盐时由本插件首次写入。 */
  async ensureKey(remote) {
    let meta = await this.remoteMeta(remote);
    const passphrase = String(this.settings.passphrase || '');
    if (!passphrase) throw new Error('还没填口令（config/snapshot.key 里那一串）');

    let saltB64 = meta.salt || '';
    if (!saltB64) {
      const salt = randomBytes(16);
      const key = deriveKey(passphrase, salt);
      await remote.batch([
        {
          sql: "INSERT OR IGNORE INTO qf_sync_meta(k, v) VALUES('salt', ?),('check', ?),('version', ?)",
          args: [Buffer.from(salt).toString('base64'), keyCheck(key), SCHEMA_VERSION],
        },
      ]);
      meta = await this.remoteMeta(remote);
      saltB64 = meta.salt || '';
      if (!saltB64) throw new Error('远端没记住刚写进去的盐，稍后重试');
    }

    const salt = Buffer.from(saltB64, 'base64');
    const key = deriveKey(passphrase, salt);
    if (meta.check && meta.check !== keyCheck(key)) {
      throw new Error('口令与远端不是同一份（校验值对不上）');
    }
    return key;
  }

  async remoteMeta(remote) {
    const result = await remote.exec('SELECT k, v FROM qf_sync_meta');
    const meta = {};
    for (const row of result.rows || []) {
      meta[textOf(row[0])] = textOf(row[1]);
    }
    return meta;
  }

  origin() {
    return String(this.settings.device || 'obsidian');
  }

  /** 单调递增的 at：同一秒内多次写也不会撞版本。 */
  nextAt() {
    const now = Date.now() / 1000;
    const last = Number(this.settings.lastAt) || 0;
    const at = Math.max(now, last + 0.001);
    this.settings.lastAt = at;
    return at;
  }

  skip(path) {
    const text = String(path || '');
    if (SKIP.some((prefix) => text.startsWith(prefix))) return true;
    // 其它隐藏目录（`.foo/`）也不碰：它们不是笔记
    return text.split('/').some((part, index, all) => index < all.length - 1 && part.startsWith('.'));
  }

  // ------------------------------------------------------------ 推

  async push(remote, key) {
    const files = this.app.vault.getMarkdownFiles().filter((file) => !this.skip(file.path));
    const present = new Set(files.map((file) => file.path));
    const statements = [];
    // **先攒着，等远端确认收下再改 `synced`。** 反过来写会有一个很坏的后果：
    // 上传失败（比如一开始那个 400）时 `synced` 已经被标成"同步过了"，下一轮
    // 直接跳过 —— 云端一行没有、插件却永远显示"推 0 / 拉 0"，而且再也推不上去。
    const applied = [];
    let pushed = 0;

    const queueRow = (path, at, deleted, payload) => {
      statements.push({
        sql:
          'INSERT INTO qf_sync_rows(tbl, pk, at, origin, deleted, enc, payload)' +
          ' VALUES(?, ?, ?, ?, ?, ?, ?)' +
          ' ON CONFLICT(tbl, pk) DO UPDATE SET at = excluded.at, origin = excluded.origin,' +
          ' deleted = excluded.deleted, enc = excluded.enc, payload = excluded.payload',
        args: [TBL, JSON.stringify([path]), at, this.origin(), deleted, 1, payload],
      });
      statements.push({
        sql: 'INSERT INTO qf_sync_log(at, origin, tbl, pk, deleted, enc, payload) VALUES(?, ?, ?, ?, ?, ?, ?)',
        args: [at, this.origin(), TBL, JSON.stringify([path]), deleted, 1, payload],
      });
    };

    // 本地删了 → 墓碑（`__deleted__` 标记防回推）
    for (const path of Object.keys(this.settings.synced)) {
      if (present.has(path)) continue;
      const known = this.settings.synced[path];
      if (known && known.hash === DELETED) continue;
      const at = this.nextAt();
      queueRow(path, at, 1, null);
      applied.push({ path, hash: DELETED, at });
      pushed += 1;
    }

    for (const file of files) {
      const raw = await this.app.vault.read(file);
      const body = splitFront(raw).body;
      const hash = sha16(body);
      const known = this.settings.synced[file.path];
      if (known && known.hash === hash) continue;

      const at = this.nextAt();
      const row = {
        path: file.path,
        body,
        hash,
        mtime: Number(file.stat && file.stat.mtime ? file.stat.mtime : Date.now()) / 1000,
        size: Buffer.byteLength(body, 'utf8'),
        updated_at: sqlTime(),
      };
      const pkText = JSON.stringify([file.path]);
      queueRow(file.path, at, 0, seal(key, pkText, row));
      applied.push({ path: file.path, hash, at });
      pushed += 1;
    }

    if (statements.length) await remote.batch(statements);
    for (const one of applied) this.settings.synced[one.path] = { hash: one.hash, at: one.at };
    if (pushed) await this.saveSettings();
    return { pushed, scanned: files.length };
  }

  // ------------------------------------------------------------ 拉

  async pull(remote, key) {
    let cursor = Number(this.settings.cursor) || 0;
    let pulled = 0;

    for (;;) {
      const result = await remote.exec(
        'SELECT pk, at, deleted, enc, payload FROM qf_sync_rows WHERE tbl = ? AND at > ?' +
          ' ORDER BY at LIMIT ?',
        [TBL, cursor, PULL_PAGE]
      );
      const rows = result.rows || [];
      if (!rows.length) break;

      for (const row of rows) {
        const pkText = textOf(row[0]);
        const at = numOf(row[1]);
        const deleted = numOf(row[2]) === 1;
        const enc = numOf(row[3]) === 1;
        const payload = textOf(row[4]);
        if (at > cursor) cursor = at;

        let path = '';
        try {
          path = cleanPath((JSON.parse(pkText) || [])[0] || '');
        } catch (err) {
          console.warn('[qf-sync] 跳过坏主键', pkText);
          continue;
        }
        if (this.skip(path)) continue;

        if (deleted) {
          await this.applyDelete(path, at);
          pulled += 1;
          continue;
        }
        if (!enc || !payload) continue;
        let data = null;
        try {
          data = unseal(key, pkText, payload);
        } catch (err) {
          console.warn('[qf-sync] 解密失败，跳过', path, err);
          continue;
        }
        await this.applyRemote(data, at);
        pulled += 1;
      }

      if (rows.length < PULL_PAGE) break;
    }

    this.settings.cursor = cursor;
    await this.saveSettings();
    return pulled;
  }

  async applyRemote(data, at) {
    const path = cleanPath(data.path || '');
    const body = String(data.body == null ? '' : data.body);
    const hash = String(data.hash || sha16(body));
    const known = this.settings.synced[path];
    const file = this.app.vault.getAbstractFileByPath(path);

    if (!(file instanceof TFile)) {
      await this.ensureFolder(path);
      await this.app.vault.create(path, body);
      this.settings.synced[path] = { hash, at };
      await this.saveSettings();
      return;
    }

    const raw = await this.app.vault.read(file);
    const local = splitFront(raw);
    const localHash = sha16(local.body);
    if (localHash === hash) {
      this.settings.synced[path] = { hash, at };
      await this.saveSettings();
      return;
    }

    // 本地也改过（或第一次接手但不一致）→ 不覆盖，留一份冲突副本
    if (known && known.hash !== localHash) {
      await this.writeConflict(path, local.head, body);
      this.settings.synced[path] = { hash, at };
      await this.saveSettings();
      return;
    }

    // 本地没改过：只换正文，保留本地 frontmatter
    await this.app.vault.process(file, () => local.head + body);
    this.settings.synced[path] = { hash, at };
    await this.saveSettings();
  }

  async applyDelete(path, at) {
    const known = this.settings.synced[path];
    const file = this.app.vault.getAbstractFileByPath(path);
    if (!(file instanceof TFile)) {
      this.settings.synced[path] = { hash: DELETED, at };
      await this.saveSettings();
      return;
    }
    const raw = await this.app.vault.read(file);
    const localHash = sha16(splitFront(raw).body);
    // 删除同步到达前用户又改过 → 保留文件（在 synced 里记 DELETED，避免回推）
    if (known && known.hash !== DELETED && known.hash !== localHash) {
      this.settings.synced[path] = { hash: DELETED, at };
      await this.saveSettings();
      return;
    }
    await this.app.vault.trash(file, true);
    this.settings.synced[path] = { hash: DELETED, at };
    await this.saveSettings();
  }

  async writeConflict(path, head, remoteBody) {
    const stamp = new Date().toISOString().slice(0, 10);
    const base = path.replace(/\.md$/i, '');
    let target = `${base}.conflict-${stamp}.md`;
    let index = 2;
    while (this.app.vault.getAbstractFileByPath(target)) {
      target = `${base}.conflict-${stamp}-${index}.md`;
      index += 1;
    }
    await this.ensureFolder(target);
    await this.app.vault.create(target, head + remoteBody);
    new Notice(`笔记冲突：云端版本已另存为 ${target}`, 8000);
  }

  async ensureFolder(path) {
    const dir = String(path || '').split('/').slice(0, -1).join('/');
    if (!dir) return;
    if (this.app.vault.getAbstractFileByPath(dir)) return;
    try {
      await this.app.vault.createFolder(dir);
    } catch (err) {
      // 并发创建 / 已存在：当成成功
    }
  }
};

// ============================================================ 设置页

class SyncSettingTab extends PluginSettingTab {
  constructor(app, plugin) {
    super(app, plugin);
    this.plugin = plugin;
  }

  display() {
    const { containerEl } = this;
    containerEl.empty();
    containerEl.createEl('h2', { text: 'QFStudio 云端同步' });

    containerEl.createEl('p', {
      text:
        '把笔记库与 QFStudio 的云端副本对齐。口令就是 QF 那台机器上 config/snapshot.key 的内容；' +
        '三样都填对之后，插件每 60 秒（可改）跑一轮，并在你保存笔记后 2 秒补一轮。',
    });

    new Setting(containerEl)
      .setName('Turso 地址')
      .setDesc('libsql:// 或 https:// 都行，插件会自己补 /v2/pipeline')
      .addText((text) =>
        text
          .setPlaceholder('libsql://xxx.turso.io')
          .setValue(this.plugin.settings.url)
          .onChange(async (value) => {
            this.plugin.settings.url = value.trim();
            await this.plugin.saveSettings();
          })
      );

    new Setting(containerEl)
      .setName('Turso token')
      .addText((text) =>
        text
          .setPlaceholder('eyJ…')
          .setValue(this.plugin.settings.token)
          .onChange(async (value) => {
            this.plugin.settings.token = value.trim();
            await this.plugin.saveSettings();
          })
      );

    new Setting(containerEl)
      .setName('口令')
      .setDesc('与 QF 的 config/snapshot.key 同一串；派生出的密钥只在本机内存里')
      .addText((text) => {
        text.inputEl.type = 'password';
        text
          .setValue(this.plugin.settings.passphrase)
          .onChange(async (value) => {
            this.plugin.settings.passphrase = value.trim();
            await this.plugin.saveSettings();
          });
      });

    new Setting(containerEl)
      .setName('同步间隔（秒）')
      .setDesc('最小 15 秒；文件变化会另外触发一轮')
      .addText((text) =>
        text.setValue(String(this.plugin.settings.interval)).onChange(async (value) => {
          const seconds = Math.max(15, Number(value) || 60);
          this.plugin.settings.interval = seconds;
          await this.plugin.saveSettings();
          this.plugin.restartTimer();
        })
      );

    new Setting(containerEl)
      .setName('设备 ID')
      .setDesc('写进远端行的 origin，用来区分是哪台机器改的')
      .addText((text) => text.setValue(this.plugin.settings.device).setDisabled(true));

    new Setting(containerEl).setName('立即同步').addButton((button) =>
      button.setButtonText('同步一轮').onClick(async () => {
        await this.plugin.tick('手动');
        new Notice('QF 同步：' + (this.plugin.statusText || '已触发'));
      })
    );

    containerEl.createEl('p', {
      text: '状态：' + (this.plugin.statusText || '待命'),
    });
  }
}
