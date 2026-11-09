# Android 打包（Capacitor + Chaquopy）

> 2026-11-08 首次打通。工程与脚本在 `mobile/`，构建说明见 `mobile/README.md`；
> 本文只记录**结论、证据与踩过的坑**，避免下次从头再查一遍。

## 一句话结构

APK 里跑的是**同一份 `api/app`**：Capacitor 的 WebView 加载 `http://127.0.0.1:8731`，
该端口由 APK 内嵌的 CPython（Chaquopy 17.2）用 uvicorn 提供，SQLite 在
`filesDir/quizforge/quizforge.db`。

**题库不随包**（2026-11-09 起）：题目 / 考纲 / 概念 / 点 / 边由 `app/bank_cloud.py`
整包同步，新设备首启是空库、头一轮同步从 Turso 拉下来；材料与切片正文走
`library_cloud.py`。**安装包只放代码、不放数据**：没网就是空库，不另做兜底；
`build-apk.sh` 里有一道闸，`assets/bank.db` 存在就报错退出。

**新设备要拿到口令**：对话 / 记忆 / 计划书是端到端加密的，钥匙是设置文件里的
`syncPassphrase`（`assets/sync/settings.local.json`，首启落到 `filesDir/quizforge/config/`）。
两条路都行：

* 从旧设备**复制设置 JSON**（设置 → 数据 → 复制）—— 里面带 `syncPassphrase`，
  粘到新设备的「导入」就一起带过去了；
* 只填了地址与令牌的，去 设置 → 同步 → 「同步口令」补一次。

没填时材料与题库（明文整包）能同步，但对话**只有标题、没有消息** ——
2026-11-09 手机端"只有对话列表"就是这么来的。眼下新设备首次打开会先进
`welcome.html`（AI → 同步 → 数据密码），每步都能跳过。

## 两道门禁（都实测通过，别再怀疑）

| 门禁 | 结论 | 证据 |
|---|---|---|
| A：Android 上能否 import 后端依赖 | 能 | 模拟器 `files/spike.json`：fastapi 0.141.1 / pydantic 2.14.0 / pydantic_core 2.50.0 / sqlalchemy 2.0.53 / uvicorn 0.53.0 全部 ok |
| B：uvicorn 能否监听回环 | 能 | 第 1 次轮询即返回 `{"ok":true,"platform":"android"}`；`Server.install_signal_handlers` 必须置空（非主线程装信号处理器抛 ValueError） |

## 依赖的四个"没得选"

| 包 | 处置 | 原因 |
|---|---|---|
| pydantic-core | 用 `mobile/wheels/` 里社区预编译的 cp314 Android wheel（2.50.0） | 官方 PyPI 无 Android wheel；2.50.0 是 pydantic 2.14.0 钉死的版本，所以桌面 pydantic 一并升到 2.14.0 |
| cryptography | 换成 `app/crypto_android.py`（javax.crypto + hashlib.scrypt） | chaquo 仓库最高 cp313，本工程是 Python 3.14；桌面继续用 `cryptography`，由 `app/crypto_compat.py` 选后端 |
| PyYAML | 装配时拷纯 Python `yaml/` | PyPI 只有含 libyaml 的平台 wheel，Chaquopy 构建期不能编译 C |
| greenlet | 不装 | sqlalchemy 无条件依赖它，但同步 ORM 用不到；`pip --no-deps` + 手写依赖清单 |

## 端到端加密必须两端逐字节一致

设备启动时写进 `files/quizforge/android_boot.log`：

```
crypto backend=android roundtrip=True key=5a97bd7411ca5496 blob=7e834f27c97e29e14515630c
```

桌面用 `mobile/README.md` 里那段代码对拍，**这两个值必须完全一致**。
实测一致（2026-11-08），并且首次同步从 Turso 拉下并解密了 596 条消息 / 11 个对话。

## 构建与安装

```bash
bash mobile/scripts/bootstrap-toolchain.sh     # 一次性：JDK21 + SDK36 + Gradle（~/.local，免 sudo）
bash mobile/scripts/keystore.sh                # 一次性：自签 keystore（~/.config/quizforge-android）
bash mobile/scripts/build-apk.sh               # 出 release APK
adb install -r mobile/android/app/build/outputs/apk/release/app-release.apk
```

