/**
 * 加解密口径的自检：**从 `main.js` 里截出 codec 段**执行，不是抄一份。
 *
 * 固定口令 / 盐 / nonce / 明文，输出一份 JSON；`api/tests/test_plugin_codec.py`
 * 用 Python 侧的 scrypt + AES-GCM 复算同一条向量，两边必须一字不差。
 *
 * 为什么值得单独跑：这段格式（nonce 位置、tag 位置、AAD 里的 pk 是 JSON 数组
 * 的字符串、payload 的键名）任何一处抄错，都不会在插件里报错 —— 只会表现成
 * "QF 那边读不出笔记"或"同步一直说改过"。跨语言对拍是唯一能提前发现的地方。
 *
 * 用法：`node obsidian-sync/selftest.mjs`
 */

import fs from 'node:fs';
import { Buffer } from 'node:buffer';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);

const START = '// #region codec';
const END = '// #endregion codec';

const source = fs.readFileSync(new URL('./main.js', import.meta.url), 'utf8');
const from = source.indexOf(START);
const to = source.indexOf(END);
if (from < 0 || to < 0 || to <= from) {
  console.error('main.js 里找不到 codec 标记（// #region codec … // #endregion codec）');
  process.exit(1);
}

const code =
  source.slice(from, to) +
  '\nreturn { TBL, sha16, splitFront, cleanPath, sqlTime, deriveKey, keyCheck, seal, sealWithNonce, unseal, endpointFor };';
const codec = new Function('require', code)(require);

const PASSPHRASE = 'test-passphrase';
const SALT = Buffer.from(Array.from({ length: 16 }, (_, i) => i));
const NONCE = Buffer.from(Array.from({ length: 12 }, (_, i) => i));
const BODY = '# 今天\n\n写了点东西。';
const PK = JSON.stringify(['日记/2026-11-09.md']);
const DATA = {
  path: '日记/2026-11-09.md',
  body: BODY,
  hash: codec.sha16(BODY),
  mtime: 1700000000.5,
  size: Buffer.byteLength(BODY, 'utf8'),
  updated_at: '2026-11-09 12:34:56.789',
};

const key = codec.deriveKey(PASSPHRASE, SALT);
const payload = codec.sealWithNonce(key, PK, DATA, NONCE);
const roundtrip = codec.unseal(key, PK, payload);

if (JSON.stringify(roundtrip) !== JSON.stringify(DATA)) {
  console.error('往返失败：解开的内容与原文不一致');
  process.exit(1);
}

process.stdout.write(
  JSON.stringify({
    keyHex: key.toString('hex'),
    check: codec.keyCheck(key),
    pk: PK,
    payload,
    plain: JSON.stringify(DATA),
  }) + '\n'
);
