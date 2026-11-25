#!/usr/bin/env python3
"""注册机多渠道推送补丁：在**线上版本**上做锚点替换，保留线上独有功能。

为什么不能整文件覆盖：
线上 `/opt/gpt-outlook-register` 与本地工作树并不同步 —— 线上有 156 处未提交改动，
本地只有 116 处。具体缺失的是 `udealproxy`（app.py 25 处、前端 3 个 chunk）和
db.py 的「新号优先」取号优化（本地 1 处，线上 4 处）。覆盖式部署会删掉这些功能。

因此本脚本只在线上文件上做定点插入/替换，且：
  * 线上文件是 CRLF，脚本读入时归一化为 LF、写回时还原 CRLF，保证 diff 最小；
  * 锚点必须唯一命中，否则中止（宁可不动）；
  * 幂等：探针已存在则跳过，重复执行不重复插入；
  * 写入前备份、写入后 py_compile，失败自动回滚。

用法：
    python3 patch-register-push-targets.py --root /opt/gpt-outlook-register --verify
    python3 patch-register-push-targets.py --root /opt/gpt-outlook-register
    python3 patch-register-push-targets.py --root <dir> --restore <备份时间戳>
"""

from __future__ import annotations

import argparse
import py_compile
import shutil
import tempfile
import time
from pathlib import Path

MARKER = "c2a-push-targets"
BACKUP_ROOT = Path("/root/c2a-register-backup")


# ---------------------------------------------------------------------------
# db.py
# ---------------------------------------------------------------------------

DB_IMPORT_ANCHOR = "import json\nimport sqlite3"
DB_IMPORT_REPLACEMENT = "import json\nimport random\nimport sqlite3"

DB_TABLE_ANCHOR = """        CREATE TABLE IF NOT EXISTS runs (
            run_id          TEXT PRIMARY KEY,
            email           TEXT,
            status          TEXT,        -- running / done / failed
            started_at      REAL,
            finished_at     REAL,
            log_path        TEXT,
            error           TEXT,
            error_category  TEXT         -- network / account / unknown
        );
    \"\"\")"""

DB_TABLE_REPLACEMENT = """        CREATE TABLE IF NOT EXISTS runs (
            run_id          TEXT PRIMARY KEY,
            email           TEXT,
            status          TEXT,        -- running / done / failed
            started_at      REAL,
            finished_at     REAL,
            log_path        TEXT,
            error           TEXT,
            error_category  TEXT         -- network / account / unknown
        );

        -- 推送目标：取代原来三个写死的导出槽位。
        -- 每次注册成功按 weight 随机选一个 enabled 目标推送，默认全为 1 即 1:1。
        CREATE TABLE IF NOT EXISTS push_targets (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            type            TEXT NOT NULL,   -- cpa / sub2api / chatgpt2api / chat2api_go
            url             TEXT NOT NULL,
            credential      TEXT,            -- 管理密钥
            group_id        TEXT,
            weight          INTEGER NOT NULL DEFAULT 1,
            enabled         INTEGER NOT NULL DEFAULT 1,
            timeout         INTEGER NOT NULL DEFAULT 15,
            fail_count      INTEGER NOT NULL DEFAULT 0,
            last_error      TEXT,
            created_at      REAL,
            updated_at      REAL
        );
    \"\"\")"""

DB_TAIL_ANCHOR = """# 模块加载时自动建表
init_db()"""

