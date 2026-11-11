"""lens（界面上叫「滤镜」）：一段提示词，不是代码（`docs/对话树.md` §十二）。

## 它长什么样：`<数据目录>/lens/` 里的独立 md

一条 lens = 一个文件，**front-matter 定元数据、正文就是那段提示词**：

    ---
    key: feynman
    label: 费曼
    kind: flow          # flow / tone / discipline
    hint: 你讲给我听，我戳你卡住的地方
    ---
    （正文：装进系统提示的那一段）

**它是用户自己的东西，不是随包发的资源**：文件在数据目录里
（Windows：`%USERPROFILE%\\quizforge\\lens\\*.md`），界面里能新增、改、删
（`PUT/DELETE /api/lens/{key}`）。所以桌面包**第一次启动时一条滤镜都没有** ——
这是用户要的默认（"打包的应用默认没有滤镜"），不是漏打包。

开发机上的那十条是仓库 `lens/` 里的**预置**：第一次读的时候拷进数据目录
（`seed_presets()`，只做一次；之后就是你的文件了，改了删了都不会再被仓库覆盖）。
打包版（`sys.frozen`）**不拷** —— 所以装上就是干净的。

读的是**每次调用时的磁盘内容**（按目录与文件的 mtime 缓存），所以改完存盘即生效、
不必重启 —— 边写边调的那十几分钟，全靠这一条。

## 三种形态，可以并存

| 形态 | 它改什么 | 类型 | 怎么用 |
|---|---|---|---|
| **口吻** `tone` | 它**怎么说** | 状态（挂着，持续生效） | 装 / 取 |
| **流程** `flow` | 它**按什么步骤走** | 动作（有开始、有走完） | 发起 / 走完 |
| **纪律** `discipline` | 它**不许做什么** | 状态（挂着，持续生效） | 装 / 取 |

用户（2026-09-25）："流程/口吻/纪律实际上是可以兼容的，我随时也可以取消流程。" ——
所以**没有"同时只能有一条"这种规矩**：挂上的都生效，一条一条都可以随时取下。
真冲突时按 `block()` 末尾那条写明的优先级（纪律压过口吻）。

## 挂在哪、怎么回退

一条记录一行，住 `lens_records` 表（2026-11-09 从设置里搬出来 —— 滤镜属于数据，
不该占设置文件）：`{conversation_id, message_uid, lens_key, enabled, at}`，
`message_uid` 为空 = 整条对话。前端 `QF.store.attachLens` 写、服务端只读；
表随对话走 Turso 行级同步，也进用户数据包。

于是"某个节点上现在挂着什么" = **沿这条链从根往下，每个 key 取最近的那条记录**：

* **沿树继承**是免费得到的（不同的分支本来就走出不同的链）；
* **"走出来"自动回上层**也是免费的（链变了，最近的那条记录就换了）；
* **显式取下**就是那条 `on=False` 的记录；
* `mid` 为空 = 整条对话（第一条消息还没有 mid 可挂）。
"""

from __future__ import annotations

import hashlib
import re
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

import yaml
from sqlalchemy import select

from . import REPO_ROOT

#: 认得的三种形态，以及**渲染顺序**（流程在前：它是"这一轮在做什么"，
#: 比"怎么说"更要紧，出问题也更容易被发现）。
KINDS: tuple[str, ...] = ("flow", "tone", "discipline")
KIND_LABELS: dict[str, str] = {"flow": "流程", "tone": "口吻", "discipline": "纪律"}

#: 这条链上最多往上找几层（防病态数据把一次生成拖死；与 `recursion` 那几处同一个态度）
_WALK_MAX = 600

#: key 只能是这样：它同时是**文件名**（`<key>.md`），所以不许有空格、斜杠、中文
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")

