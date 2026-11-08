"""加密原语唯一的取用点：桌面用 `cryptography`，Android 用它自己的替身。

为什么单开一层：`sync.py` 的 4 个调用点（`Scrypt(...).derive`、`AESGCM(...).encrypt`、
`AESGCM(...).decrypt`、`except InvalidTag`）在两个平台上签名必须一致，而
Android 上没有 `cryptography` 的 cp314 wheel。把选择收在这里，`sync.py` 的代码
一行都不用分平台写。

`crypto_android` 里 `import java` 只会在 Chaquopy 里成功；桌面永远走上面那支，
所以它不会被 import —— 这也是这里用 try/except 而不是平台判断的原因：
Chaquopy 的 Python 里 `sys.platform == "android"`，但那不是唯一的判断依据。
"""

from __future__ import annotations

try:  # 桌面 / 打包后的 exe
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    BACKEND = "cryptography"
except ModuleNotFoundError:  # Android（Chaquopy）
    from .crypto_android import AESGCM, InvalidTag, Scrypt  # type: ignore[assignment]

    BACKEND = "android"

__all__ = ["AESGCM", "BACKEND", "InvalidTag", "Scrypt"]