DB_TAIL_REPLACEMENT = r'''# ──────────────────────── 推送目标（push_targets） ────────────────────────


PUSH_TARGET_TYPES = ("cpa", "sub2api", "chatgpt2api", "chat2api_go")
PUSH_TARGET_TYPE_LABELS = {
    "cpa": "CPA",
    "sub2api": "SUB2API",
    "chatgpt2api": "ChatGPT2API(Python)",
    "chat2api_go": "chat2api-Go",
}
_MIGRATION_KEY = "push_targets_migrated_v2"


def _target_row(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["weight"] = int(item.get("weight") or 1)
    item["enabled"] = bool(item.get("enabled"))
    item["timeout"] = int(item.get("timeout") or 15)
    item["fail_count"] = int(item.get("fail_count") or 0)
    return item


def list_push_targets(*, include_disabled: bool = True) -> list[dict]:
    con = _conn()
    try:
        sql = "SELECT * FROM push_targets"
        if not include_disabled:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id"
        return [_target_row(r) for r in con.execute(sql).fetchall()]
    finally:
        con.close()


def get_push_target(target_id: int) -> Optional[dict]:
    con = _conn()
    try:
        row = con.execute("SELECT * FROM push_targets WHERE id=?", (int(target_id),)).fetchone()
        return _target_row(row) if row else None
    finally:
        con.close()


def create_push_target(data: dict) -> int:
    now = time.time()
    kind = str(data.get("type") or "").strip()
    if kind not in PUSH_TARGET_TYPES:
        raise ValueError(f"unsupported target type: {kind}")
    con = _conn()
    try:
        with _lock:
            cur = con.execute(
                "INSERT INTO push_targets"
                "(name, type, url, credential, group_id, weight, enabled, timeout,"
                " fail_count, last_error, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,0,NULL,?,?)",
                (
                    str(data.get("name") or kind).strip() or kind,
                    kind,
                    str(data.get("url") or "").strip(),
                    str(data.get("credential") or "").strip(),
                    str(data.get("group_id") or "").strip(),
                    max(1, int(data.get("weight") or 1)),
                    1 if data.get("enabled", True) else 0,
                    max(1, int(data.get("timeout") or 15)),
                    now,
                    now,
                ),
            )
            con.commit()
            return int(cur.lastrowid)
    finally:
        con.close()


def update_push_target(target_id: int, data: dict) -> bool:
    fields: list[str] = []
    params: list = []
    for key, coerce in (
        ("name", lambda v: str(v or "").strip()),
        ("type", lambda v: str(v or "").strip()),
        ("url", lambda v: str(v or "").strip()),
        ("credential", lambda v: str(v or "").strip()),
        ("group_id", lambda v: str(v or "").strip()),
        ("weight", lambda v: max(1, int(v or 1))),
        ("enabled", lambda v: 1 if v else 0),
        ("timeout", lambda v: max(1, int(v or 15))),
    ):
        if key not in data:
            continue
        value = coerce(data[key])
        if key == "type" and value not in PUSH_TARGET_TYPES:
            raise ValueError(f"unsupported target type: {value}")
        # 密文字段传 '***' 表示「不修改」，与既有导出配置的约定一致
        if key == "credential" and value == "***":
            continue
        fields.append(f"{key}=?")
        params.append(value)
    if not fields:
        return False
    fields.append("updated_at=?")
    params.append(time.time())
    params.append(int(target_id))
    con = _conn()
    try:
        with _lock:
            cur = con.execute(
                f"UPDATE push_targets SET {', '.join(fields)} WHERE id=?", params
            )
            con.commit()
            return cur.rowcount > 0
    finally:
        con.close()


def delete_push_target(target_id: int) -> bool:
    con = _conn()
    try:
        with _lock:
            cur = con.execute("DELETE FROM push_targets WHERE id=?", (int(target_id),))
            con.commit()
            return cur.rowcount > 0
    finally:
        con.close()


def record_push_result(target_id: int, ok: bool, message: str = "") -> None:
    """记录一次推送结果。失败累加 fail_count，便于前端看出哪个目标在坏。"""
    con = _conn()
    try:
        with _lock:
            if ok:
                con.execute(
                    "UPDATE push_targets SET fail_count=0, last_error=NULL, updated_at=? WHERE id=?",
                    (time.time(), int(target_id)),
                )
            else:
                con.execute(
                    "UPDATE push_targets SET fail_count=fail_count+1, last_error=?, updated_at=? WHERE id=?",
                    (str(message or "")[:500], time.time(), int(target_id)),
                )
            con.commit()
    finally:
        con.close()


def pick_push_target() -> Optional[dict]:
    """按权重随机挑一个启用中的目标。

    权重是「相对比例」而不是绝对配额：全为 1 即 1:1，配 3:1 即 75%/25%。
    用整数权重逐个累加后取随机点，避免浮点误差。
    """
    targets = [t for t in list_push_targets(include_disabled=False) if t["weight"] > 0]
    if not targets:
        return None
    total = sum(t["weight"] for t in targets)
    point = random.uniform(0, total)
    cursor = 0.0
    for target in targets:
        cursor += target["weight"]
        if point <= cursor:
            return target
    return targets[-1]


def mark_registered_push(email: str, payload: dict) -> None:
    """把推送结果写进 registered.extra_json.push（不改表结构）。"""
    con = _conn()
    try:
        with _lock:
            row = con.execute("SELECT extra_json FROM registered WHERE email=?", (email,)).fetchone()
            if not row:
                return
            try:
                extra = json.loads(row["extra_json"] or "{}")
                if not isinstance(extra, dict):
                    extra = {}
            except (TypeError, ValueError):
                extra = {}
            extra["push"] = payload
            con.execute(
                "UPDATE registered SET extra_json=? WHERE email=?",
                (json.dumps(extra, ensure_ascii=False), email),
            )
            con.commit()
    finally:
        con.close()


def list_registered_push_pending(limit: int = 50) -> list[dict]:
    """列出推送失败或从未推送过的已注册账号。

    判据在 Python 侧精确判定，避免依赖不一定可用的 SQLite JSON1。
    """
    con = _conn()
    try:
        rows = con.execute(
            "SELECT * FROM registered ORDER BY created_at DESC LIMIT ?",
            (max(1, int(limit)) * 5,),
        ).fetchall()
    finally:
        con.close()
    pending: list[dict] = []
    for row in rows:
        item = dict(row)
        try:
            extra = json.loads(item.get("extra_json") or "{}")
        except (TypeError, ValueError):
            extra = {}
        push = extra.get("push") if isinstance(extra, dict) else None
        if isinstance(push, dict) and push.get("ok"):
            continue
        pending.append(item)
        if len(pending) >= limit:
            break
    return pending


def migrate_legacy_export_targets() -> int:
    """把旧的三个固定槽位一次性转成 push_targets 记录。

    旧键保留不删：回滚到旧版本时仍能读到原配置。迁移只跑一次。
    """
    if get_setting(_MIGRATION_KEY, "") == "1":
        return 0
    created = 0
    legacy = (
        ("cpa", "CPA(迁移)", "export_cpa_enabled", "export_cpa_url",
         "export_cpa_mgmt_key", "", "export_cpa_timeout"),
        ("sub2api", "SUB2API(迁移)", "export_sub2api_enabled", "export_sub2api_url",
         "export_sub2api_api_key", "export_sub2api_group_ids", "export_sub2api_timeout"),
        ("chatgpt2api", "ChatGPT2API(迁移)", "export_chatgpt2api_enabled",
         "export_chatgpt2api_url", "export_chatgpt2api_admin_key",
         "export_chatgpt2api_target_group_id", "export_chatgpt2api_timeout"),
    )
    for kind, name, enabled_key, url_key, cred_key, group_key, timeout_key in legacy:
        if get_setting(enabled_key, "0") not in ("1", "true"):
            continue
        url = get_setting(url_key, "")
        if not url:
            continue
        try:
            create_push_target({
                "name": name,
                "type": kind,
                "url": url,
                "credential": get_setting(cred_key, ""),
                "group_id": get_setting(group_key, "") if group_key else "",
                "weight": 1,
                "enabled": True,
                "timeout": get_setting(timeout_key, "15") or 15,
            })
            created += 1
        except (ValueError, TypeError):
            continue
    set_setting(_MIGRATION_KEY, "1")
    return created


# 模块加载时自动建表
init_db()
try:
    migrate_legacy_export_targets()
except Exception:  # 迁移失败不能阻断进程启动
    pass'''


