"""联动替代单：一组产品版本共用子配方的整单替换与不可变快照。

规则（与 README「联动替代单」一致）：
- 预览只登记替代单，不改动任何配方；范围覆盖产品引用链上所有使用目标批次的
  组件（含多级子配方传递）；同一旧版本在同一单中只产生一个替代版本，
  不涉及替代的分支沿用原版本；
- 受影响引用必须都是各配方的当前版本，否则预览拒绝；
- 采纳是整单原子操作：预览后受影响配方被修改、替代批次失效或任一环节失败，
  整单不生效，不留下部分生效的版本与审批状态变化；
- 采纳成功后重复查询 / 重复采纳返回同一结果，不重复创建版本；
- 已放行历史与不受影响的配方保持原有证据，新版本重新走双人复核。
"""
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
    """校验并规范化 原批次 -> {material_id, batch_id} 对应表。"""
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


def _batch_code(conn, batch_id):
    row = conn.execute("SELECT code FROM batches WHERE id=?", (batch_id,)).fetchone()
    return row["code"] if row else f"#{batch_id}"


def _analyze(conn, roots, targets):
    """完整展开各产品版本的引用链，定位所有使用目标批次的组件。

    返回 (affected, changes, recipes)：
      affected  需要替代的旧版本 id，自底向上（子配方先于引用它的父配方，去重）
      changes   批次替换明细（哪个版本、哪个原批次换成哪个新批次）
      recipes   受影响 recipe_id -> 旧版本 id（采纳时据此复核预览后未被改动）
    """
    affected: list[int] = []
    changes: list[dict] = []
    recipes: dict[str, int] = {}
    memo: dict[int, bool] = {}

    def walk(vid, stack):
        if vid in memo:
            return memo[vid]
        if vid in stack:
            raise service.ServiceError(
                f"检测到循环引用：{service._version_label(conn, vid)}", 422)
        row = conn.execute(
            "SELECT * FROM recipe_versions WHERE id=?", (vid,)).fetchone()
        if row is None:
            raise service.ServiceError(f"配方版本 #{vid} 不存在", 404)
        hit = False
        for c in conn.execute(
            "SELECT * FROM components WHERE recipe_version_id=? ORDER BY position", (vid,)
        ):
            if c["kind"] == "material":
                target = targets.get(str(c["batch_id"]))
                if target is not None:
                    hit = True
                    changes.append({
                        "version_id": vid,
                        "recipe": service._version_label(conn, vid),
                        "position": c["position"],
                        "old_batch_id": c["batch_id"],
                        "old_batch_code": _batch_code(conn, c["batch_id"]),
                        "new_material_id": target["material_id"],
                        "new_batch_id": target["batch_id"],
                        "new_batch_code": _batch_code(conn, target["batch_id"]),
                    })
            elif walk(c["child_version_id"], stack | {vid}):
                hit = True
        memo[vid] = hit
        if hit:
            recipe = conn.execute(
                "SELECT * FROM recipes WHERE id=?", (row["recipe_id"],)).fetchone()
            if recipe["current_version_id"] != vid:
                raise service.ServiceError(
                    f"配方「{recipe['code']}」受影响版本 v{row['version_no']} 已不是"
                    "当前版本：受影响引用必须都是各配方当前版本，预览拒绝", 409)
            recipes[str(row["recipe_id"])] = vid
            affected.append(vid)
        return hit

    for root in roots:
        if not walk(root, set()):
            raise service.ServiceError(
                f"产品版本 {service._version_label(conn, root)} 的引用链上"
                "未使用任何目标批次", 422)
    used = {c["old_batch_id"] for c in changes}
    for old in targets:
        if int(old) not in used:
            raise service.ServiceError(
                f"原批次「{_batch_code(conn, int(old))}」未出现在任何产品引用链上", 422)
    return affected, changes, recipes


