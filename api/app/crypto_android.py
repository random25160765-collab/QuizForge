"""`cryptography` 在 Android 上的替身（只有 sync.py 需要的那三个 API）。

为什么需要它：`cryptography` 没有 cp314 的 Android wheel，而 Python 3.14 是
Chaquopy 17 上唯一能拿到 pydantic-core 预编译包的版本（见 mobile 的 README）。
好在 sync.py 只用到三样东西：AES-256-GCM、scrypt、InvalidTag —— 前两者在
Android 上都有系统级实现，不必自己写纯 Python 的 AES。

* **AES-GCM 走 `javax.crypto`**：Android 的 Conscrypt 有硬件加速，比纯 Python 快几个
  数量级。密文格式与 `cryptography` 完全一致（GCM 的 tag 附在密文尾部），所以
  两端可以互相解开对方的密文 —— 这一点是同步能成立的前提。
* **scrypt 走 `hashlib.scrypt`**：标准库，OpenSSL 提供；参数与桌面端一致
  （n=2**15, r=8, p=1, length=32），派生结果逐字节相同。

这个模块**只能在 Android 上 import**（`java` 是 Chaquopy 注入的）；桌面端由
`crypto_compat` 选择 `cryptography`，永远不会走到这里。
"""

from __future__ import annotations

import hashlib

from java import jclass  # type: ignore[import-not-found]  # noqa: E402  Chaquopy-only

#: 与 `cryptography.exceptions.InvalidTag` 同义：解密失败（tag 不对）时抛它，
#: 调用方只按类型捕获，不关心底层是谁抛的。
InvalidTag = jclass("javax.crypto.AEADBadTagException")


def _from_java(array) -> bytes:
    """Java byte[] → Python bytes。

    按元素取（jarray 的迭代给出 int）而不是 `jarray('b')(...)` 构造：
    构造那一支要求传 JNI 签名，签名写法在 Chaquopy 版本间变过 ——
    实测 17.2 上 `jarray('b')` 直接抛 `Invalid JNI signature: 'b'`。
    """
    return bytes(bytearray(array))


class AESGCM:
    """`cryptography.hazmat.primitives.ciphers.aead.AESGCM` 的同形替身。"""

    def __init__(self, key: bytes) -> None:
        if len(key) not in (16, 24, 32):
            raise ValueError("AESGCM 只接受 128/192/256 位的密钥")
        self._key = bytes(key)
        self._cipher = jclass("javax.crypto.Cipher")
        self._spec = jclass("javax.crypto.spec.GCMParameterSpec")
        self._key_spec = jclass("javax.crypto.spec.SecretKeySpec")

    def _init(self, mode: int, nonce: bytes, aad: bytes):
        cipher = self._cipher.getInstance("AES/GCM/NoPadding")
        # bytes 直接传：Chaquopy 会把 Python bytes 隐式转成 Java byte[]。
        cipher.init(mode, self._key_spec(self._key, "AES"), self._spec(128, nonce))
        if aad:
            cipher.updateAAD(aad)
        return cipher

    def encrypt(self, nonce: bytes, data: bytes, aad: bytes | None = None) -> bytes:
        cipher = self._init(self._cipher.ENCRYPT_MODE, nonce, aad or b"")
        return _from_java(cipher.doFinal(data))

    def decrypt(self, nonce: bytes, data: bytes, aad: bytes | None = None) -> bytes:
        cipher = self._init(self._cipher.DECRYPT_MODE, nonce, aad or b"")
        try:
            return _from_java(cipher.doFinal(data))
        except Exception as exc:
            # 把 Java 的一堆异常类型收敛成调用方认识的 InvalidTag。
            raise InvalidTag(str(exc)) from exc


class Scrypt:
    """`cryptography.hazmat.primitives.kdf.scrypt.Scrypt` 的同形替身。"""

    def __init__(self, *, salt: bytes, length: int, n: int, r: int, p: int) -> None:
        self._salt = bytes(salt)
        self._length = int(length)
        self._n = int(n)
        self._r = int(r)
        self._p = int(p)

    def derive(self, key_material: bytes) -> bytes:
        # maxmem 必须显式给：OpenSSL 的默认上限（32MB）正好卡在 128*r*n = 33.5MB 之下，
        # 不给就会报 "memory limit exceeded"，而参数本身是对的。
        return hashlib.scrypt(
            key_material,
            salt=self._salt,
            n=self._n,
            r=self._r,
            p=self._p,
            dklen=self._length,
            maxmem=128 * self._r * self._n * 2,
        )
