"""路由契约的系统性检查。

这里放的都是「一次能覆盖全部路由」的检查，而不是逐个接口断言 ——
踩过的坑是同一类错误犯三次：把读接口写成写接口的依赖
（`AuthenticatedWriter` 内含 CSRF 双提交校验），浏览器发出不带
CSRF 头的 `GET` 时直接 403，而且只有真跑起来才发现。

与其为每个 `GET` 手写一遍断言，不如遍历路由表一次性兜住，
新增接口自动纳入检查范围。
"""

from __future__ import annotations

from fastapi.routing import APIRoute

from app.main import create_app

CSRF_DEPENDENCIES = {"require_csrf", "get_authenticated_writer"}
READ_METHODS = {"GET", "HEAD", "OPTIONS"}


def _dependant_names(dependant) -> set[str]:  # noqa: ANN001
    names = {getattr(dependant.call, "__name__", "")}
    for child in dependant.dependencies:
        names |= _dependant_names(child)
    return names


def _api_routes() -> list[APIRoute]:
    return [route for route in create_app().routes if isinstance(route, APIRoute)]


def test_read_routes_do_not_require_csrf() -> None:
    """读接口不能挂 CSRF 依赖 —— 浏览器不会给 GET 带 CSRF 头。"""
    offenders = []
    for route in _api_routes():
        if not route.methods or route.methods - READ_METHODS:
            continue
        # /api/auth/* 的读接口不需要登录，也不该校验 CSRF
        if "csrf" in route.path.lower():
            continue
        if _dependant_names(route.dependant) & CSRF_DEPENDENCIES:
            offenders.append(f"{sorted(route.methods)} {route.path}")
    assert not offenders, "这些读接口挂了 CSRF 依赖，浏览器会拿到 403：" + "；".join(offenders)


def test_write_routes_require_csrf_or_same_origin() -> None:
    """写接口要么走带 CSRF 的依赖，要么显式做同源校验。

    登录/注册这类「尚未持有 CSRF 令牌」的请求以前者之外的方式防护，
    但绝不能两者都没有。
    """
    unprotected = []
    for route in _api_routes():
        if not route.methods or not (route.methods - READ_METHODS):
            continue
        names = _dependant_names(route.dependant)
        if names & CSRF_DEPENDENCIES or "verify_same_origin" in names:
            continue
        unprotected.append(f"{sorted(route.methods)} {route.path}")
    assert not unprotected, "这些写接口既没有 CSRF 也没有同源校验：" + "；".join(unprotected)


def test_every_api_route_is_under_api_prefix() -> None:
    """业务路由统一挂在 /api 下，静态资源与 SPA 兜底才不会误吞接口请求。"""
    stray = [route.path for route in _api_routes() if not route.path.startswith("/api")]
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