#: 字段长度上限。**不是为了省地方，是为了界面不变形**：名字太长会把清单那一行挤成
#: 两行、说明太长会把整张单子撑成一条长龙。这些数就是编辑器里 `maxlength` 的那几个，
#: 两边必须一致（前端只是"提前拦"，服务端才是**唯一**的判据 —— 手改 md 也走这条路）。
MAX_LABEL = 24
MAX_HINT = 60
MAX_PROMPT = 4000


@dataclass(frozen=True)
class Lens:
    """一条 lens。`prompt` 就是它的全部实现 —— 加一条只是加一个 md。"""

    key: str
    label: str
    kind: str
    hint: str
    prompt: str
    #: 它来自哪个文件（报错、排查、以及"我改的到底是哪一个"都要它）
    source: str = ""


_cache: dict = {"dir": None, "stamp": None, "items": ()}


def folder() -> Path:
    """滤镜的文件夹：**数据目录**下的 `lens/`（不是仓库、也不是解包目录）。

    用户要的是"这东西是我的"：能改、能删、能自己加。仓库里那份 `lens/` 只是
    开发机上的预置（见 `seed_presets`），打包版一条都不带。
    """
    from .config import get_settings  # noqa: PLC0415

    return Path(get_settings().data_dir) / "lens"


def seed_presets() -> int:
    """开发机第一次读的时候，把仓库 `lens/` 里那批预置拷进数据目录。返回拷了几条。

    * **只做一次**：目标目录存在就什么都不做 —— 于是"我删掉了某条"不会下次启动又冒出来；
    * **打包版不做**：`sys.frozen` 里没有仓库，用户要的默认就是"一条都没有"。
    """
    if getattr(sys, "frozen", False):
        return 0
    target = folder()
    if target.exists():
        return 0
    source = REPO_ROOT / "lens"
    if not source.is_dir():
        return 0
    target.mkdir(parents=True, exist_ok=True)
    copied = 0
    for one in sorted(source.glob("*.md")):
        try:
            shutil.copy2(one, target / one.name)
            copied += 1
        except OSError:
            continue
    return copied


def _parse(text: str, source: str) -> Lens | None:
    """一个 md → 一条 lens。**不合格的返回 None**（坏一条不该让整个目录空掉）。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return None
    try:
        meta = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(meta, dict):
        return None
    key = str(meta.get("key") or "").strip()
    kind = str(meta.get("kind") or "").strip()
    if not key or kind not in KINDS:
        return None
    body = "\n".join(lines[end + 1 :]).strip()
    if not body:
        return None
    return Lens(
        key=key,
        label=str(meta.get("label") or key).strip(),
        kind=kind,
        hint=str(meta.get("hint") or "").strip(),
        prompt=body,
        source=source,
    )


def _load() -> tuple[Lens, ...]:
    """读那个文件夹（按 mtime 缓存：改完存盘即生效，不必重启）。"""
    seed_presets()
    home = folder()
    if not home.is_dir():
        return ()
    try:
        files = sorted(one for one in home.glob("*.md") if one.is_file())
        stamp = tuple((str(one), one.stat().st_mtime_ns) for one in files)
    except OSError:
        return ()
    if _cache["dir"] == str(home) and _cache["stamp"] == stamp:
        return _cache["items"]
    out = []
    for one in files:
        try:
            lens = _parse(one.read_text(encoding="utf-8"), one.name)
        except OSError:
            continue
        if lens is not None:
            out.append(lens)
    out.sort(key=lambda item: (KINDS.index(item.kind), item.key))
    _cache["dir"] = str(home)
    _cache["stamp"] = stamp
    _cache["items"] = tuple(out)
    return _cache["items"]


def _digest(text: str) -> str:
    """md 原文的指纹：判断"磁盘上这份"与"库里这份"是不是同一份。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _from_row(row) -> Lens:  # noqa: ANN001
    return Lens(
        key=row.key,
        label=row.label,
        kind=row.kind,
        hint=row.hint or "",
        prompt=row.prompt or "",
        source=f"{row.key}.md",
    )


