# QFStudio for Android

把 QFStudio 装进手机：**Capacitor 做壳，Chaquopy 把 CPython 和 FastAPI 一起打进 APK**。
手机上跑的是与桌面完全同一份后端代码，SQLite 是本地工作库，Turso 只做跨设备的状态中转。

## 为什么不走「壳 + 远程后端」

那样出门就没网、没后端、没数据。这里选的是「后端随包」，代价是 APK 变大（86M 左右）
和三条依赖上的妥协（见下），换来的是飞行模式下也能刷题、看对话、写笔记。

## 结构

```
┌─ SplashActivity（原生启动页）
│    · 释放 assets/web → filesDir/web（25M，按 lastUpdateTime 判断要不要重放）
│    · 拷贝 assets/bank.db → filesDir/quizforge/quizforge.db（仅首次）
│    · Python.start() → android_boot.start_in_thread()
│    · 每秒打 /api/health，就绪后跳 MainActivity，失败则显示日志尾部
└─ MainActivity（BridgeActivity）
     · capacitor.config.json 的 server.url = http://127.0.0.1:8731
     · ⚠️ 必须等服务起来才跳转，否则 WebView 一片白
```

`android_boot.py` 与 `build/launcher.py` 是同一套流程（`ensure_schema` → uvicorn 回环），
差别只在数据目录来自 `context.getFilesDir()`、不开浏览器、日志写 `filesDir/quizforge/android_boot.log`。

## 构建

一次性装工具链（约 3-4GB，全在 `~/.local`，不需要 sudo）：

```bash
bash mobile/scripts/bootstrap-toolchain.sh            # JDK21 + SDK 36 + Gradle
bash mobile/scripts/bootstrap-toolchain.sh --with-emulator   # 顺带装模拟器
bash mobile/scripts/keystore.sh                      # 自签 keystore（仓库外）
```

出包：

```bash
bash mobile/scripts/build-apk.sh              # release，自签
bash mobile/scripts/build-apk.sh --debug      # debug，最快
bash mobile/scripts/build-apk.sh --with-sync  # 把本机 Turso 配置打进包（含密钥，仅自用）
```

产物：`mobile/android/app/build/outputs/apk/release/app-release.apk`

安装：`adb install -r <apk>`，或直接把 APK 传到手机点击安装（需允许"未知来源"）。

## 依赖上的三条妥协（都不是口味问题，是没得选）

| 包 | 桌面 | Android | 为什么 |
|---|---|---|---|
| pydantic-core | 2.46.5 | **2.50.0** | 只有社区预编译的 cp314 wheel（`mobile/wheels/`）；2.50.0 正好是 pydantic 2.14.0 钉死的版本，所以桌面 2.13.5 要跟到 2.14.0 |
| cryptography | 50.0.2 | **无** | chaquo 仓库最高 cp313，cp314 没有 wheel。`app/crypto_android.py` 用 `javax.crypto`（AES-GCM）+ `hashlib.scrypt` 顶上，`crypto_compat.py` 负责选后端 |
| PyYAML | 6.0.3 | **vendored** | PyPI 只有含 libyaml 的平台 wheel；装配脚本把纯 Python 的 `yaml/` 拷进源码目录 |
| greenlet | 有 | **跳过** | sqlalchemy 会无条件拉它，但同步 ORM 用不到；`pip --no-deps` + 手写依赖清单 |

另外 `uvicorn` 不能带 `[standard]`（uvloop/httptools/watchfiles 都是 C 扩展）。

**清单维护方式**：`android/app/build.gradle` 的 `chaquopy { pip { … } }` 是唯一事实来源，
版本要跟 `api/.venv` 对齐，改一处记得改另一处。

## 加密必须两端逐字节一致

对话同步走端到端加密，Android 侧换成 `javax.crypto` 之后**格式必须与 `cryptography` 完全相同**
（AES-256-GCM，tag 附在密文尾部；scrypt n=2**15, r=8, p=1, dklen=32）。

每次启动都会自检并写进 `android_boot.log`：

```
crypto backend=android roundtrip=True key=5a97bd7411ca5496 blob=7e834f27c97e29e14515630c
```

桌面侧对拍（应输出同样的两个值）：

```bash
api/.venv/bin/python -c "
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
key = Scrypt(salt=bytes(range(16)), length=32, n=2**14, r=8, p=1).derive(b'quizforge-probe')
blob = AESGCM(key).encrypt(b'\x00'*12, b'quizforge', b'aad')
print('key=' + key.hex()[:16], 'blob=' + blob.hex()[:24])"
```

对不上就说明两端的算法参数被改动了 —— 症状是「拉下来的对话全是乱码/解密失败」。

## 题库怎么随包走

`mobile/scripts/export-bank-db.py` 按 `app/datapack.py` 的 `BANK_TABLES` 从 `data/quizforge.db`
抽一张只含题库表的新库（59M），**用户表与 qf_sync_\* 一张都不进** —— 既减体积，也避免把
对话/进度发出去。首启由 `BootHelper` 拷成工作库，`ensure_schema` 再补建用户表。

题库不参与同步：手机与电脑各自有题库，同步的只有用户态。

## 已知限制

- **资料库原文在手机上为空**：`reference/` 是电脑上的 282M PDF/HTML，没有打进 APK。
  资料页能看到元数据与切片正文，但"打开原始文件"会失败。
- **AI 对话需要自己填 API key**：设置 → AI。密钥不随包（`--with-sync` 只带 Turso）。
- **Pyodide 不预取**：`QF_HEAVY_PREFETCH=0`，用到"跑 Python"时才下载 76M。
- **深色主题**跟随系统；浅色档未在真机校准过。
- 首次启动要解包 Python 运行时 + 拷 25M 前端，冷启动约 15-30 秒（视存储速度）；之后 3-5 秒。

## 排错

```bash
# 启动到哪一步
adb shell run-as com.quizforge.app cat files/quizforge/android_boot.log

# 后端是否在监听
adb forward tcp:8731 tcp:8731 && curl -s http://127.0.0.1:8731/api/health

# 同步状态
curl -s http://127.0.0.1:8731/api/sync/status
```

`android_boot.log` 里的 `FAILED` 段带完整 traceback；启动页失败时显示的就是它的最后 20 行。