# ---------------------------------------------------------------------------
# exporter.py
# ---------------------------------------------------------------------------

EXPORTER_APPENDIX = r'''

# --- c2a-push-targets: chat2api-Go 目标与统一分发 -------------------------


def export_to_chat2api_go(cred: dict, cfg: dict, *,
                          log_fn: Optional[Callable[[str, str], None]] = None) -> dict:
    """推送到 Go 版号池的 /admin/api/accounts/import。

    与 Python 版 ChatGPT2API 的差异（都来自源码）：
      * 鉴权用 `X-Admin-Token` 头，不是 Bearer；
      * body 是「账号对象数组」，不是 {accounts: [...]} 信封；
      * 只认 access_token / refresh_token / email / account_id。
    """
    log = log_fn or (lambda m, lvl="info": logger.info(m))
    url = str(cfg.get("url") or "").strip().rstrip("/")
    token = str(cfg.get("credential") or "").strip()
    timeout = int(cfg.get("timeout") or 15)
    email = str(cred.get("email") or "").strip()
    if not url:
        return {"ok": False, "error": "chat2api-Go 地址未配置"}
    if not token:
        return {"ok": False, "error": "chat2api-Go 管理令牌未配置"}

    access_token = str(cred.get("access_token") or "").strip()
    if not access_token:
        return {"ok": False, "error": "缺少 access_token"}
    item = {
        "access_token": access_token,
        "refresh_token": str(cred.get("refresh_token") or "").strip(),
        "email": email,
        "account_id": str(cred.get("account_id") or "").strip(),
    }
    endpoint = f"{url}/admin/api/accounts/import"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Admin-Token": token,
    }
    payload = json.dumps([item], ensure_ascii=False).encode("utf-8")

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            log(f"[chat2api-Go] 第 {attempt}/{MAX_ATTEMPTS} 次导入 {email} -> {url}", "info")
            resp = requests.post(
                endpoint,
                headers=headers,
                data=payload,
                timeout=timeout,
                impersonate="chrome110",
                verify=False,
                proxies=None,
            )
            body = (resp.text or "")[:400]
            if resp.status_code in (200, 201):
                log(f"[chat2api-Go] 导入成功: {body[:200]}", "ok")
                return {"ok": True, "response": resp.json() if resp.text else {}}
            log(f"[chat2api-Go] HTTP {resp.status_code}: {body}", "error")
            if attempt < MAX_ATTEMPTS and resp.status_code >= 500:
                time.sleep(RETRY_DELAYS_S[attempt - 1])
                continue
            return {"ok": False, "error": f"HTTP {resp.status_code}: {body[:200]}"}
        except Exception as exc:
            log(f"[chat2api-Go] 请求异常: {exc}", "error")
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_DELAYS_S[attempt - 1])
                continue
            return {"ok": False, "error": str(exc)}
    return {"ok": False, "error": "unreachable"}


def export_to_target(cred: dict, target: dict, *,
                     log_fn: Optional[Callable[[str, str], None]] = None) -> dict:
    """按目标类型分发。返回统一的 {ok, message, response?} 结构。"""
    kind = str(target.get("type") or "").strip()
    cfg = {
        "url": target.get("url"),
        "credential": target.get("credential"),
        "group_id": target.get("group_id"),
        "timeout": target.get("timeout") or DEFAULT_TIMEOUT,
    }
    if kind == "cpa":
        return export_to_cpa(cred, {
            "cpa_url": cfg["url"],
            "cpa_mgmt_key": cfg["credential"],
            "cpa_timeout": cfg["timeout"],
        }, log_fn=log_fn)
    if kind == "sub2api":
        return export_to_sub2api(cred, {
            "sub2api_url": cfg["url"],
            "sub2api_api_key": cfg["credential"],
            "sub2api_group_ids": cfg["group_id"] or ",".join(
                str(g) for g in DEFAULT_SUB2API_GROUP_IDS
            ),
            "sub2api_timeout": cfg["timeout"],
        }, log_fn=log_fn)
    if kind == "chatgpt2api":
        return export_to_chatgpt2api(cred, {
            "chatgpt2api_url": cfg["url"],
            "chatgpt2api_admin_key": cfg["credential"],
            "chatgpt2api_target_group_id": cfg["group_id"] or "",
            "chatgpt2api_sync_after_import": True,
            "chatgpt2api_timeout": cfg["timeout"],
        }, log_fn=log_fn)
    if kind == "chat2api_go":
        return export_to_chat2api_go(cred, cfg, log_fn=log_fn)
    return {"ok": False, "error": f"未知目标类型: {kind}"}


def run_exports_to_target(cred: dict, target: dict, *,
                          log_fn: Optional[Callable[[str, str], None]] = None) -> dict:
    """推送到单个目标；CPA / SUB2API 需要先把 refresh_token 换成 Codex token。"""
    log = log_fn or (lambda m, lvl="info": logger.info(m))
    kind = str(target.get("type") or "").strip()
    export_cred = dict(cred)
    if kind in ("cpa", "sub2api"):
        refresh_token = str(cred.get("refresh_token") or "").strip()
        if not refresh_token:
            return {"ok": False, "error": "缺少 refresh_token，无法换取 Codex token"}
        try:
            log("[exporter] 用 refresh_token 换新的 Codex access_token...", "info")
            fresh = refresh_codex_token(refresh_token)
            export_cred.update({
                "access_token": fresh["access_token"],
                "refresh_token": fresh.get("refresh_token") or refresh_token,
                "id_token": fresh.get("id_token") or cred.get("id_token", ""),
            })
            log("[exporter] Codex token 刷新成功", "ok")
        except Exception as exc:
            return {"ok": False, "error": f"Codex token 刷新失败: {exc}"}
    try:
        return export_to_target(export_cred, target, log_fn=log)
    except Exception as exc:
        log(f"[exporter] 目标 {target.get('name')} 导出异常: {exc}", "error")
        return {"ok": False, "error": str(exc)}


def test_target(target: dict) -> dict:
    """按目标类型做连通性测试（不带真实账号，只验证鉴权与地址）。"""
    kind = str(target.get("type") or "").strip()
    cfg = {
        "url": target.get("url"),
        "credential": target.get("credential"),
        "group_id": target.get("group_id"),
        "timeout": target.get("timeout") or DEFAULT_TIMEOUT,
    }
    if kind == "cpa":
        return test_cpa({
            "cpa_url": cfg["url"],
            "cpa_mgmt_key": cfg["credential"],
            "cpa_timeout": cfg["timeout"],
        })
    if kind == "sub2api":
        return test_sub2api({
            "sub2api_url": cfg["url"],
            "sub2api_api_key": cfg["credential"],
            "sub2api_group_ids": cfg["group_id"] or "",
            "sub2api_timeout": cfg["timeout"],
        })
    if kind == "chatgpt2api":
        return test_chatgpt2api({
            "chatgpt2api_url": cfg["url"],
            "chatgpt2api_admin_key": cfg["credential"],
            "chatgpt2api_timeout": cfg["timeout"],
        })
    if kind == "chat2api_go":
        url = str(cfg["url"] or "").strip().rstrip("/")
        token = str(cfg["credential"] or "").strip()
        if not url:
            return {"ok": False, "error": "地址未配置"}
        if not token:
            return {"ok": False, "error": "管理令牌未配置"}
        try:
            resp = requests.get(
                f"{url}/admin/api/accounts?limit=1",
                headers={"X-Admin-Token": token, "Accept": "application/json"},
                timeout=int(cfg["timeout"]),
                impersonate="chrome110",
                verify=False,
                proxies=None,
            )
            if resp.status_code == 401:
                return {"ok": False, "error": "管理令牌被拒绝（X-Admin-Token）"}
            if resp.status_code >= 500:
                return {"ok": False, "error": f"上游错误 HTTP {resp.status_code}"}
            return {"ok": True, "status": resp.status_code}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
    return {"ok": False, "error": f"未知目标类型: {kind}"}
'''