`make apk` / `make apk-debug` / `make apk-sync` 是同一套脚本的入口。

## 踩过的坑（按代价排序）

1. **`gradlew` 在本机下不动 Gradle**：`services.gradle.org` 的 SSL 握手失败（fake-ip）。
   改用腾讯镜像的 zip + 解压版 Gradle（`~/.local/gradle/...`），并把
   `gradle-wrapper.properties` 的 distributionUrl 也换成镜像。
2. **Gradle 拉 Maven 依赖会卡死**：Java 直连走 TUN 时连上但无数据。必须给 JVM 显式代理
   `-Dhttps.proxyHost=127.0.0.1 -Dhttps.proxyPort=7890`（curl 能用不代表 Java 能用）。
3. **Chaquopy 的 `jarray('b')` 在 17.2 上报 `Invalid JNI signature`**：不要手工构造 Java
   byte[]，直接传 Python `bytes`，Chaquopy 会隐式转换；反向用 `bytes(bytearray(jarray))`。
4. **rsync 少写尾斜杠会多套一层目录**：`rsync src dst` 在目标不存在时可能得到 `dst/src/...`，
   症状是运行时 `No module named 'app.db'`（APK 里变成 `app/app/db.pyc`）。
5. **`sync_api` 从未登记在 `routers/__init__.py`**：`shell.js` 早就在调 `/api/sync/status`，
   但一直 404（前端 catch 掉了，表现为同步面板永远空白）。已补登记。
6. **WebView 不能先于后端加载**：Capacitor 的 `server.url` 一创建就去 loadUrl，
   所以必须有 SplashActivity 先等 `/api/health`，否则白屏且无任何线索。
7. **AGP 会把 assets 里的 `.gz` 解压并去掉后缀**（2026-11-10 实测）：`core.dump.gz`
   变成 160MB 的 `core.dump`，`tex_files/` 的 633 个 `.gz` 全部失去后缀 —— 而
   TikZJax 的引擎写死 `fetch('core.dump.gz')`，于是手机上**所有 TikZ 图**都变
   `tikzjax-broken`（桌面不受影响，桌面直接从 `api/web` 读 `.gz`）。
   做法：`build-apk.sh` 进包前把 TikZ 载荷改成 `.gzz` 骗过 AGP，
   `android_boot.restore_tikzjax_gz()` 启动时改回来（实测 635 个，`/assets/tikzjax/
   core.dump.gz` 返回 200 / 2,777,702 字节）。

## 已知限制

- 资料库原文（`reference/` 的 282M PDF/HTML）不在包里，手机上只能看切片正文与元数据。
- AI 需要用户在设置里填自己的 key；`--with-sync` 只带 Turso 配置与对话口令（且会进 APK，仅自用）。
- Pyodide **不预取也不随包**（`QF_HEAVY_PREFETCH=0`）：清单（3KB）随包走
  （`assets/runtime/pyodide-manifest.json` → `BootHelper` 拷到数据目录 →
  `QF_HEAVY_MANIFEST`），第一次真用到"跑 Python"时才按清单从 jsdelivr 取 146M
  并逐个校验；取不到就如实报"这一项不可用"（失败后 10 分钟内不重复试）。
  2026-11-09 之前的包**没带清单**，所以那个"用到时才下载"从来不成立。
- 实测环境是 x86_64 模拟器（API 35）；真机（arm64）走的是同一份 wheel 与同一套启动路径，
  但**尚未在物理设备上验收** —— 首次真机安装时重点看：首启释放耗时、`android_boot.log` 的
  `crypto backend=android` 一行、以及 390dp 以下小屏的输入法遮挡。

## 验收记录（2026-11-08，模拟器 Pixel 5 / API 35 / x86_64）

- 冷启动（首次）：释放 84M 资源 1.7s，Python 起来到 `/api/health` 200 共约 6s。
- 热启动：约 3s。
- 题库：`/api/bank` 返回 1625 道题、454 个主题。
- 同步：`/api/sync/run` 拉取 1789 行、应用 661、删除 1128（远端墓碑），12.3s。
- 390×844 五个页面 `scrollWidth - clientWidth = 0`。
- release APK 80M，`apksigner verify` 通过，覆盖安装正常。
