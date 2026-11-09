/* QFStudio Bridge —— 让 QFStudio 的对话读写这个 vault（文件信箱，不走网络）。
 *
 * 协议见仓库里的 docs/Obsidian桥接协议.md。要点：
 *
 *   <vault>/.quizforge-bridge/
 *     bridge.json   我写：协议版本、能力、vault 名
 *     heartbeat     每秒 touch；对方据此判断我在线
 *     inbox/        对方写 req-<id>.json
 *     outbox/       我写 res-<id>.json
 *     tmp/          原子写的中间文件
 *
 * 两条纪律：
 *   1. 写任何文件都先写 tmp、再 rename —— 对方要么看不到，要么看到完整文件；
 *   2. 一次只处理一个请求（单通道）；处理前先看 outbox 里有没有同 id 的响应（重放安全）。
 *
 * 这里用 Node 的 fs 读写信箱，用 Obsidian 的 vault / metadataCache 读写笔记本身
 * （frontmatter、标签、双链都以 Obsidian 的解析为准）。
 */

const { Plugin, Notice } = require('obsidian');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const BOX = '.quizforge-bridge';
const PROTOCOL = 1;
const SKIP = new Set(['.obsidian', '.trash', '.quizforge-bridge', '.git']);
const MD = new Set(['.md', '.markdown']);

function sha16(text) {
  return crypto.createHash('sha256').update(text, 'utf8').digest('hex').slice(0, 16);
}

function splitFront(text) {
  const match = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/.exec(text);
  if (!match) return { head: '', body: text };
  return { head: text.slice(0, match[0].length), body: text.slice(match[0].length) };
}