# ---------------------------------------------------------------------------
# app.py
# ---------------------------------------------------------------------------

APP_ROUTES_ANCHOR = "class ManualExportReq(BaseModel):"

APP_ROUTES_REPLACEMENT = r'''class PushTargetReq(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = Field(default=None, description="cpa / sub2api / chatgpt2api / chat2api_go")
    url: Optional[str] = None
    credential: Optional[str] = Field(default=None, description="传 '***' 表示不修改")
    group_id: Optional[str] = None
    weight: Optional[int] = Field(default=None, ge=1, le=100)
    enabled: Optional[bool] = None
    timeout: Optional[int] = Field(default=None, ge=1, le=600)


@app.get("/api/push-targets")
def api_list_push_targets():
    """列出所有推送目标。密文字段以 *** 占位，前端不会拿到明文。"""
    items = []
    for target in db.list_push_targets():
        item = dict(target)
        item["has_credential"] = bool(item.get("credential"))
        item["credential"] = "***" if item.get("credential") else ""
        items.append(item)
    return {
        "ok": True,
        "targets": items,
        "types": [
            {"value": kind, "label": db.PUSH_TARGET_TYPE_LABELS.get(kind, kind)}
            for kind in db.PUSH_TARGET_TYPES
        ],
    }


@app.post("/api/push-targets")
def api_create_push_target(req: PushTargetReq):
    try:
        target_id = db.create_push_target(req.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "id": target_id}


@app.put("/api/push-targets/{target_id}")
def api_update_push_target(target_id: int, req: PushTargetReq):
    try:
        changed = db.update_push_target(target_id, req.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not changed:
        raise HTTPException(404, f"未找到推送目标: {target_id}")
    return {"ok": True}


@app.delete("/api/push-targets/{target_id}")
def api_delete_push_target(target_id: int):
    if not db.delete_push_target(target_id):
        raise HTTPException(404, f"未找到推送目标: {target_id}")
    return {"ok": True}


@app.post("/api/push-targets/{target_id}/test")
def api_test_push_target(target_id: int):
    """连通性测试：不带真实账号，只验证鉴权与地址。"""
    from . import exporter
    target = db.get_push_target(target_id)
    if not target:
        raise HTTPException(404, f"未找到推送目标: {target_id}")
    try:
        result = exporter.test_target(target)
    except Exception as exc:
        raise HTTPException(500, f"测试失败: {exc}")
    return {"ok": bool(result.get("ok")), "result": result}


class RetryPushReq(BaseModel):
    target_id: Optional[int] = Field(default=None, description="只重推该目标；为空则按权重随机")
    limit: int = Field(default=50, ge=1, le=500)


@app.post("/api/push-targets/retry")
def api_retry_pending_push(req: RetryPushReq):
    """把推送失败（extra_json.push.ok=false）的账号再推一次。只挑失败的。"""
    from . import exporter
    pending = db.list_registered_push_pending(limit=req.limit)
    if not pending:
        return {"ok": True, "attempted": 0, "succeeded": 0, "results": []}
    target = db.get_push_target(req.target_id) if req.target_id else db.pick_push_target()
    if not target:
        raise HTTPException(400, "没有可用的推送目标")
    results = []
    succeeded = 0
    for cred in pending:
        result = exporter.run_exports_to_target(cred, target)
        ok = bool(result.get("ok"))
        message = str(result.get("message") or result.get("error") or "")
        db.record_push_result(int(target["id"]), ok, message)
        db.mark_registered_push(cred.get("email", ""), {
            "target_id": target.get("id"),
            "target_name": target.get("name"),
            "type": target.get("type"),
            "ok": ok,
            "message": message[:500],
            "at": __import__("time").time(),
        })
        succeeded += 1 if ok else 0
        results.append({"email": cred.get("email"), "ok": ok, "message": message})
    return {
        "ok": True,
        "attempted": len(pending),
        "succeeded": succeeded,
        "results": results,
        "target": {"id": target.get("id"), "name": target.get("name")},
    }


class ManualExportReq(BaseModel):'''

