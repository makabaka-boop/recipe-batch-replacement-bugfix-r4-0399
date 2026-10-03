"""联动替代单：整组一致采纳、原子性（不留部分生效版本）、历史证据保留、
不涉及分支沿用原版本、新版本重新双人复核、幂等与并发。"""
import threading

import pytest

from app import service
from app.db import new_conn, transaction
from app.replacement import apply, get_plan, preview
from tests.conftest import ids_by_code


def _version(conn, recipe_code: str, no: int) -> int:
    return conn.execute(
        """SELECT v.id FROM recipe_versions v JOIN recipes r ON r.id=v.recipe_id
            WHERE r.code=? AND v.version_no=?""",
        (recipe_code, no),
    ).fetchone()["id"]


def _batch(conn, code: str) -> int:
    return conn.execute("SELECT id FROM batches WHERE code=?", (code,)).fetchone()["id"]


def _material(conn, code: str) -> int:
    return conn.execute("SELECT id FROM materials WHERE code=?", (code,)).fetchone()["id"]


def _milk_to_oat(conn) -> dict:
    return {str(_batch(conn, "B-MILK-2609")): {
        "material_id": _material(conn, "OATMILK"), "batch_id": _batch(conn, "B-OAT-2610")}}


def _components(conn, vid: int):
    return conn.execute(
        "SELECT * FROM components WHERE recipe_version_id=? ORDER BY position", (vid,)
    ).fetchall()


def _current(conn, recipe_code: str) -> int:
    return conn.execute(
        "SELECT current_version_id FROM recipes WHERE code=?", (recipe_code,)
    ).fetchone()["current_version_id"]


def _version_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) n FROM recipe_versions").fetchone()["n"]


# ---------------- 整组一致结果 ----------------
def test_apply_replaces_shared_chain_exactly_once(fresh_db):
    """多级链 + 两个产品共用子配方：每个受影响配方只生成一个新版本。"""
    conn = fresh_db
    sign2 = _version(conn, "RC-SIGN", 2)
    cake1 = _version(conn, "RC-CAKE", 1)
    custard1 = _version(conn, "RC-CUSTARD", 1)

    plan = preview(conn, [sign2, cake1], _milk_to_oat(conn), "审核员-联动")
    assert plan["status"] == "open"
    # 预览自底向上覆盖整条链：卡仕达 -> 蛋糕胚 -> 招牌
    assert [a["version_id"] for a in plan["preview"]["affected"]] == [custard1, cake1, sign2]
    assert {c["old_batch_code"] for c in plan["preview"]["changes"]} == {"B-MILK-2609"}
    assert {c["new_batch_code"] for c in plan["preview"]["changes"]} == {"B-OAT-2610"}

    done = apply(conn, plan["id"])
    assert done["status"] == "applied"
    versions = done["result"]["versions"]
    # 同一子配方在同一单中只有一个替代结果（蛋糕胚既是产品又是招牌的子配方）
    assert set(versions) == {str(custard1), str(cake1), str(sign2)}
    custard2, cake2, sign3 = (versions[str(custard1)], versions[str(cake1)], versions[str(sign2)])
    assert done["result"]["roots"] == [sign3, cake2]

    # 新链结构一致：招牌 v3 -> 蛋糕胚 v2 -> 卡仕达 v2 -> 燕麦奶批次
    sign3_sub = next(c for c in _components(conn, sign3) if c["kind"] == "subrecipe")
    assert sign3_sub["child_version_id"] == cake2
    cake2_sub = next(c for c in _components(conn, cake2) if c["kind"] == "subrecipe")
    assert cake2_sub["child_version_id"] == custard2
    oat_batch = _batch(conn, "B-OAT-2610")
    assert any(c["batch_id"] == oat_batch for c in _components(conn, custard2))

    # 标签清单 / 传播路径对应同一次替代：MILK 消失，旧批次不再出现在新链
    graph = service.resolve_graph(conn, sign3)
    labels = {l["code"] for l in graph["labels"]}
    assert "MILK" not in labels
    assert {"EGG", "GLUTEN", "PEANUT", "SOY", "NUT"} <= labels
    assert all(b["batch_id"] != _batch(conn, "B-MILK-2609") for b in graph["batches"])

    # 每个受影响配方只前进一个版本
    assert _current(conn, "RC-CUSTARD") == custard2
    assert _current(conn, "RC-CAKE") == cake2
    assert _current(conn, "RC-SIGN") == sign3


