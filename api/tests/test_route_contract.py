"""路由契约的系统性检查。

这里放的都是「一次能覆盖全部路由」的检查，而不是逐个接口断言 ——
踩过的坑是同一类错误犯三次：把读接口写成写接口的依赖
（`AuthenticatedWriter` 内含 CSRF 双提交校验），浏览器发出不带
CSRF 头的 `GET` 时直接 403，而且只有真跑起来才发现。

与其为每个 `GET` 手写一遍断言，不如遍历路由表一次性兜住，
新增接口自动纳入检查范围。

单用户本地形态（2026-09-18）之后，这一层的重点变成了**反向的**：
账号面已经整块删掉，谁也不许把那些守卫挂回来。
"""

from __future__ import annotations

from fastapi.routing import APIRoute

from app.main import create_app

READ_METHODS = {"GET", "HEAD", "OPTIONS"}

#: 账号面删掉的依赖名。挂回任何一个，"要登录 / 要 CSRF / 跳登录页"就会跟着回来 ——
#: 而那正是这一版要拆掉的东西（见 `app/deps.py` 的文件注释）。
REMOVED_GUARDS = {
    "require_csrf",
    "get_authenticated_writer",
    "guard_write",
    "verify_same_origin",
    "get_optional_user",
    "resolve_session",
    "set_session_cookie",
    "set_csrf_cookie",
    # 2026-09-22：连"用户"这个概念也拆掉了（含 `users` 表与 10 张表的 `user_id`）。
    # 这两个依赖只要挂回来一个，`users` 表就会跟着回来 —— 见下面那条测试。
    "get_current_user",
    "get_local_user",
}


def _dependant_names(dependant) -> set[str]:  # noqa: ANN001
    names = {getattr(dependant.call, "__name__", "")}
    for child in dependant.dependencies:
        names |= _dependant_names(child)
    return names


def _api_routes() -> list[APIRoute]:
    return [route for route in create_app().routes if isinstance(route, APIRoute)]


def test_no_route_depends_on_the_removed_account_layer() -> None:
    """账号面的守卫不许挂回来 —— 这是"去账号"的护栏。"""
    offenders = []
    for route in _api_routes():
        hit = _dependant_names(route.dependant) & REMOVED_GUARDS
        if hit:
            offenders.append(f"{sorted(route.methods or [])} {route.path} → {sorted(hit)}")
    assert not offenders, "这些路由挂回了已删除的账号依赖：" + "；".join(offenders)


def test_the_auth_endpoints_are_gone() -> None:
    """`/api/auth/*` 整块删掉了：本地单机没有注册、登录、退出、改密。"""
    stray = [
        f"{sorted(route.methods or [])} {route.path}"
        for route in _api_routes()
        if route.path.startswith("/api/auth")
    ]
    assert not stray, f"账号面的路由又出现了：{stray}"


def test_the_user_model_is_gone() -> None:
    """`users` 表与任何 `user_id` 列都不许回归 —— 这是 2026-09-22 那次拆除的护栏。

    为什么这条要单独钉住：账号思维很容易悄悄渗回来（"加个 `user_id` 以后好做同步"），
    而它比"路由上没挂依赖"更根本 —— 模型里只要还有 `users`，
    那些依赖迟早会被写回来（2026-09-18 就是这么只拆了一半）。

    单机里"用户"没有指代对象：没有第二个人，也就没有归属要记。
    详见 `app/models.py` 顶部那段与 `docs/STATUS.md` §五。
    """
    from app.db import Base

    tables = set(Base.metadata.tables)
    assert "users" not in tables, "`users` 表又回来了"
    assert "user_settings" not in tables, "`user_settings` 又回来了（应为 `app_settings`）"
    assert "user_questions" not in tables, "`user_questions` 又回来了（应为 `my_questions`）"

    leaked = sorted(
        f"{table.name}.{column.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
        if column.name == "user_id"
    )
    assert not leaked, f"这些列又挂上了 user_id：{leaked}"


#: 允许挂在 `/api` 之外的路由前缀 —— 只给**静态资源**这一类，
#: 它们和 StaticFiles 的挂载点是同一个命名空间（浏览器按 URL 直接取）。
STATIC_ROUTE_PREFIXES = ("/assets/",)


def test_every_api_route_is_under_api_prefix() -> None:
    """业务路由统一挂在 /api 下，静态资源与 SPA 兜底才不会误吞接口请求。

    例外只有一条：`/assets/*`。那里放的是**给浏览器直接取的文件**
    （Pyodide 运行时按文件名逐个取，见 `app/heavy_deps.py`）——
    它的路径必须与 `StaticFiles` 挂载的 `/assets` 同源，
    挪到 `/api` 下反而会让"资源"和"接口"混在一起。
    """
    stray = [
        route.path
        for route in _api_routes()
        if not route.path.startswith("/api")
        and not route.path.startswith(STATIC_ROUTE_PREFIXES)
    ]
    assert not stray, f"这些路由不在 /api 下：{stray}"


def test_no_duplicate_route_paths() -> None:
    """同一个「方法 + 路径」只能注册一次，否则请求会打到随机一个上去。"""
    seen = {}
    duplicates = []
    for route in _api_routes():
        for method in route.methods or []:
            key = (method, route.path)
            if key in seen:
                duplicates.append(f"{method} {route.path}")
            seen[key] = True
    assert not duplicates, f"重复注册的路由：{duplicates}"


# ------------------------------------------------------------------ 缓存策略


def _cache_control(client, path: str) -> str:  # noqa: ANN001
    return client.get(path).headers.get("cache-control", "")


def test_static_assets_always_revalidate(client) -> None:  # noqa: ANN001
    """JS / CSS / HTML 的文件名**不带内容哈希**，所以必须每次回来校验。

    否则部署新版本后，老用户会继续用缓存里的旧 JS——
    表现是「改了前端，刷新看不到」，而且会持续一段时间。
    配合 ETag 校验只是 304，代价很小。
    """
    for path in ("/", "/quiz.html", "/assets/runtime/app.js", "/assets/app.css"):
        assert "no-cache" in _cache_control(client, path), path


def test_fonts_are_cached_forever(client) -> None:  # noqa: ANN001
    """KaTeX 字体内容永不改变，长缓存 + immutable。"""
    header = _cache_control(client, "/assets/fonts/KaTeX_Main-Regular.woff2")
    assert "immutable" in header
    assert "max-age=31536000" in header


def test_api_responses_are_not_cached(client) -> None:  # noqa: ANN001
    """接口响应不带长缓存 —— 进度是实时的。"""
    header = _cache_control(client, "/api/health")
    assert "max-age=31536000" not in header