APP_MANUAL_ANCHOR = r'''class ManualExportReq(BaseModel):
    email: str = Field(..., description="要导出的已注册账号邮箱")
    targets: list[str] = Field(
        default_factory=lambda: ["cpa", "sub2api", "chatgpt2api"],
        description="选择导出目标：cpa / sub2api / chatgpt2api",
    )


@app.post("/api/registered/export_to_panel")
def api_manual_export_to_panel(req: ManualExportReq):
    """对一个已注册账号手动触发到面板的导出。

    targets 里选 cpa / sub2api 之一或全部。即使总开关未启用，本接口也会执行
    （只要 URL/密钥 等基础配置已填）。
    """
    from . import exporter
    cred = db.get_registered(req.email)
    if not cred:
        raise HTTPException(404, f"未找到已注册账号: {req.email}")

    cfg = db.get_export_internal_config()
    targets = {t.strip().lower() for t in (req.targets or []) if t}

    cpa_cfg = dict(cfg["cpa"]) if "cpa" in targets else None
    sub2api_cfg = dict(cfg["sub2api"]) if "sub2api" in targets else None
    chatgpt2api_cfg = dict(cfg["chatgpt2api"]) if "chatgpt2api" in targets else None
    for part in (cpa_cfg, sub2api_cfg, chatgpt2api_cfg):
        if part is not None:
            part["enabled"] = True

    results = exporter.run_exports(
        cred,
        cpa_cfg=cpa_cfg,
        sub2api_cfg=sub2api_cfg,
        chatgpt2api_cfg=chatgpt2api_cfg,
    )
    out = {
        "email": req.email,
        "cpa": results.get("cpa"),
        "sub2api": results.get("sub2api"),
        "chatgpt2api": results.get("chatgpt2api"),
    }
    return {"ok": True, **out}'''