def test_unaffected_branches_keep_original_versions(fresh_db):
    """只替换大豆卵磷脂：蛋糕胚/卡仕达不受影响，不产生新版本、证据不变。"""
    conn = fresh_db
    sign2 = _version(conn, "RC-SIGN", 2)
    cake1 = _version(conn, "RC-CAKE", 1)
    custard1 = _version(conn, "RC-CUSTARD", 1)
    targets = {str(_batch(conn, "B-SOY-2607")): {
        "material_id": _material(conn, "EGG"), "batch_id": _batch(conn, "B-EGG-2609")}}

    plan = preview(conn, [sign2], targets, "审核员-联动")
    assert [a["version_id"] for a in plan["preview"]["affected"]] == [sign2]
    done = apply(conn, plan["id"])

    versions = done["result"]["versions"]
    assert list(versions) == [str(sign2)]  # 只有招牌产生新版本
    assert _current(conn, "RC-CAKE") == cake1
    assert _current(conn, "RC-CUSTARD") == custard1
    # 不涉及替代的分支沿用原版本
    sign3 = versions[str(sign2)]
    sub = next(c for c in _components(conn, sign3) if c["kind"] == "subrecipe")
    assert sub["child_version_id"] == cake1
    labels = {l["code"] for l in service.resolve_graph(conn, sign3)["labels"]}
    assert "SOY" not in labels and "MILK" in labels  # 其余链原样保留


# ---------------- 原子性：整单不生效 ----------------
def test_apply_atomic_when_recipe_changed_after_preview(fresh_db):
    """预览后他人修改受影响配方：采纳 409，且不留下任何部分生效的版本/审批变化。"""
    conn = fresh_db
    sign2 = _version(conn, "RC-SIGN", 2)
    cake1 = _version(conn, "RC-CAKE", 1)
    custard1 = _version(conn, "RC-CUSTARD", 1)
    plan = preview(conn, [sign2, cake1], _milk_to_oat(conn), "审核员-联动")

    # 预览后他人基于卡仕达当前版本提交了新版本
    ids = ids_by_code(conn, "MILK", "EGG", "FLOUR")
    with transaction(conn):
        service.create_version(conn, 1, "起草人-赵配方", [
            {"kind": "material", "material_id": ids["MILK"],
             "batch_id": _batch(conn, "B-MILK-2609"), "qty": "380g"},
            {"kind": "material", "material_id": ids["EGG"],
             "batch_id": _batch(conn, "B-EGG-2609"), "qty": "200g"},
            {"kind": "material", "material_id": ids["FLOUR"],
             "batch_id": _batch(conn, "B-FLOUR-2609"), "qty": "60g"},
        ], note="他人修改", expected_current_version_id=custard1)

    before = _version_count(conn)
    with pytest.raises(service.ServiceError) as ei:
        apply(conn, plan["id"])
    assert ei.value.status == 409

    # 整单不生效：无新增版本、头指针未动、审批状态未被波及
    assert _version_count(conn) == before
    assert _current(conn, "RC-CAKE") == cake1
    assert _current(conn, "RC-SIGN") == sign2
    reviewer_a = conn.execute(
        "SELECT id FROM reviewers WHERE name='审核者A-王敏'").fetchone()["id"]
    a = conn.execute(
        "SELECT stale FROM approvals WHERE recipe_version_id=? AND reviewer_id=?",
        (sign2, reviewer_a)).fetchone()
    assert a["stale"] == 0
    # 替代单未提交，可修正后重新采纳
    assert get_plan(conn, plan["id"])["status"] == "open"


def test_apply_atomic_when_replacement_batch_recalled_after_preview(fresh_db):
    """预览后候选批次被召回：采纳 422，整单不生效。"""
    conn = fresh_db
    sign2 = _version(conn, "RC-SIGN", 2)
    plan = preview(conn, [sign2], _milk_to_oat(conn), "审核员-联动")

    with transaction(conn):
        service.recall_batch(conn, _batch(conn, "B-OAT-2610"), "候选批次演练召回")

    before = _version_count(conn)
    with pytest.raises(service.ServiceError) as ei:
        apply(conn, plan["id"])
    assert ei.value.status == 422 and "整单不生效" in str(ei.value)

    assert _version_count(conn) == before
    assert _current(conn, "RC-SIGN") == sign2
    assert _current(conn, "RC-CUSTARD") == _version(conn, "RC-CUSTARD", 1)
    assert get_plan(conn, plan["id"])["status"] == "open"


