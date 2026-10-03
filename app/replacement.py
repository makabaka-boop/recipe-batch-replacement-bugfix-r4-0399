"""Shared-subrecipe replacement plans and immutable release snapshots."""

from __future__ import annotations

import json
from . import service
from .db import transaction


def initialize(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS replacement_plans (
        id INTEGER PRIMARY KEY, document TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open', result TEXT)"""
    )


def _id(value):
    if type(value) is not int or value <= 0:
        raise service.ServiceError("标识必须是正整数", 422)
    return value


def _targets(conn, targets):
    if not isinstance(targets, dict) or not targets:
        raise service.ServiceError("替代单至少包含一个批次", 422)
    normalized = {}
    for old, entry in targets.items():
        try:
            old = _id(int(old))
        except (ValueError, TypeError):
            raise service.ServiceError("原批次标识无效", 422)
        if str(old) in normalized or not isinstance(entry, dict):
            raise service.ServiceError("原批次重复或替代资料无效", 422)
        material = _id(entry.get("material_id"))
        batch = _id(entry.get("batch_id"))
        if not conn.execute("SELECT 1 FROM batches WHERE id=?", (old,)).fetchone():
            raise service.ServiceError("原批次不存在", 404)
        row = conn.execute("SELECT * FROM batches WHERE id=?", (batch,)).fetchone()
        if row is None or row["material_id"] != material:
            raise service.ServiceError("替代批次与原料不匹配", 422)
        if service.effective_batch_status(row) != "active":
            raise service.ServiceError("替代批次已失效", 422)
        if old == batch:
            raise service.ServiceError("替代必须使用不同批次", 422)
        normalized[str(old)] = {"material_id": material, "batch_id": batch}
    return normalized


def _graph(conn, roots, targets):
    order = []
    fingerprints = {}
    heads = {}
    used = set()
    for root in roots:
        row = conn.execute(
            "SELECT * FROM recipe_versions WHERE id=?", (root,)
        ).fetchone()
        if row is None:
            raise service.ServiceError("版本不存在", 404)
        order.append(root)
        heads[str(row["recipe_id"])] = root
        fingerprints[str(root)] = row["content_hash"]
        for comp in conn.execute(
            "SELECT * FROM components WHERE recipe_version_id=?", (root,)
        ):
            if comp["kind"] == "subrecipe":
                order.append(comp["child_version_id"])
            elif str(comp["batch_id"]) in targets:
                used.add(str(comp["batch_id"]))
    return {
        "roots": roots,
        "targets": targets,
        "order": order,
        "heads": heads,
        "fingerprints": fingerprints,
    }, {}


def preview(conn, roots, targets, created_by):
    initialize(conn)
    if not isinstance(roots, list) or not roots or len(roots) > 20:
        raise service.ServiceError("请提供一至二十个产品版本", 422)
    roots = [_id(x) for x in roots]
    if len(set(roots)) != len(roots):
        raise service.ServiceError("产品版本不能重复", 422)
    if not isinstance(created_by, str) or not created_by.strip():
        raise service.ServiceError("请提供起草人", 422)
    with transaction(conn):
        targets = _targets(conn, targets)
        plan, _ = _graph(conn, roots, targets)
        plan["created_by"] = created_by.strip()
        cur = conn.execute(
            "INSERT INTO replacement_plans(document) VALUES (?)",
            (json.dumps(plan, ensure_ascii=False, sort_keys=True),),
        )
        return get_plan(conn, cur.lastrowid)


def get_plan(conn, plan_id):
    initialize(conn)
    row = conn.execute(
        "SELECT * FROM replacement_plans WHERE id=?", (_id(plan_id),)
    ).fetchone()
    if row is None:
        raise service.ServiceError("替代单不存在", 404)
    return {
        "id": row["id"],
        "status": row["status"],
        "preview": json.loads(row["document"]),
        "result": json.loads(row["result"]) if row["result"] else None,
    }


def apply(conn, plan_id):
    view = get_plan(conn, plan_id)
    if view["status"] == "applied":
        return view
    plan = view["preview"]
    result = {"versions": {}, "roots": []}

    def replace(vid):
        row = conn.execute(
            "SELECT * FROM recipe_versions WHERE id=?", (vid,)
        ).fetchone()
        comps = []
        for c in conn.execute(
            "SELECT * FROM components WHERE recipe_version_id=? ORDER BY position",
            (vid,),
        ).fetchall():
            if c["kind"] == "material":
                target = plan["targets"].get(str(c["batch_id"]), {})
                comps.append(
                    {
                        "kind": "material",
                        "qty": c["qty"],
                        "material_id": target.get("material_id", c["material_id"]),
                        "batch_id": target.get("batch_id", c["batch_id"]),
                    }
                )
            else:
                child = c["child_version_id"]
                direct = conn.execute(
                    "SELECT 1 FROM components WHERE recipe_version_id=? AND batch_id IN ("
                    + ",".join("?" for _ in plan["targets"])
                    + ")",
                    (child, *map(int, plan["targets"])),
                ).fetchone()
                if direct:
                    child = replace(child)
                comps.append(
                    {"kind": "subrecipe", "qty": c["qty"], "child_version_id": child}
                )
        new = service.create_version(
            conn,
            row["recipe_id"],
            plan["created_by"],
            comps,
            note=f"联动替代单 #{plan_id}",
            expected_current_version_id=vid,
        )
        conn.commit()
        result["versions"][str(vid)] = new
        return new

    for root in plan["roots"]:
        result["roots"].append(replace(root))
    conn.execute(
        "UPDATE replacement_plans SET status='applied',result=? WHERE id=?",
        (json.dumps(result, sort_keys=True), plan_id),
    )
    return get_plan(conn, plan_id)