APP_MANUAL_REPLACEMENT = r'''class ManualExportReq(BaseModel):
    email: str = Field(..., description="要导出的已注册账号邮箱")
    target_ids: list[int] = Field(
        default_factory=list,
        description="推送到这些 push_targets.id；为空则按权重随机选一个启用中的目标",
    )
    targets: list[str] = Field(
        default_factory=list,
        description="兼容旧调用：按类型选 cpa / sub2api / chatgpt2api / chat2api_go",
    )


@app.post("/api/registered/export_to_panel")
def api_manual_export_to_panel(req: ManualExportReq):
    """对一个已注册账号手动触发推送。

    优先级：target_ids > targets（按类型）> 按权重随机。
    """
    from . import exporter
    cred = db.get_registered(req.email)
    if not cred:
        raise HTTPException(404, f"未找到已注册账号: {req.email}")

    all_targets = db.list_push_targets()
    selected: list[dict] = []
    if req.target_ids:
        wanted = {int(i) for i in req.target_ids}
        selected = [t for t in all_targets if int(t["id"]) in wanted]
    elif req.targets:
        wanted_types = {t.strip().lower() for t in req.targets if t}
        selected = [t for t in all_targets if t["type"] in wanted_types]
    else:
        picked = db.pick_push_target()
        selected = [picked] if picked else []
    if not selected:
        raise HTTPException(404, "没有匹配的推送目标")

    results = []
    for target in selected:
        result = exporter.run_exports_to_target(cred, target)
        ok = bool(result.get("ok"))
        message = str(result.get("message") or result.get("error") or "")
        db.record_push_result(int(target["id"]), ok, message)
        db.mark_registered_push(req.email, {
            "target_id": target.get("id"),
            "target_name": target.get("name"),
            "type": target.get("type"),
            "ok": ok,
            "message": message[:500],
            "at": __import__("time").time(),
        })
        results.append({
            "target_id": target.get("id"),
            "target_name": target.get("name"),
            "type": target.get("type"),
            "ok": ok,
            "message": message,
        })
    return {"ok": True, "email": req.email, "results": results}'''


# ---------------------------------------------------------------------------
# registrar.py
# ---------------------------------------------------------------------------

REGISTRAR_ANCHOR = r'''def _try_export_to_panels(run_id: str, cred: dict) -> None:
    """注册完成后可选地把凭证导出到 CPA / SUB2API / ChatGPT2API 面板。

    - 任一目标的"启用"开关关闭时,该目标跳过(不发请求);全部未启用时整段 no-op。
    - 任何异常都不抛,只 emit 日志/状态(不影响注册主流程)。
    """
    try:
        cfg = db.get_export_internal_config()
    except Exception as e:
        logging.getLogger("registrar").warning(f"[export] 读取配置失败: {e}")
        return

    cpa_enabled = bool(cfg.get("cpa", {}).get("enabled"))
    sub2api_enabled = bool(cfg.get("sub2api", {}).get("enabled"))
    chatgpt2api_enabled = bool(cfg.get("chatgpt2api", {}).get("enabled"))
    if not (cpa_enabled or sub2api_enabled or chatgpt2api_enabled):
        return  # 用户没勾选任何目标 → 完全不执行

    from . import exporter  # 懒 import,避免未启用时强依赖

    explog = logging.getLogger("registrar")

    def _log(msg: str, level: str = "info") -> None:
        if level == "error":
            explog.error(f"[export] {msg}")
        elif level == "warn":
            explog.warning(f"[export] {msg}")
        else:
            explog.info(f"[export] {msg}")
        try:
            _emit_status(run_id, "phase", {"phase": "export", "message": msg, "level": level})
        except Exception:
            pass

    try:
        results = exporter.run_exports(
            cred,
            cpa_cfg=cfg.get("cpa") if cpa_enabled else None,
            sub2api_cfg=cfg.get("sub2api") if sub2api_enabled else None,
            chatgpt2api_cfg=cfg.get("chatgpt2api") if chatgpt2api_enabled else None,
            log_fn=_log,
        )
    except Exception as e:
        _log(f"导出整体异常: {e}", "error")
        return

    # 汇总成一个事件给前端
    summary = {}
    if results.get("cpa") is not None:
        summary["cpa"] = {"ok": bool(results["cpa"].get("ok")),
                          "message": results["cpa"].get("message") or results["cpa"].get("error") or ""}
    if results.get("sub2api") is not None:
        summary["sub2api"] = {"ok": bool(results["sub2api"].get("ok")),
                              "message": results["sub2api"].get("message") or results["sub2api"].get("error") or ""}
    if results.get("chatgpt2api") is not None:
        summary["chatgpt2api"] = {
            "ok": bool(results["chatgpt2api"].get("ok")),
            "message": results["chatgpt2api"].get("message") or results["chatgpt2api"].get("error") or "",
        }
    try:
        _emit_status(run_id, "phase", {"phase": "export_done", "summary": summary})
    except Exception:
        pass'''

