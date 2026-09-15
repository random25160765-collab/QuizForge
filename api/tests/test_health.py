"""骨架自检：应用能装配、数据库能连、健康检查不泄露密钥。"""

from __future__ import annotations


def test_health_ok(client) -> None:  # noqa: ANN001
    """只断言「形状与连通性」，不断言具体题数。

    题数取决于题库是否已导入，而用例执行顺序不该影响结论 ——
    「导入后健康检查要报出规模」由下面那条用例负责。
    """
    resp = client.get("/api/health")
    assert resp.status_code == 200

    body = resp.json()
    assert body["ok"] is True
    assert body["database"]["ok"] is True
    assert isinstance(body["bank"]["questions"], int)
    assert isinstance(body["bank"]["topics"], int)
    assert body["bank"]["loaded"] == (body["bank"]["questions"] > 0)


def test_health_reports_bank_scale(client, imported_bank) -> None:  # noqa: ANN001
    """题库导入后，健康检查要报出真实规模与版本哈希。"""
    body = client.get("/api/health").json()
    assert body["bank"]["loaded"] is True
    assert body["bank"]["questions"] == len(imported_bank.questions)
    assert body["bank"]["versionHash"] == imported_bank.content_hash


def test_health_never_leaks_api_key(client) -> None:  # noqa: ANN001
    """健康检查只报告「配没配」，绝不回显密钥。"""
    raw = client.get("/api/health").text
    assert "apiKey" not in raw
    assert "api_key" not in raw
    assert "sk-" not in raw


def test_unknown_api_path_returns_json(client) -> None:  # noqa: ANN001
    """API 路径下的 404 必须是 JSON，前端 api.js 假定响应体可解析。"""
    resp = client.get("/api/definitely-not-here")
    assert resp.status_code in (404, 405)
    assert resp.headers["content-type"].startswith("application/json")


def test_request_id_header(client) -> None:  # noqa: ANN001
    """每个响应带请求 id，便于把浏览器报错和服务器日志对上。"""
    resp = client.get("/api/health")
    assert resp.headers.get("x-request-id")


def test_health_exposes_subject_list_for_landing(client, imported_bank) -> None:  # noqa: ANN001
    """首页的「覆盖范围」靠这份数据渲染，字段名与口径都是契约。

    题数按**子树**汇总：题挂在知识点一级，而首页要显示学科总量，
    所以各学科题数之和必须等于总题数。
    """
    bank = client.get("/api/health").json()["bank"]
    subjects = bank["subjectList"]

    assert subjects, "导入题库后应当有一级学科"
    assert all({"key", "name", "color", "count"} <= set(item) for item in subjects)
    assert all(item["count"] > 0 for item in subjects), "空的学科不该出现"
    assert sum(item["count"] for item in subjects) == bank["questions"]
    # 学科数应当远小于主题总数（后者含单元与知识点）
    assert len(subjects) < bank["topics"]


def test_health_reports_type_count(client, imported_bank) -> None:  # noqa: ANN001
    """题型数只数真正出现过的题型 —— 与离线首页的口径一致。"""
    bank = client.get("/api/health").json()["bank"]
    assert 1 <= bank["typeCount"] <= 5