def _db_items(db) -> tuple[Lens, ...]:  # noqa: ANN001
    from .models import LensDef  # noqa: PLC0415 - 延迟导入：lens 是热点模块

    rows = db.scalars(select(LensDef).where(LensDef.deleted.is_(False))).all()
    out = [_from_row(one) for one in rows]
    out.sort(key=lambda item: (KINDS.index(item.kind) if item.kind in KINDS else len(KINDS), item.key))
    return tuple(out)


def sync_folder(db) -> None:  # noqa: ANN001
    """文件 ↔ `lenses` 表：两边对齐（每次读之前跑，稳态下不写库）。

    **规则只有一条：文件优先。** 文件被手改过（digest 与库不同）就以文件为准写库 ——
    直接编辑文件是最强的意图表达，这条是原来"改完存盘即生效"的延续。

    反方向也要走：库里多出来的行（**别的设备同步来的**）落成文件，于是
    `ls <数据目录>/lens` 看得见、编辑器和备份都拿得到。墓碑（`deleted`）负责
    "删掉的不许被磁盘上的残留文件带回来" —— 没有它，远端一删、本地一启动就复活。
    """
    from .models import LensDef  # noqa: PLC0415

    home = folder()
    seed_presets()
    on_disk: dict[str, tuple[Lens, str]] = {}
    if home.is_dir():
        try:
            files = list(home.glob("*.md"))
        except OSError:
            files = []
        for one in files:
            try:
                text = one.read_text(encoding="utf-8")
            except OSError:
                continue
            lens = _parse(text, one.name)
            if lens is not None:
                on_disk[lens.key] = (lens, _digest(text))

    rows = {one.key: one for one in db.scalars(select(LensDef)).all()}
    changed = False

    # 文件 → 库
    for key, (lens, stamp) in on_disk.items():
        row = rows.get(key)
        if row is None:
            db.add(
                LensDef(
                    key=lens.key,
                    label=lens.label,
                    kind=lens.kind,
                    hint=lens.hint,
                    prompt=lens.prompt,
                    digest=stamp,
                )
            )
            changed = True
        elif row.deleted:
            continue  # 删过了：磁盘上这份是残留，不导回
        elif row.digest != stamp:
            row.label, row.kind, row.hint = lens.label, lens.kind, lens.hint
            row.prompt, row.digest = lens.prompt, stamp
            changed = True

    # 库 → 文件
    for key, row in rows.items():
        path = home / f"{key}.md"
        if row.deleted:
            try:
                path.unlink()
            except OSError:
                pass
            continue
        if key in on_disk:
            continue
        text = _render(row.key, row.label, row.kind, row.hint or "", row.prompt or "")
        try:
            home.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError:
            continue
        row.digest = _digest(text)
        changed = True

    if changed:
        db.commit()
        _cache["stamp"] = None


def all_lenses(db=None) -> tuple[Lens, ...]:  # noqa: ANN001
    """全部滤镜。给了 `db` 就以表为准（并把文件对齐），没给就退回去读文件。

    为什么还留文件这条路：启动体检（`startup._resource_step`）拿不到会话，
    只想知道"有几条"；那时读目录最省事。
    """
    if db is None:
        return _load()
    sync_folder(db)
    return _db_items(db)


def find(word: str, db=None) -> Lens | None:  # noqa: ANN001
    """一个词 → 一条 lens（认 key 也认中文名，大小写不敏感）。

    只按 key / 名字认。**没有 `/` 前缀那回事**了（2026-11-06 起界面不再有命令）。
    """
    raw = str(word or "").strip()
    if not raw:
        return None
    for one in all_lenses(db):
        if one.key.lower() == raw.lower() or one.label == raw:
            return one
    return None


def catalog(db=None) -> list[dict]:  # noqa: ANN001
    """给界面的一份清单（药丸里那份列表照它渲染）。**不带 `prompt`** —— 那是服务端的事。"""
    return [
        {"key": one.key, "label": one.label, "kind": one.kind, "hint": one.hint, "source": one.source}
        for one in all_lenses(db)
    ]