REGISTRAR_REPLACEMENT = r'''def _try_export_to_panels(run_id: str, cred: dict) -> None:
    """注册完成后把凭证推送到「推送目标」里的一个渠道。

    按 weight 随机选一个 enabled 目标，默认全为 1 即 1:1。
    推送失败不丢号：结果写进 registered.extra_json.push，账号可由前端批量重推。
    任何异常都不抛，只 emit 日志/状态（不影响注册主流程）。
    """
    explog = logging.getLogger("registrar")

    def _log(msg: str, level: str = "info") -> None:
        if level == "error":
            explog.error(f"[export] {msg}")
        elif level == "warn":
            explog.warning(f"[export] {msg}")
        else:
            explog.info(f"[export] {msg}")
        try:
            _emit_status(run_id, "phase", {"phase": "export", "message": msg, "level": level})
        except Exception:
            pass

    try:
        target = db.pick_push_target()
    except Exception as e:
        explog.warning(f"[export] 读取推送目标失败: {e}")
        return
    if not target:
        return  # 没有启用的目标 → 完全不执行

    from . import exporter  # 懒 import,避免未推送时强依赖

    email = str(cred.get("email") or "").strip()
    _log(f"选中推送目标: {target.get('name')} ({target.get('type')}) 权重={target.get('weight')}")

    try:
        result = exporter.run_exports_to_target(cred, target, log_fn=_log)
    except Exception as e:
        result = {"ok": False, "error": str(e)}
        _log(f"推送整体异常: {e}", "error")

    ok = bool(result.get("ok"))
    message = str(result.get("message") or result.get("error") or "")
    push_record = {
        "target_id": target.get("id"),
        "target_name": target.get("name"),
        "type": target.get("type"),
        "ok": ok,
        "message": message[:500],
        "at": time.time(),
    }
    try:
        db.mark_registered_push(email, push_record)
    except Exception as e:
        explog.warning(f"[export] 写入推送结果失败: {e}")
    try:
        db.record_push_result(int(target.get("id") or 0), ok, message)
    except Exception:
        pass

    try:
        _emit_status(run_id, "phase", {
            "phase": "export_done",
            "summary": {str(target.get("type")): {"ok": ok, "message": message}},
        })
    except Exception:
        pass'''


# ---------------------------------------------------------------------------
# 引擎：CRLF 安全的锚点补丁
# ---------------------------------------------------------------------------


