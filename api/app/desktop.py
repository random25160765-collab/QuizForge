"""和本机桌面打交道的那一点点：在系统文件管理器里打开一个目录。

为什么值得单独一个模块：它是**唯一**一处我们会去启动别的程序。所以规矩写在这里，
而不是散在路由里：

* **路径只由服务端给**。接口那边永远从"已登记的库 / 资料根"里取真实路径，
  绝不让客户端传一个路径进来 —— 否则这就成了一个"按请求打开任意目录"的口子
  （`notes.safe_path` 管的是"别越出库"，这里管的是"根本不存在这条路"）。
* **打不开不算错**。没装 `xdg-open`、SSH 会话、无桌面环境、容器里 —— 都是正常情况。
  返回 `opened: False` 让界面上给一句人话，而不是抛 500 让人以为库坏了。
* **不经 shell**。命令与参数分开放进列表，`..`、空格、`;` 都只是普通字符。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def command_for(platform: str, path: str) -> list[str]:
    """各平台打开一个目录的命令。抽成纯函数就是为了能测 —— 这里没有别的逻辑。"""
    if platform == "darwin":
        return ["open", path]
    if platform.startswith("win"):
        # `explorer` 打开目录是它的正常用法（不是"打开文件夹里的文件"那种）
        return ["explorer", path]
    return ["xdg-open", path]


def reveal(path: Path) -> dict:
    """在系统文件管理器里打开这个目录。返回 `{"opened": bool, ...}`。"""
    target = str(Path(path))
    if not Path(path).exists():
        return {"opened": False, "path": target, "why": "这个目录不在本机（可能被移走了）"}
    cmd = command_for(sys.platform if os.name != "nt" else "win32", target)
    try:
        # Popen 而不是 run：文件管理器是**长驻**程序，run 会一直等它关掉
        subprocess.Popen(  # noqa: S603 —— 命令是我们自己拼的固定几种，参数不经 shell
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"opened": False, "path": target, "why": str(exc)}
    return {"opened": True, "path": target}


#: 用户要在系统选择器里干什么（各平台的文案）
PICK_TITLE = "选择资料目录"


def pick_folder_commands(platform: str) -> list[list[str]]:
    """各平台"弹系统文件夹选择器"的命令，按优先级排。空列表 = 这台上没有。

    抽成纯函数就是为了能测（与 `command_for` 同一条理由）。
    Linux 上给三种：zenity（GNOME）/ kdialog（KDE）/ qarma（轻量 WM）——
    桌面环境五花八门，少写一个就是少一半用户能用。
    """
    if platform == "darwin":
        return [["osascript", "-e", f'POSIX path of (choose folder with prompt "{PICK_TITLE}")']]
    if platform.startswith("win"):
        script = (
            "Add-Type -AssemblyName System.Windows.Forms | Out-Null;"
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
            f"$d.Description = '{PICK_TITLE}';"
            "if ($d.ShowDialog() -eq 'OK') { Write-Output $d.SelectedPath } else { exit 1 }"
        )
        return [["powershell", "-NoProfile", "-NonInteractive", "-Command", script]]
    return [
        ["zenity", "--file-selection", "--directory", "--title=" + PICK_TITLE],
        ["kdialog", "--getexistingdirectory", ".", "--title", PICK_TITLE],
        ["qarma", "--file-selection", "--directory", "--title=" + PICK_TITLE],
    ]


def pick_folder(timeout_s: float = 300.0) -> dict:
    """弹一次系统文件夹选择器。

    返回值三态，界面照这三态说不同的话：

    * `{"path": "/…"}` —— 用户选好了
    * `{"cancelled": True}` —— 用户按了取消（**不是错误**，别报错）
    * `{"unsupported": True, "why": "…"}` —— 这台上没有可用的图形选择器
      （无桌面环境、容器里、SSH 会话）：这时界面回退到"手输路径"，
      并把原因说出来 —— 直接静默失败的话，用户只会以为按钮坏了。

    为什么要这个而不是让人手打路径（用户连着说了两遍）：路径是这个界面里
    **最不该让人手打**的东西 —— 打错一个字符就"根目录不存在"，
    而"选一个目录"在系统里本来就是点两下的事。
    """
    platform = sys.platform if os.name != "nt" else "win32"
    tried: list[str] = []
    for cmd in pick_folder_commands(platform):
        if not shutil.which(cmd[0]):
            tried.append(cmd[0])
            continue
        try:
            result = subprocess.run(  # noqa: S603 —— 命令是我们自己拼的固定几种，参数不经 shell
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {"unsupported": True, "why": str(exc)[:200]}
        if result.returncode != 0:
            # 用户取消（zenity/osascript 都是非 0）—— 与"没有工具"要分开
            return {"cancelled": True}
        picked = (result.stdout or "").strip().splitlines()
        path = Path(picked[-1].strip()) if picked else None
        if not path or not path.is_dir():
            return {"cancelled": True}
        return {"path": str(path)}
    return {
        "unsupported": True,
        "why": "这台机器上没有可用的图形选择器（试过：" + "、".join(tried) + "）；"
        "可以手动填目录的绝对路径。",
    }


__all__ = ["PICK_TITLE", "command_for", "pick_folder", "pick_folder_commands", "reveal"]