def _render(key: str, label: str, kind: str, hint: str, prompt: str) -> str:
    """一条 lens → 一个 md 的文本（字段顺序固定：手改时好读，diff 也稳定）。"""
    meta = {"key": key, "label": label, "kind": kind}
    if hint:
        meta["hint"] = hint
    head = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    return "---\n" + head + "\n---\n\n" + prompt.strip() + "\n"


def _store(db, one: Lens) -> None:  # noqa: ANN001
    """把一条写进 `lenses`（含清墓碑）。digest 与刚写下去的**那份文件**对齐。

    不这么做的话，下一次 `sync_folder` 会把它当成"文件被手改过"再导一遍 ——
    内容一样，但 `updated_at` 变了，等于每次启动都白推一次同步。
    """
    from .models import LensDef  # noqa: PLC0415

    text = _render(one.key, one.label, one.kind, one.hint, one.prompt)
    row = db.get(LensDef, one.key)
    if row is None:
        db.add(
            LensDef(
                key=one.key,
                label=one.label,
                kind=one.kind,
                hint=one.hint,
                prompt=one.prompt,
                digest=_digest(text),
            )
        )
    else:
        row.label, row.kind, row.hint = one.label, one.kind, one.hint
        row.prompt, row.digest, row.deleted = one.prompt, _digest(text), False
    db.commit()


def save(
    *,
    key: str = "",
    label: str,
    kind: str,
    hint: str = "",
    prompt: str,
    db=None,  # noqa: ANN001
) -> Lens:
    """新增 / 改一条（界面里的"保存"）。

    写文件是**先写临时文件再 replace**：半截写坏的文件会被 `_parse` 判成不合格，
    于是"改到一半断电"就等于这条滤镜凭空消失 —— 宁可整份换过去。
    """
    clean_label = str(label or "").strip()
    clean_kind = str(kind or "").strip()
    clean_prompt = str(prompt or "").strip()
    clean_hint = str(hint or "").strip()
    if not clean_label:
        raise ValueError("名字不能空")
    if len(clean_label) > MAX_LABEL:
        raise ValueError(f"名字最多 {MAX_LABEL} 个字")
    if clean_kind not in KINDS:
        raise ValueError(f"形态只能是 {'/'.join(KINDS)}")
    if len(clean_hint) > MAX_HINT:
        raise ValueError(f"一句话说明最多 {MAX_HINT} 个字")
    if not clean_prompt:
        raise ValueError("提示词不能空")
    if len(clean_prompt) > MAX_PROMPT:
        raise ValueError(f"提示词最多 {MAX_PROMPT} 个字")
    name = str(key or "").strip().lower()
    if name and not _KEY_RE.match(name):
        raise ValueError("key 只能是小写字母、数字、短横线")
    if not name:
        name = "lens-" + uuid.uuid4().hex[:8]
    home = folder()
    home.mkdir(parents=True, exist_ok=True)
    path = home / f"{name}.md"
    tmp = home / f".{name}.md.tmp"
    tmp.write_text(_render(name, clean_label, clean_kind, clean_hint, clean_prompt), encoding="utf-8")
    tmp.replace(path)
    _cache["stamp"] = None
    one = Lens(name, clean_label, clean_kind, clean_hint, clean_prompt, path.name)
    if db is not None:
        _store(db, one)
    return one


def delete(key: str, db=None) -> bool:  # noqa: ANN001
    """删一条。**挂在链上的记录留着**（引用了不存在的 key 会被忽略）。

    库里留一行墓碑（`deleted=True`）而不是物理删：这张表跟着 Turso 同步，
    物理删只能表达"本机删了"；墓碑才能让别的设备也删、并且保证磁盘上的
    残留文件不会在下次启动时把它导回来。
    """
    name = str(key or "").strip().lower()
    if not _KEY_RE.match(name or ""):
        return False
    path = folder() / f"{name}.md"
    try:
        path.unlink()
    except OSError:
        return False
    _cache["stamp"] = None
    if db is not None:
        from .models import LensDef  # noqa: PLC0415

        row = db.get(LensDef, name)
        if row is None:
            db.add(
                LensDef(
                    key=name,
                    label=name,
                    kind=KINDS[0],
                    hint="",
                    prompt="",
                    digest="",
                    deleted=True,
                )
            )
        else:
            row.deleted = True
        db.commit()
    return True