def _read_lf(path: Path) -> str:
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def _write_lf_as_crlf(path: Path, text: str) -> None:
    data = text.replace("\n", "\r\n").encode("utf-8")
    handle = tempfile.NamedTemporaryFile(
        "wb", dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp", delete=False
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write(data)
            handle.flush()
            import os as _os

            _os.fsync(handle.fileno())
        temp_path.replace(path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


class Session:
    def __init__(self, root: Path, *, dry_run: bool = False, backup_root: Path = BACKUP_ROOT) -> None:
        self.root = root
        self.dry_run = dry_run
        self.backup_root = Path(backup_root)
        self.applied: list[str] = []
        self.skipped: list[str] = []
        self.written: list[Path] = []
        self.stamp = time.strftime("%Y%m%d-%H%M%S")
        self.backup_dir = self.backup_root / self.stamp
        self._originals: dict[Path, str] = {}

    def _backup(self, path: Path) -> None:
        if self.dry_run:
            return
        self._originals.setdefault(path, path.read_bytes().decode("utf-8"))
        target = self.backup_dir / path.relative_to(self.root)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)

    def replace(self, relative: str, *, anchor: str, replacement: str, label: str,
                probe: str = "") -> None:
        path = self.root / relative
        text = _read_lf(path)
        # 幂等判据必须用「补丁独有」的探针，不能用 anchor：
        # app.py 的路由插入会把 anchor（class ManualExportReq）留在替换文本里，
        # 用 anchor 判断会导致复跑时重复插入一份路由。
        if probe and probe in text:
            self.skipped.append(f"{label} (already applied)")
            return
        count = text.count(anchor)
        if count != 1:
            raise SystemExit(f"ANCHOR FAIL {label}: matched {count}x in {relative}")
        self._backup(path)
        if not self.dry_run:
            _write_lf_as_crlf(path, text.replace(anchor, replacement, 1))
            self.written.append(path)
        self.applied.append(label)

    def append(self, relative: str, *, content: str, probe: str, label: str) -> None:
        path = self.root / relative
        text = _read_lf(path)
        if probe in text:
            self.skipped.append(f"{label} (already applied)")
            return
        self._backup(path)
        if not self.dry_run:
            _write_lf_as_crlf(path, text.rstrip("\n") + "\n" + content)
            self.written.append(path)
        self.applied.append(label)

    def verify(self) -> None:
        if self.dry_run:
            return
        broken = []
        with tempfile.TemporaryDirectory() as scratch:
            for i, path in enumerate(dict.fromkeys(self.written)):
                try:
                    py_compile.compile(str(path), doraise=True, cfile=f"{scratch}/{i}.pyc")
                except py_compile.PyCompileError as exc:
                    broken.append(f"{path}: {exc.msg}")
        if broken:
            self.rollback()
            raise SystemExit("SYNTAX FAIL, rolled back:\n" + "\n".join(broken))

    def rollback(self) -> None:
        for path, original in self._originals.items():
            path.write_bytes(original.encode("utf-8"))
        print(f"[rollback] restored {len(self._originals)} file(s)")

    def report(self) -> None:
        print(f"applied: {len(self.applied)}")
        for item in self.applied:
            print(f"  + {item}")
        print(f"skipped: {len(self.skipped)}")
        for item in self.skipped:
            print(f"  = {item}")
        if self.applied and not self.dry_run:
            print(f"[backup] {self.backup_dir}")


def restore(root: Path, stamp: str, backup_root: Path = BACKUP_ROOT) -> None:
    src = Path(backup_root) / stamp
    if not src.is_dir():
        raise SystemExit(f"backup not found: {src}")
    count = 0
    for item in src.rglob("*"):
        if item.is_file():
            target = root / item.relative_to(src)
            shutil.copy2(item, target)
            count += 1
    print(f"[restore] {count} file(s) from {src}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--restore", default="")
    parser.add_argument("--backup-root", default=str(BACKUP_ROOT))
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser()
    if not root.is_dir():
        raise SystemExit(f"root not found: {root}")
    backup_root = Path(args.backup_root).expanduser()
    if args.restore:
        restore(root, args.restore, backup_root)
        return 0

    dry = args.verify
    s = Session(root, dry_run=dry, backup_root=backup_root)
    try:
        s.replace("webui/db.py", anchor=DB_IMPORT_ANCHOR,
                  replacement=DB_IMPORT_REPLACEMENT, label="db: import random",
                  probe="import json\nimport random\nimport sqlite3")
        s.replace("webui/db.py", anchor=DB_TABLE_ANCHOR,
                  replacement=DB_TABLE_REPLACEMENT, label="db: push_targets table",
                  probe="CREATE TABLE IF NOT EXISTS push_targets")
        s.replace("webui/db.py", anchor=DB_TAIL_ANCHOR,
                  replacement=DB_TAIL_REPLACEMENT, label="db: CRUD + migration",
                  probe="def pick_push_target")
        s.append("webui/exporter.py", content=EXPORTER_APPENDIX,
                 probe="def export_to_chat2api_go", label="exporter: go target + dispatch")
        # 顺序不可颠倒：先把新路由插到 `class ManualExportReq` 之前（此时文件里
        # 只有一个该 class），再整体替换旧 class + handler。反过来的话第二次插入
        # 会再造一个 ManualExportReq 定义。
        s.replace("webui/app.py", anchor=APP_ROUTES_ANCHOR,
                  replacement=APP_ROUTES_REPLACEMENT, label="app: push-targets routes",
                  probe="api/push-targets")
        s.replace("webui/app.py", anchor=APP_MANUAL_ANCHOR,
                  replacement=APP_MANUAL_REPLACEMENT, label="app: manual push by target_id",
                  probe="target_ids: list[int]")
        s.replace("webui/registrar.py", anchor=REGISTRAR_ANCHOR,
                  replacement=REGISTRAR_REPLACEMENT, label="registrar: weighted pick",
                  probe="按 weight 随机选一个 enabled 目标")
        s.verify()
    except SystemExit:
        if not dry:
            s.rollback()
        raise
    s.report()
    if dry:
        print("[verify] anchors ok, nothing written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv[1:]))