# ---------------- 历史证据与重新复核 ----------------
def test_released_history_kept_and_new_versions_need_fresh_review(fresh_db):
    conn = fresh_db
    sign1 = _version(conn, "RC-SIGN", 1)
    sign2 = _version(conn, "RC-SIGN", 2)
    custard1 = _version(conn, "RC-CUSTARD", 1)

    plan = preview(conn, [sign2], _milk_to_oat(conn), "审核员-联动")
    done = apply(conn, plan["id"])
    versions = done["result"]["versions"]

    # 已放行历史原样保留：status / released_on / 审批行不动
    r1 = conn.execute(
        "SELECT status, released_on FROM recipe_versions WHERE id=?", (sign1,)).fetchone()
    assert r1["status"] == "released" and r1["released_on"]
    assert conn.execute(
        "SELECT COUNT(*) n FROM approvals WHERE recipe_version_id=? AND stale=0",
        (sign1,)).fetchone()["n"] == 2
    assert conn.execute(
        "SELECT status FROM recipe_versions WHERE id=?", (custard1,)).fetchone()["status"] == "released"

    # 旧待审批快照上的半成品确认随新版本失效（既有规则）
    assert conn.execute(
        "SELECT COUNT(*) n FROM approvals WHERE recipe_version_id=? AND stale=1",
        (sign2,)).fetchone()["n"] == 1

    # 新版本全部回到待审批、零确认，须各自重新完成双人复核
    assert len(versions) == 3
    for vid in versions.values():
        row = conn.execute(
            "SELECT status, needs_review FROM recipe_versions WHERE id=?", (vid,)).fetchone()
        assert row["status"] == "in_approval" and row["needs_review"] == 0
        assert conn.execute(
            "SELECT COUNT(*) n FROM approvals WHERE recipe_version_id=?",
            (vid,)).fetchone()["n"] == 0

    # 新版本可重新走双人放行（子配方未放行会挡住父配方，先放子链）
    rids = {r["name"]: r["id"] for r in conn.execute("SELECT id,name FROM reviewers")}
    custard2 = versions[str(_version(conn, "RC-CUSTARD", 1))]
    with transaction(conn):
        service.approve(conn, custard2, rids["审核者A-王敏"])
        result = service.approve(conn, custard2, rids["审核者B-李强"])
    assert result["released"] is True


# ---------------- 幂等 / 并发 ----------------
def test_apply_is_idempotent_and_get_returns_same_result(fresh_db):
    conn = fresh_db
    sign2 = _version(conn, "RC-SIGN", 2)
    plan = preview(conn, [sign2], _milk_to_oat(conn), "审核员-联动")

    first = apply(conn, plan["id"])
    count = _version_count(conn)
    again = apply(conn, plan["id"])  # 重复采纳不重复创建版本
    assert again["result"] == first["result"]
    assert _version_count(conn) == count
    fetched = get_plan(conn, plan["id"])
    assert fetched["status"] == "applied" and fetched["result"] == first["result"]