def _records(db) -> list[dict]:  # noqa: ANN001
    """库里那批挂载记录（前端写的，服务端只读）。坏数据一律当没有。

    2026-11-09 起住在 `lens_records` 表：它属于**数据**（挂在消息上、沿树继承），
    随对话走 Turso 行级同步，也随用户数据包换机器。不再住设置。
    """
    from .models import LensRecord

    try:
        _migrate_from_settings(db)
        rows = db.query(LensRecord).all()
    except Exception:  # noqa: BLE001 - lens 是加成，不该拖垮一轮生成
        return []
    keys = {one.key for one in _load()}
    out = []
    for one in rows:
        key = str(one.lens_key or "").strip()
        if key in keys:
            out.append(
                {
                    "cid": str(one.conversation_id),
                    "message_uid": str(one.message_uid or ""),
                    "key": key,
                    "on": bool(one.enabled),
                    "at": int(one.at or 0),
                }
            )
    return out


def _migrate_from_settings(db) -> None:  # noqa: ANN001
    """老数据一次性搬运：设置里的 `lens` / `skills` → `lens_records`。

    只在表还是空的时候做。`mid`（本机消息 id）在这里翻成 `Message.uid` ——
    记录一旦进表就只认跨机身份，换台机器照样对得上。
    """
    from . import settings_file, settings_store
    from .models import AppSettings, Conversation, LensRecord, Message

    if db.query(LensRecord).first() is not None:
        return
    # **读未过滤的那一份**：设置接口已经把 `lens` / `skills` 滤掉了，
    # 这里要的恰恰是它们。
    conf = settings_file.load()
    if not conf:
        found = db.get(AppSettings, 1)
        raw = getattr(found, "data", None)
        conf = dict(raw) if isinstance(raw, dict) else {}
    rows = conf.get("lens")
    if not isinstance(rows, list):
        rows = conf.get("skills")
    if not isinstance(rows, list) or not rows:
        return

    moved = 0
    for one in rows:
        if not isinstance(one, dict):
            continue
        key = str(one.get("key") or "").strip()
        if not key:
            continue
        try:
            conv_id = uuid.UUID(str(one.get("cid") or ""))
        except (ValueError, AttributeError):
            continue
        if db.get(Conversation, conv_id) is None:
            continue
        message_uid = ""
        mid = str(one.get("mid") or "").strip()
        if mid:
            try:
                msg = db.get(Message, int(mid))
            except (TypeError, ValueError):
                msg = None
            if msg is None or str(msg.conversation_id) != str(conv_id):
                continue
            message_uid = str(msg.uid or "")
        db.add(
            LensRecord(
                conversation_id=conv_id,
                message_uid=message_uid,
                lens_key=key,
                enabled=one.get("on") is not False,
                at=int(one.get("at") or 0),
            )
        )
        moved += 1
    db.commit()
    if moved:
        # 搬完就从设置里删掉（设置文件是给人复制的，不装用户痕迹）
        settings_store.put(db, lens=None, skills=None)


