# TikZJax（离线副本）

* 整包来自 npm **`@rod2ik/tikzjax@1.6.0`** 的 `dist/`（7.1MB）：
  `tikzjax.js`（入口，认 `text/tikz`）、`run-tex.js`（引擎）、`core.dump.gz`、
  `tex.wasm.gz`、`tex_files/`（245 个宏包，**按需取** —— 里面有 `circuitikz.sty.gz`、
  `tikz-cd.sty.gz`）、`fonts/`、`fonts.css`。
* 取件方式：`curl -sSL --http1.1 -o pkg.tgz https://registry.npmjs.org/@rod2ik/tikzjax/-/tikzjax-1.6.0.tgz`
  （**必须 `--http1.1`**：本机 DNS 把外网指到 198.18.0.10 的本地代理，HTTP/2 会超时）。
  解包取 `package/dist/` 下的东西，**平铺**放进 `vendor/tikzjax/` ——
  它按**相对自己的路径**找 `run-tex.js` / `core.dump.gz` / `tex_files/`，目录结构不能改。

## 为什么不是另外两份（都试过）

* `tikzjax.com/v1/tikzjax.js` + 它 S3 上那两个哈希载荷（`.wasm` 598KB、`.gz` 9.79MB）：
  能跑，但**只支持纯 TikZ** —— `circuitikz` / `tikz-cd` / AMScd 一律内部致命错误（`unreachable`）。
  实测四块对照页：纯 TikZ ✓、其余三块 ✗。
* npm `node-tikzjax` 的载荷：格式对不上（`tex_files.tar.gz` 与浏览器包不是一代）。