def test_concurrent_apply_creates_versions_only_once(fresh_db):
    conn = fresh_db
    sign2 = _version(conn, "RC-SIGN", 2)
    plan = preview(conn, [sign2], _milk_to_oat(conn), "审核员-联动")
    before = _version_count(conn)
    outcomes = []

    def worker():
        c = new_conn()
        try:
            outcomes.append(("ok", apply(c, plan["id"])["result"]))
        except Exception as e:  # noqa: BLE001
            outcomes.append(("err", str(e)))
        finally:
            c.close()

    ts = [threading.Thread(target=worker) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    # 串行化后只有一个事务真正创建版本；两个调用拿到同一份结果
    assert _version_count(conn) == before + 3
    oks = [o[1] for o in outcomes if o[0] == "ok"]
    assert oks and all(r == oks[0] for r in oks)


# ---------------- 预览拒绝 ----------------
def test_preview_rejects_non_current_affected_version(fresh_db):
    """受影响引用不是配方当前版本 -> 预览拒绝（409）。"""
    conn = fresh_db
    sign1 = _version(conn, "RC-SIGN", 1)  # 当前版本是 v2
    with pytest.raises(service.ServiceError) as ei:
        preview(conn, [sign1], _milk_to_oat(conn), "审核员")
    assert ei.value.status == 409


def test_preview_rejects_root_without_target_batch(fresh_db):
    """产品引用链未命中任何目标批次 -> 预览拒绝（422）。"""
    conn = fresh_db
    cake1 = _version(conn, "RC-CAKE", 1)  # 蛋糕胚不用大豆卵磷脂
    targets = {str(_batch(conn, "B-SOY-2607")): {
        "material_id": _material(conn, "EGG"), "batch_id": _batch(conn, "B-EGG-2609")}}
    with pytest.raises(service.ServiceError) as ei:
        preview(conn, [cake1], targets, "审核员")
    assert ei.value.status == 422


def test_preview_rejects_unused_target_batch(fresh_db):
    """目标批次未出现在任何产品引用链上 -> 预览拒绝（422）。"""
    conn = fresh_db
    sign2 = _version(conn, "RC-SIGN", 2)
    targets = {str(_batch(conn, "B-SHR-2609")): {  # 虾仁批次无人使用
        "material_id": _material(conn, "EGG"), "batch_id": _batch(conn, "B-EGG-2609")}}
    with pytest.raises(service.ServiceError) as ei:
        preview(conn, [sign2], targets, "审核员")
    assert ei.value.status == 422


def test_preview_rejects_invalid_replacement_batch(fresh_db):
    """替代批次失效 / 与原批次相同 -> 预览拒绝。"""
    conn = fresh_db
    sign2 = _version(conn, "RC-SIGN", 2)
    old_milk = _batch(conn, "B-MILK-2608-OLD")  # 已过期
    targets = {str(_batch(conn, "B-MILK-2609")): {
        "material_id": _material(conn, "MILK"), "batch_id": old_milk}}
    with pytest.raises(service.ServiceError) as ei:
        preview(conn, [sign2], targets, "审核员")
    assert ei.value.status == 422

    same = {str(_batch(conn, "B-MILK-2609")): {
        "material_id": _material(conn, "MILK"), "batch_id": _batch(conn, "B-MILK-2609")}}
    with pytest.raises(service.ServiceError):
        preview(conn, [sign2], same, "审核员")


# ---------------- HTTP 端到端 ----------------
def test_http_preview_apply_and_views_are_consistent(client, fresh_db):
    conn = fresh_db
    sign2 = _version(conn, "RC-SIGN", 2)
    cake1 = _version(conn, "RC-CAKE", 1)
    milk_batch = _batch(conn, "B-MILK-2609")

    resp = client.post("/api/replacements/preview", json={
        "roots": [sign2, cake1],
        "targets": {str(milk_batch): {
            "material_id": _material(conn, "OATMILK"),
            "batch_id": _batch(conn, "B-OAT-2610")}},
        "created_by": "审核员-联动"})
    assert resp.status_code == 200
    plan = resp.json()
    assert plan["status"] == "open" and plan["result"] is None

    done = client.post(f"/api/replacements/{plan['id']}/apply")
    assert done.status_code == 200
    body = done.json()
    assert body["status"] == "applied"
    versions = body["result"]["versions"]
    sign3 = versions[str(sign2)]

    # 标签清单 / 传播路径 / 版本差异 / 导出对应同一次替代
    g = client.get(f"/api/versions/{sign3}/graph").json()
    assert "MILK" not in [l["code"] for l in g["labels"]]
    diff = client.get(f"/api/diff/{sign2}/{sign3}").json()
    assert "MILK" in diff["labels_removed"]
    export = client.get(f"/api/versions/{sign3}/export?format=json").json()
    v = client.get(f"/api/versions/{sign3}").json()
    assert export["content_hash"] == v["content_hash"]
    assert v["status"] == "in_approval" and v["approvals"] == []

    # 重复采纳 / 查询返回同一结果，不重复创建版本
    again = client.post(f"/api/replacements/{plan['id']}/apply")
    assert again.status_code == 200 and again.json()["result"] == body["result"]
    fetched = client.get(f"/api/replacements/{plan['id']}")
    assert fetched.status_code == 200 and fetched.json()["result"] == body["result"]


def test_http_apply_failure_leaves_no_partial_effect(client, fresh_db):
    """HTTP 层：预览后配方被修改 -> 采纳 409，其他产品版本与审批不变。"""
    conn = fresh_db
    sign2 = _version(conn, "RC-SIGN", 2)
    cake1 = _version(conn, "RC-CAKE", 1)
    custard1 = _version(conn, "RC-CUSTARD", 1)
    milk_batch = _batch(conn, "B-MILK-2609")

    plan = client.post("/api/replacements/preview", json={
        "roots": [sign2, cake1],
        "targets": {str(milk_batch): {
            "material_id": _material(conn, "OATMILK"),
            "batch_id": _batch(conn, "B-OAT-2610")}},
        "created_by": "审核员-联动"}).json()

    ids = ids_by_code(conn, "MILK", "EGG", "FLOUR")
    with transaction(conn):
        service.create_version(conn, 1, "起草人-赵配方", [
            {"kind": "material", "material_id": ids["MILK"],
             "batch_id": milk_batch, "qty": "380g"},
            {"kind": "material", "material_id": ids["EGG"],
             "batch_id": _batch(conn, "B-EGG-2609"), "qty": "200g"},
            {"kind": "material", "material_id": ids["FLOUR"],
             "batch_id": _batch(conn, "B-FLOUR-2609"), "qty": "60g"},
        ], note="他人修改", expected_current_version_id=custard1)

    before = _version_count(conn)
    resp = client.post(f"/api/replacements/{plan['id']}/apply")
    assert resp.status_code == 409
    assert _version_count(conn) == before
    assert _current(conn, "RC-CAKE") == cake1
    assert _current(conn, "RC-SIGN") == sign2
    assert client.get(f"/api/replacements/{plan['id']}").json()["status"] == "open"