function outlineOf(body) {
  const rows = [];
  body.split('\n').forEach((line, index) => {
    const m = /^(#{1,6})\s+(.*)$/.exec(line);
    if (m) rows.push({ level: m[1].length, text: m[2].trim(), line: index + 1 });
  });
  return rows;
}

module.exports = class QFStudioBridge extends Plugin {
  async onload() {
    this.base = this.app.vault.adapter.getBasePath
      ? this.app.vault.adapter.getBasePath()
      : null;
    if (!this.base) {
      new Notice('QFStudio Bridge：拿不到 vault 的本地路径（只支持桌面版）。');
      return;
    }
    this.box = path.join(this.base, BOX);
    this.inbox = path.join(this.box, 'inbox');
    this.outbox = path.join(this.box, 'outbox');
    this.tmp = path.join(this.box, 'tmp');
    for (const dir of [this.inbox, this.outbox, this.tmp]) {
      fs.mkdirSync(dir, { recursive: true });
    }
    this.cleanStale();
    this.writeMeta();

    this.busy = false;
    this.watcher = null;
    try {
      this.watcher = fs.watch(this.inbox, () => this.drain());
    } catch (err) {
      /* watch 起不来就只靠轮询 */
    }
    // watch 在 Windows 上偶尔漏事件：200ms 扫一遍兜底（目录很小，成本可忽略）
    this.poll = window.setInterval(() => this.drain(), 200);
    this.beat = window.setInterval(() => this.touchHeartbeat(), 1000);
    this.touchHeartbeat();
    new Notice('QFStudio Bridge 已启用');
  }

  onunload() {
    if (this.poll) window.clearInterval(this.poll);
    if (this.beat) window.clearInterval(this.beat);
    if (this.watcher) {
      try { this.watcher.close(); } catch (err) { /* 忽略 */ }
    }
    try { fs.unlinkSync(path.join(this.box, 'bridge.json')); } catch (err) { /* 已经没了 */ }
    try { fs.unlinkSync(path.join(this.box, 'heartbeat')); } catch (err) { /* 同上 */ }
  }

  // ------------------------------------------------------------------ 信箱

  cleanStale() {
    const now = Date.now();
    for (const dir of [this.inbox, this.outbox, this.tmp]) {
      let names = [];
      try { names = fs.readdirSync(dir); } catch (err) { continue; }
      for (const name of names) {
        const file = path.join(dir, name);
        try {
          if (now - fs.statSync(file).mtimeMs > 60_000) fs.unlinkSync(file);
        } catch (err) { /* 别人正在删 */ }
      }
    }
  }

  writeMeta() {
    const meta = {
      protocol: PROTOCOL,
      pluginVersion: this.manifest.version,
      vaultName: this.app.vault.getName(),
      pid: process.pid,
      startedAt: Date.now(),
      capabilities: ['read', 'search', 'write', 'open', 'trash'],
    };
    this.atomicWrite(path.join(this.box, 'bridge.json'), JSON.stringify(meta));
  }

  touchHeartbeat() {
    try {
      fs.writeFileSync(path.join(this.box, 'heartbeat'), String(Date.now()));
    } catch (err) { /* 目录被删了就下一轮重建 */ }
  }

  atomicWrite(target, text) {
    const tmp = path.join(this.tmp, path.basename(target) + '.tmp');
    fs.writeFileSync(tmp, text, 'utf8');
    fs.renameSync(tmp, target);
  }

  drain() {
    if (this.busy) return;
    let names = [];
    try { names = fs.readdirSync(this.inbox); } catch (err) { return; }
    const request = names.filter((n) => n.startsWith('req-') && n.endsWith('.json')).sort()[0];
    if (!request) return;

    this.busy = true;
    const file = path.join(this.inbox, request);
    let payload = null;
    try {
      payload = JSON.parse(fs.readFileSync(file, 'utf8'));
    } catch (err) {
      this.busy = false;
      return; // 对方还在写（它该用 tmp+rename），下一轮再说
    }
    this.handle(payload)
      .then((reply) => {
        const resPath = path.join(this.outbox, `res-${payload.id}.json`);
        // 重放安全：已经有响应就不重复执行
        if (!fs.existsSync(resPath)) {
          this.atomicWrite(resPath, JSON.stringify(reply));
        }
      })
      .catch((err) => {
        this.atomicWrite(
          path.join(this.outbox, `res-${payload.id}.json`),
          JSON.stringify({
            protocol: PROTOCOL,
            id: payload.id,
            ok: false,
            error: { code: 'internal', message: String((err && err.message) || err) },
            ts: Date.now(),
          })
        );
      })
      .then(() => {
        try { fs.unlinkSync(file); } catch (err) { /* 已经没了 */ }
        this.busy = false;
      });
  }

  // ------------------------------------------------------------------ 操作

  async handle(request) {
    const op = String(request.op || '');
    const params = request.params || {};
    const id = request.id;
    const ok = (result) => ({ protocol: PROTOCOL, id, ok: true, result, ts: Date.now() });
    const fail = (code, message) => ({ protocol: PROTOCOL, id, ok: false, error: { code, message }, ts: Date.now() });

    try {
      if (op === 'ping') {
        return ok({
          protocol: PROTOCOL,
          pluginVersion: this.manifest.version,
          vaultName: this.app.vault.getName(),
          noteCount: this.app.vault.getMarkdownFiles().length,
          capabilities: ['read', 'search', 'write', 'open', 'trash'],
        });
      }
      if (op === 'tree') return ok(this.tree());
      if (op === 'read') return ok(await this.readNote(params));
      if (op === 'search') return ok(this.search(params));
      if (op === 'write') return ok(await this.writeNote(params));
      if (op === 'create') return ok(await this.createNote(params));
      if (op === 'open') return ok(await this.openNote(params));
      if (op === 'trash') return ok(await this.trashNote(params));
      return fail('unsupported', `不认识的 op：${op}`);
    } catch (err) {
      const message = String((err && err.message) || err);
      if (/没有这篇|找不到/.test(message)) return fail('not_found', message);
      if (/路径越界|越界/.test(message)) return fail('forbidden', message);
      if (/改过|冲突/.test(message)) return fail('conflict', message);
      return fail('internal', message);
    }
  }

  /** 相对路径 → TFile；顺带做越界校验。 */
  fileOf(rel) {
    const clean = String(rel || '').replace(/\\/g, '/').replace(/^\/+/, '');
    if (!clean || clean.split('/').some((part) => part === '..')) {
      throw new Error('路径越界（必须在 vault 内）');
    }
    if (clean === BOX || clean.startsWith(BOX + '/')) {
      throw new Error('路径越界（信箱不给笔记工具用）');
    }
    const file = this.app.vault.getAbstractFileByPath(clean);
    if (!file) throw new Error(`没有这篇笔记：${clean}`);
    return file;
  }

  tree() {
    const root = { name: this.app.vault.getName(), path: '', dirs: {}, files: [] };
    for (const file of this.app.vault.getMarkdownFiles()) {
      const rel = file.path;
      const parts = rel.split('/');
      let node = root;
      for (const part of parts.slice(0, -1)) {
        if (!node.dirs[part]) {
          node.dirs[part] = { name: part, path: '', dirs: {}, files: [] };
        }
        node = node.dirs[part];
      }
      const cache = this.app.metadataCache.getFileCache(file) || {};
      const fm = cache.frontmatter || {};
      node.files.push({
        path: rel,
        title: String(fm.title || file.basename),
        tags: Array.isArray(fm.tags) ? fm.tags : fm.tags ? [String(fm.tags)] : [],
        kind: 'note',
      });
    }
    return root;
  }

  async readNote(params) {
    const file = this.fileOf(params.path);
    const text = await this.app.vault.read(file);
    const { body } = splitFront(text);
    const cache = this.app.metadataCache.getFileCache(file) || {};
    const fm = cache.frontmatter || {};

    const start = Math.max(0, Number(params.startChar || 0));
    // 默认给**全文**：工具层（tools.read_note）自己按 startChar/maxChars 切。
    // 这一层再切一次，会让 startChar 生效两遍、chars 也变成切片长度（实测踩过）。
    const want = Number(params.maxChars || 0);
    const limit = want > 0 ? Math.max(200, Math.min(100000, want)) : body.length || 1;
    const chunk = body.slice(start, start + limit);
    const tail = start + chunk.length < body.length ? start + chunk.length : 0;

    const needles = new Set(
      (cache.links || [])
        .map((link) => String(link.link || '').split('#')[0].split('|')[0].trim())
        .filter(Boolean)
    );
    const backlinks = [];
    for (const other of this.app.vault.getMarkdownFiles()) {
      if (other.path === file.path) continue;
      const otherCache = this.app.metadataCache.getFileCache(other) || {};
      const hit = (otherCache.links || []).some((link) => {
        const target = String(link.link || '').split('#')[0].split('|')[0].trim();
        return target === file.basename || target === file.path.replace(/\.md$/, '');
      });
      if (hit) backlinks.push({ path: other.path, title: other.basename });
    }

    return {
      lib: this.app.vault.getName(),
      path: file.path,
      kind: 'note',
      title: String(fm.title || file.basename),
      meta: fm,
      tags: Array.isArray(fm.tags) ? fm.tags : fm.tags ? [String(fm.tags)] : [],
      body: chunk,
      chars: body.length,
      startChar: start,
      truncated: tail > 0,
      nextChar: tail,
      hash: sha16(body),
      mtime: new Date(file.stat.mtime).toISOString().slice(0, 19),
      outline: outlineOf(body),
      backlinks,
      outlinks: [...needles].map((target) => ({ target, resolved: '' })),
    };
  }

  search(params) {
    const query = String(params.query || '').trim();
    const limit = Math.max(1, Math.min(50, Number(params.limit || 12)));
    const words = query.split(/\s+/).filter(Boolean);
    const tags = words.filter((w) => w.startsWith('#')).map((w) => w.slice(1).toLowerCase());
    const titles = words.filter((w) => w.startsWith('title:')).map((w) => w.slice(6).toLowerCase());
    const plain = words
      .filter((w) => !w.startsWith('#') && !w.startsWith('title:') && !w.startsWith('-'))
      .map((w) => w.toLowerCase())
      .filter((w) => !['and', 'or', 'not'].includes(w));

    const items = [];
    for (const file of this.app.vault.getMarkdownFiles()) {
      const cache = this.app.metadataCache.getFileCache(file) || {};
      const fm = cache.frontmatter || {};
      const title = String(fm.title || file.basename);
      const ownTags = (Array.isArray(fm.tags) ? fm.tags : fm.tags ? [fm.tags] : []).map((t) => String(t).toLowerCase());
      if (titles.length && !titles.every((w) => title.toLowerCase().includes(w))) continue;
      if (tags.length && !tags.every((t) => ownTags.includes(t))) continue;

      let body = '';
      try {
        // 搜索要正文：Obsidian 的 metadataCache 不带正文，只能读。按需读，命中即止。
        body = splitFront(fs.readFileSync(path.join(this.base, file.path), 'utf8')).body;
      } catch (err) {
        body = '';
      }
      const hay = (title + '\n' + body).toLowerCase();
      if (plain.length && !plain.every((w) => hay.includes(w))) continue;
      const line = body.split('\n').find((row) => row.trim()) || '';
      items.push({
        lib: this.app.vault.getName(),
        path: file.path,
        title,
        snippet: line.trim().slice(0, 120),
        mtime: new Date(file.stat.mtime).toISOString().slice(0, 19),
      });
      if (items.length >= limit) break;
    }
    return { items, count: items.length };
  }

  async writeNote(params) {
    const file = this.fileOf(params.path);
    const body = String(params.body == null ? '' : params.body);
    const current = await this.app.vault.read(file);
    const { head, body: oldBody } = splitFront(current);
    const base = String(params.baseHash || '');
    if (base && sha16(oldBody) !== base) {
      throw new Error('文件在读取后被改过，请重读再改');
    }
    const merged = head + body;
    if (this.app.vault.process) {
      await this.app.vault.process(file, () => merged);
    } else {
      await this.app.vault.modify(file, merged);
    }
    return { path: file.path, hash: sha16(body), chars: body.length };
  }

  async createNote(params) {
    // 两种给法：`path` 是完整相对路径（edit_note 写到新文件时用这个，
    // 目录也一并建）；`folder` + `title` 是老的写法。
    const folder = String(params.folder || '').replace(/^\/+|\/+$/g, '');
    const title = String(params.title || '').trim() || '未命名';
    const rel = params.path
      ? String(params.path).replace(/\\/g, '/').replace(/^\/+/, '')
      : (folder ? folder + '/' : '') + title + '.md';
    if (!rel || rel.split('/').some((part) => part === '..')) {
      throw new Error('路径越界（必须在 vault 内）');
    }
    if (this.app.vault.getAbstractFileByPath(rel)) {
      throw new Error(`已经有这一篇：${rel}`);
    }
    const dir = rel.includes('/') ? rel.slice(0, rel.lastIndexOf('/')) : '';
    if (dir && !this.app.vault.getAbstractFileByPath(dir)) {
      await this.app.vault.createFolder(dir);
    }
    await this.app.vault.create(rel, String(params.body || ''));
    return { path: rel };
  }

  async openNote(params) {
    const file = this.fileOf(params.path);
    const leaf = this.app.workspace.getLeaf(false);
    await leaf.openFile(file, { eState: { line: Number(params.line || 0) } });
    return { ok: true };
  }

  async trashNote(params) {
    const file = this.fileOf(params.path);
    await this.app.vault.trash(file, true);
    return { ok: true };
  }
};
