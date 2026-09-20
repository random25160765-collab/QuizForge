# vendored CodeMirror 6

- version: codemirror 6.0.2（@codemirror/view 6.43.12 / state 6.7.5 / language 6.12.4 / lang-markdown 6.5.2）
- build: `node tools/build_cm6.mjs --modules <装了依赖的目录>`（入口 `tools/cm6/entry.js`，esbuild 打成一个 IIFE）
- 产物: cm6.js（577.1 KB，minify 后）
- sha256: 60dc25ecface40a3e1b630345e6abf476bde3f6191b03320178508cc09cfba05
- synced_at: 2026-09-19T21:24

为什么不是像 katex 那样"直接拷一个 dist 文件"：CM6 是若干 ESM-only 的包，而本仓库前端
走全局 `QF` + 经典 `<script>`（没有打包链）。所以内核在**构建期**打一次，产物提交进仓库 ——
日常 `make web` 不需要 node，也不依赖网络。

只由 `tools/build_cm6.mjs` 生成；升级内核时跑它，然后把上面的版本/hash/体积改掉。