def preview(conn, roots, targets, created_by):
    """登记替代单并返回整组预览；不改动任何配方版本。"""
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
        affected, changes, recipes = _analyze(conn, roots, targets)
        affected_docs = []
        for vid in affected:
            v = conn.execute(
                "SELECT recipe_id, content_hash FROM recipe_versions WHERE id=?",
                (vid,),
            ).fetchone()
            affected_docs.append({
                "version_id": vid,
                "recipe_id": v["recipe_id"],
                "recipe": service._version_label(conn, vid),
                "content_hash": v["content_hash"],
            })
        plan = {
            "roots": roots,
            "targets": targets,
            "created_by": created_by.strip(),
            "affected": affected_docs,
            "recipes": recipes,
            "changes": changes,
        }
        cur = conn.execute(
            "INSERT INTO replacement_plans(document) VALUES (?)",
            (json.dumps(plan, ensure_ascii=False, sort_keys=True),),
        )
        plan_id = cur.lastrowid
    return get_plan(conn, plan_id)


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
    """采纳整单：为预览涉及的整组配方生成一致的新不可变版本。

    单个 BEGIN IMMEDIATE 事务内完成：先复核（替代批次仍有效、受影响配方
    预览后未被改动），再自底向上逐配方各生成一个新版本；任一环节失败整体
    回滚，不留下部分生效的版本。重复采纳返回首次结果，不重复创建版本。
    """
    initialize(conn)
    with transaction(conn):
        row = conn.execute(
            "SELECT * FROM replacement_plans WHERE id=?", (_id(plan_id),)
        ).fetchone()
        if row is None:
            raise service.ServiceError("替代单不存在", 404)
        if row["status"] != "applied":
            plan = json.loads(row["document"])
            targets = plan["targets"]
            # 复核一：预览后替代批次失效 -> 整单不生效
            for t in targets.values():
                b = conn.execute(
                    "SELECT * FROM batches WHERE id=?", (t["batch_id"],)).fetchone()
                if b is None or service.effective_batch_status(b) != "active":
                    raise service.ServiceError(
                        f"替代批次「{b['code'] if b else t['batch_id']}」已失效，"
                        "整单不生效；请重新选择批次并预览", 422)
            # 复核二：预览后受影响配方产生新版本 -> 整单不生效
            for rid, vid in plan["recipes"].items():
                cur = conn.execute(
                    "SELECT current_version_id FROM recipes WHERE id=?", (int(rid),)
                ).fetchone()
                if cur is None or cur["current_version_id"] != vid:
                    raise service.ServiceError(
                        "预览后受影响配方已被他人修改（产生新版本），整单不生效；"
                        "请基于最新版本重新预览", 409)

            memo: dict[int, int] = {}
            new_versions: dict[str, int] = {}

            def replace(vid):
                """返回 vid 的替代版本；未受影响的版本原样沿用（同一旧版本只替代一次）。"""
                if vid in memo:
                    return memo[vid]
                vrow = conn.execute(
                    "SELECT * FROM recipe_versions WHERE id=?", (vid,)).fetchone()
                comps = []
                changed = False
                for c in conn.execute(
                    """SELECT * FROM components WHERE recipe_version_id=?
                         ORDER BY position""",
                    (vid,),
                ).fetchall():
                    if c["kind"] == "material":
                        t = targets.get(str(c["batch_id"]))
                        if t is not None:
                            changed = True
                            comps.append({
                                "kind": "material", "qty": c["qty"],
                                "material_id": t["material_id"],
                                "batch_id": t["batch_id"],
                            })
                        else:
                            comps.append({
                                "kind": "material", "qty": c["qty"],
                                "material_id": c["material_id"],
                                "batch_id": c["batch_id"],
                            })
                    else:
                        child = replace(c["child_version_id"])
                        if child != c["child_version_id"]:
                            changed = True
                        comps.append({
                            "kind": "subrecipe", "qty": c["qty"],
                            "child_version_id": child,
                        })
                if not changed:
                    memo[vid] = vid
                    return vid
                new_vid = service.create_version(
                    conn,
                    vrow["recipe_id"],
                    plan["created_by"],
                    comps,
                    note=f"联动替代单 #{plan_id}",
                    expected_current_version_id=vid,
                )
                memo[vid] = new_vid
                new_versions[str(vid)] = new_vid
                return new_vid

            new_roots = [replace(v) for v in plan["roots"]]
            result = {
                "versions": new_versions,
                "roots": new_roots,
                "applied_on": service.now_iso(),
            }
            conn.execute(
                "UPDATE replacement_plans SET status='applied', result=? WHERE id=?",
                (json.dumps(result, ensure_ascii=False, sort_keys=True), plan_id),
            )
    return get_plan(conn, plan_id)