def records(db, cid: str = "") -> list[dict]:  # noqa: ANN001
    """给前端的记录行（`mid` 是**本机消息 id**，空 = 整条对话）。

    表里存的是 `message_uid`（跨机身份），这里按本机库翻回来；消息还没到本机
    （同步只到了一半）的那些记录先不给 —— 给个空 `mid` 会被误读成"整条对话"。
    """
    rows = _records(db)
    if cid:
        rows = [one for one in rows if one["cid"] == str(cid)]
    uids = {one["message_uid"] for one in rows if one["message_uid"]}
    mapping: dict[str, str] = {}
    if uids:
        from .models import Message

        for msg in db.query(Message).filter(Message.uid.in_(uids)).all():
            mapping[str(msg.uid)] = str(msg.id)
    out = []
    for one in rows:
        uid = one["message_uid"]
        if uid and uid not in mapping:
            continue
        out.append(
            {
                "cid": one["cid"],
                "mid": mapping.get(uid, ""),
                "key": one["key"],
                "on": one["on"],
                "at": one["at"],
            }
        )
    return out


def _parent(db, node):  # noqa: ANN001, ANN202
    """往上走一层（按 `parent_id` 取那一条）。"""
    pid = getattr(node, "parent_id", None)
    if not pid:
        return None
    from .models import Message

    return db.get(Message, int(pid))


def resolve(db, conv, node) -> list[Lens]:  # noqa: ANN001
    """这条链上（根 → `node`）当下挂着哪些 lens。**同一 key 取最近的记录。**

    三种形态一视同仁：**挂上的都生效**（用户："流程/口吻/纪律实际上是可以兼容的"）。
    没有"同时只能有一条流程"这种规矩 —— 取下哪条由他点，不由我们替他收。
    """
    rows = _records(db)
    if not rows:
        return []
    cid = str(getattr(conv, "id", "") or "")
    mine = [one for one in rows if one["cid"] == cid]
    if not mine:
        return []

    by_uid: dict[str, list[dict]] = {}
    for one in mine:
        by_uid.setdefault(one["message_uid"], []).append(one)

    # 从 node 往上走到根（只取 uid —— 这里只需要知道路径上有没有这条记录，不必查正文）
    stack: list[str] = []
    walker = node
    guard = 0
    while walker is not None and guard < _WALK_MAX:
        stack.append(str(getattr(walker, "uid", "") or ""))
        walker = _parent(db, walker)
        guard += 1
    stack.reverse()                     # 根 → 节点
    stack.insert(0, "")                 # `message_uid` 为空 = 整条对话（排在根之前）

    decided: dict[str, bool] = {}
    for uid in stack:                   # 越靠近节点的记录越后写，天然覆盖前面的
        for one in by_uid.get(uid, []):
            decided[one["key"]] = one["on"]

    # 一次读齐（`find()` 每条都会走一遍 sync_folder + 查询 —— 每轮生成都这么做太亏）
    current = all_lenses(db)
    order = {one.key: index for index, one in enumerate(current)}
    by_key = {one.key: one for one in current}
    live = [by_key[key] for key, on in decided.items() if on and key in by_key]
    live.sort(key=lambda one: order.get(one.key, 0))
    return live


def block(lenses: list[Lens]) -> str:
    """拼成系统提示里那一段。没有就返回空串（调用方据此不加）。"""
    if not lenses:
        return ""
    parts = [
        "## 他这一轮给你装了什么（滤镜）",
        "",
        "他从「滤镜」清单里装上的东西在下面。**它们都生效**（可以同时装好几条），"
        "**一条一条都可以随时被他取下** —— 取下之后就别再照它做了，也不要问「还要不要那样」。",
    ]
    for kind in KINDS:
        mine = [one for one in lenses if one.kind == kind]
        if not mine:
            continue
        parts.append("")
        parts.append("### " + KIND_LABELS[kind])
        for one in mine:
            parts.append("")
            parts.append("**" + one.label + "**")
            parts.append(one.prompt)
    parts.append("")
    parts.append(
        "**冲突时怎么排**：纪律压过口吻（「不许做什么」比「怎么说」硬）；"
        "流程要你换一种做法时，按流程来 —— 它管的是这一轮**谁在做主**。"
        "几条之间若实在冲突，按他**最后装上**的那条（`at` 更大的）。"
    )
    return "\n".join(parts)
