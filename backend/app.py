import hashlib, secrets, sqlite3
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

PRODUCT = "日切放行"
SKELETON = "业务日分区切片登记后裁决完整度，完整才放行下游，并与消费水位对账"

# ---------------------------------------------------------------------------
# 期望变更口径（当日钉死，全局只贯彻这一种写法，两侧不得混用）
# 甲规则：变更期望即作废旧完整/放行通过态、强制重裁，旧完整不得再放行、不得推水位。
# ---------------------------------------------------------------------------
CALIBER_RULE = "甲"
CALIBER_STATEMENT = (
    "甲规则（当日钉死，全局唯一写法）：期望行数变更时，在单一事务内整行写入——递增期望版本，"
    "同时作废该切片的完整/放行通过态（完整度改记“待重裁”、放行态回“未放行”、待重裁标记置位），"
    "旧完整标记随期望版本失效，不得再凭旧完整放行或推进消费水位；放行闸只认按当前期望版本重裁通过的切片。"
    "变更须携带读取到的期望版本，版本不一致记“期望争用”且本次不生效；"
    "当日口径一经钉死不得更换，缺少、混用或偏离本说明的提交一律拒绝并记“期望口径偏了”。"
    "禁止期望已升高仍凭旧完整推水位，禁止变更期望时半更新切片字段。"
)


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def today_text() -> str:
    return date.today().isoformat()


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def write_connect():
    """写事务专用连接：显式 BEGIN IMMEDIATE，拿写锁后再读当前版本。"""
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()


def compute_completeness(row_count: int, expected_count: int) -> str:
    if expected_count > 0 and row_count >= expected_count:
        return "完整"
    return "部分完整" if row_count > 0 else "待检"


def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))


class LoginIn(BaseModel):
    username: str
    password: str


def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u


def ensure_policy(conn, operator: str = "system"):
    """当日口径：一天一条，首次读取即钉死；已有记录不可被更换。"""
    day = today_text()
    row = conn.execute("SELECT * FROM policy_caliber WHERE day=?", (day,)).fetchone()
    if row is None:
        try:
            conn.execute(
                "INSERT INTO policy_caliber(day,rule,statement,created_by,created_at) VALUES (?,?,?,?,?)",
                (day, CALIBER_RULE, CALIBER_STATEMENT, operator, now_text()),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            pass  # 并发首读：另一请求已钉死同一天，沿用即可
        row = conn.execute("SELECT * FROM policy_caliber WHERE day=?", (day,)).fetchone()
    # 已有记录原样沿用：说明文本与规则当天一律不重写
    return row


def policy_view(row) -> dict:
    return {
        "day": row["day"],
        "rule": row["rule"],
        "statement": row["statement"],
        "locked": True,
        "created_by": row["created_by"],
        "created_at": row["created_at"],
    }


def slice_view(row) -> dict:
    """列表 / 重裁目录 / 放行闸 / 旁注共用同一份视图，保证几处口径对齐。"""
    d = dict(row)
    expected_rev = int(d.get("expected_rev") or 1)
    evaluation_rev = int(d.get("evaluation_rev") or 0)
    d["expected_rev"] = expected_rev
    d["evaluation_rev"] = evaluation_rev
    stale = bool(d.get("recut_required")) or evaluation_rev != expected_rev
    d["recut_required"] = stale
    if stale:
        reason = (f"期望v{expected_rev}已变更 · 旧完整作废"
                  if evaluation_rev != expected_rev else "实收变更 · 旧结论作废")
        gate, note = "待重裁", f"{reason}，待重裁"
    elif d["completeness"] == "待重裁":
        gate, note = "待重裁", "待重裁"
    elif d["completeness"] != "完整":
        gap = max(int(d["expected_count"]) - int(d["row_count"]), 0)
        gate, note = "未完整", f"缺 {gap} 行 · 未完整"
    elif d.get("release_status") == "已放行":
        gate, note = "已放行", "已放行"
    else:
        gate = "可放行"
        note = f"期望v{expected_rev} · 已按当前期望重裁通过"
    d["gate"] = gate
    d["note"] = note
    return d


def stale_watermarks(conn, slice_no: str | None = None):
    """各消费方“当前水位”若仍低于切片当前期望版本，则单列出来只做对照。"""
    sql = """SELECT w.* FROM watermarks w
             JOIN slices s ON s.slice_no = w.source_slice
             WHERE w.id = (SELECT MAX(w2.id) FROM watermarks w2
                           WHERE w2.consumer = w.consumer AND w2.source_slice = w.source_slice)
               AND w.expected_rev < s.expected_rev"""
    params: tuple = ()
    if slice_no is not None:
        sql += " AND w.source_slice = ?"
        params = (slice_no,)
    sql += " ORDER BY w.id DESC"
    return conn.execute(sql, params).fetchall()


def insert_evaluation(conn, slice_id, slice_no, expected_rev, expected_count,
                      row_count, completeness, source, operator):
    conn.execute(
        """INSERT INTO evaluations(slice_id,slice_no,expected_rev,expected_count,row_count,
           completeness,source,operator,created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
        (slice_id, slice_no, expected_rev, expected_count, row_count,
         completeness, source, operator, now_text()),
    )


def insert_expectation_change(conn, slice_id, slice_no, old_expected, new_expected,
                              old_rev, new_rev, rule, status, note, operator,
                              statement):
    conn.execute(
        """INSERT INTO expectation_changes(slice_id,slice_no,day,old_expected,new_expected,
           old_rev,new_rev,rule,statement,status,note,operator,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (slice_id, slice_no, today_text(), old_expected, new_expected,
         old_rev, new_rev, rule, statement, status, note, operator, now_text()),
    )


def log_expectation_change(slice_id, slice_no, old_expected, new_expected,
                           old_rev, new_rev, rule, status, note, operator,
                           statement=CALIBER_STATEMENT):
    with connect() as c:
        insert_expectation_change(c, slice_id, slice_no, old_expected, new_expected,
                                  old_rev, new_rev, rule, status, note, operator,
                                  statement)


def _migrate(conn):
    """单机旧库原地升级：只加列，不删表、不动登录态与既有数据。"""
    slice_cols = {r["name"] for r in conn.execute("PRAGMA table_info(slices)")}
    slice_adds = [
        ("expected_rev", "INTEGER NOT NULL DEFAULT 1"),
        ("evaluation_rev", "INTEGER NOT NULL DEFAULT 1"),
        ("recut_required", "INTEGER NOT NULL DEFAULT 0"),
        ("expected_updated_by", "TEXT NOT NULL DEFAULT ''"),
        ("expected_updated_at", "TEXT NOT NULL DEFAULT ''"),
    ]
    for name, ddl in slice_adds:
        if name not in slice_cols:
            conn.execute(f"ALTER TABLE slices ADD COLUMN {name} {ddl}")
    rel_cols = {r["name"] for r in conn.execute("PRAGMA table_info(releases)")}
    if "expected_rev" not in rel_cols:
        conn.execute("ALTER TABLE releases ADD COLUMN expected_rev INTEGER NOT NULL DEFAULT 0")
        # 历史流水回填到所属切片当前期望版本，避免旧记录被当成“v0”另类口径
        conn.execute("""UPDATE releases SET expected_rev=(
                          SELECT s.expected_rev FROM slices s WHERE s.id=releases.slice_id)
                        WHERE expected_rev=0""")
    wm_cols = {r["name"] for r in conn.execute("PRAGMA table_info(watermarks)")}
    if "expected_rev" not in wm_cols:
        conn.execute("ALTER TABLE watermarks ADD COLUMN expected_rev INTEGER NOT NULL DEFAULT 0")
        conn.execute("""UPDATE watermarks SET expected_rev=(
                          SELECT s.expected_rev FROM slices s WHERE s.slice_no=watermarks.source_slice)
                        WHERE expected_rev=0""")
    # 旧切片补一条重裁基线，保证期望详情里轨迹不断档
    conn.execute("""INSERT INTO evaluations(slice_id,slice_no,expected_rev,expected_count,
                      row_count,completeness,source,operator,created_at)
                    SELECT s.id,s.slice_no,s.expected_rev,s.expected_count,s.row_count,
                           s.completeness,'登记','system',?
                    FROM slices s
                    WHERE NOT EXISTS(SELECT 1 FROM evaluations e WHERE e.slice_id=s.id)""",
                 (now_text(),))


app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS slices(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          slice_no TEXT UNIQUE NOT NULL,
          biz_date TEXT NOT NULL,
          partition_key TEXT NOT NULL,
          row_count INTEGER NOT NULL,
          expected_count INTEGER NOT NULL,
          completeness TEXT NOT NULL,
          release_status TEXT NOT NULL,
          expected_rev INTEGER NOT NULL DEFAULT 1,
          evaluation_rev INTEGER NOT NULL DEFAULT 1,
          recut_required INTEGER NOT NULL DEFAULT 0,
          expected_updated_by TEXT NOT NULL DEFAULT '',
          expected_updated_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS releases(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          slice_id INTEGER NOT NULL,
          consumer TEXT NOT NULL,
          status TEXT NOT NULL,
          note TEXT NOT NULL,
          created_at TEXT NOT NULL,
          expected_rev INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS watermarks(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          consumer TEXT NOT NULL,
          biz_date TEXT NOT NULL,
          watermark TEXT NOT NULL,
          source_slice TEXT NOT NULL,
          expected_rev INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS expectation_changes(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          slice_id INTEGER NOT NULL,
          slice_no TEXT NOT NULL,
          day TEXT NOT NULL,
          old_expected INTEGER NOT NULL,
          new_expected INTEGER NOT NULL,
          old_rev INTEGER NOT NULL,
          new_rev INTEGER NOT NULL,
          rule TEXT NOT NULL,
          statement TEXT NOT NULL,
          status TEXT NOT NULL,
          note TEXT NOT NULL,
          operator TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS evaluations(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          slice_id INTEGER NOT NULL,
          slice_no TEXT NOT NULL,
          expected_rev INTEGER NOT NULL,
          expected_count INTEGER NOT NULL,
          row_count INTEGER NOT NULL,
          completeness TEXT NOT NULL,
          source TEXT NOT NULL,
          operator TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS policy_caliber(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          day TEXT UNIQUE NOT NULL,
          rule TEXT NOT NULL,
          statement TEXT NOT NULL,
          created_by TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS policy_events(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          day TEXT NOT NULL,
          attempted_rule TEXT NOT NULL,
          fixed_rule TEXT NOT NULL,
          status TEXT NOT NULL,
          note TEXT NOT NULL,
          operator TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """)
        _migrate(conn)
        ensure_policy(conn)
        if conn.execute("SELECT COUNT(*) c FROM slices").fetchone()["c"] == 0:
            rows = [
                ("SL-2026-11-01-east", "2026-11-01", "east", 980, 1000, "部分完整", "未放行"),
                ("SL-2026-11-01-west", "2026-11-01", "west", 1000, 1000, "完整", "已放行"),
                ("SL-2026-11-02-east", "2026-11-02", "east", 0, 1000, "待检", "未放行"),
            ]
            for r in rows:
                conn.execute("INSERT INTO slices(slice_no,biz_date,partition_key,row_count,expected_count,completeness,release_status) VALUES (?,?,?,?,?,?,?)", r)
            conn.execute("INSERT INTO releases(slice_id,consumer,status,note,created_at,expected_rev) VALUES (2,'ads_order','成功','完整切片放行','2026-11-01T18:00:00',1)")
            conn.execute("INSERT INTO watermarks(consumer,biz_date,watermark,source_slice,expected_rev) VALUES ('ads_order','2026-11-01','2026-11-01T18:00:00','SL-2026-11-01-west',1)")
            for sid, slice_no, exp, row_n, comp in [
                (1, "SL-2026-11-01-east", 1000, 980, "部分完整"),
                (2, "SL-2026-11-01-west", 1000, 1000, "完整"),
                (3, "SL-2026-11-02-east", 1000, 0, "待检"),
            ]:
                conn.execute(
                    """INSERT INTO evaluations(slice_id,slice_no,expected_rev,expected_count,row_count,
                       completeness,source,operator,created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (sid, slice_no, 1, exp, row_n, comp, "登记", "system", "2026-11-01T00:00:00"),
                )


@app.on_event("startup")
def _start():
    init_db()


@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM slices").fetchone()["c"]
        p = ensure_policy(c)
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "slices": n,
            "db": str(DB_PATH), "caliber": p["rule"], "caliber_day": p["day"]}


@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username, role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row:
        raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16); SESSIONS[tok] = row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}


# ---------------------------------------------------------------------------
# 当日期望口径（策略条）
# ---------------------------------------------------------------------------
class PolicyIn(BaseModel):
    rule: str
    note: str = ""


@app.get("/api/policy")
def get_policy(authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    with connect() as c:
        row = ensure_policy(c, user)
        events = c.execute(
            "SELECT * FROM policy_events ORDER BY id DESC LIMIT 20"
        ).fetchall()
    return {"policy": policy_view(row), "events": [dict(e) for e in events]}


@app.post("/api/policy")
def set_policy(body: PolicyIn, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    with connect() as c:
        row = ensure_policy(c, user)
        if body.rule != row["rule"]:
            c.execute(
                """INSERT INTO policy_events(day,attempted_rule,fixed_rule,status,note,operator,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (row["day"], body.rule, row["rule"], "期望口径偏了",
                 body.note or "当日口径已钉死，拒绝更换", user, now_text()),
            )
            c.commit()
            raise HTTPException(409, f"期望口径偏了：当日期望变更说明已钉死为{CALIBER_RULE}规则")
    return {"ok": True, "policy": policy_view(row)}


# ---------------------------------------------------------------------------
# 切片登记 / 列表
# ---------------------------------------------------------------------------
@app.get("/api/slices")
def list_slices(authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM slices ORDER BY id").fetchall()
        policy = ensure_policy(c, user)
    return {"items": [slice_view(r) for r in rows], "policy": policy_view(policy)}


class SliceIn(BaseModel):
    slice_no: str
    biz_date: str
    partition_key: str
    row_count: int
    expected_count: int


@app.post("/api/slices")
def add_slice(body: SliceIn, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    comp = compute_completeness(body.row_count, body.expected_count)
    conn = write_connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                """INSERT INTO slices(slice_no,biz_date,partition_key,row_count,expected_count,
                   completeness,release_status,expected_rev,evaluation_rev,recut_required)
                   VALUES (?,?,?,?,?,?,?,1,1,0)""",
                (body.slice_no, body.biz_date, body.partition_key, body.row_count,
                 body.expected_count, comp, "未放行"),
            )
            sid = cur.lastrowid
            insert_evaluation(conn, sid, body.slice_no, 1, body.expected_count,
                              body.row_count, comp, "登记", user)
            conn.execute("COMMIT")
            return {"id": sid, "completeness": comp, "expected_rev": 1}
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            raise HTTPException(409, "切片号已存在")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 期望变更闭环
# ---------------------------------------------------------------------------
class ExpectationIn(BaseModel):
    expected_count: int
    expected_rev: int = 0
    rule: str | None = None
    statement: str | None = None
    reason: str = ""


@app.post("/api/slices/{sid}/expectation")
def change_expectation(sid: int, body: ExpectationIn,
                       authorization: str | None = Header(default=None)):
    operator = require_user(authorization)
    with connect() as c:
        policy = ensure_policy(c, operator)

    rule = body.rule or policy["rule"]
    if rule != policy["rule"]:
        with connect() as c:
            row = c.execute("SELECT * FROM slices WHERE id=?", (sid,)).fetchone()
        if not row:
            raise HTTPException(404, "切片不存在")
        log_expectation_change(
            sid, row["slice_no"], row["expected_count"], body.expected_count,
            row["expected_rev"], row["expected_rev"], rule, "期望口径偏了",
            f"当日口径为{policy['rule']}，提交口径为{rule}，本次不生效", operator,
            policy["statement"],
        )
        raise HTTPException(409, f"期望口径偏了：当日期望变更说明钉死为{policy['rule']}规则")

    if body.statement is not None and body.statement != policy["statement"]:
        with connect() as c:
            row = c.execute("SELECT * FROM slices WHERE id=?", (sid,)).fetchone()
        if not row:
            raise HTTPException(404, "切片不存在")
        log_expectation_change(
            sid, row["slice_no"], row["expected_count"], body.expected_count,
            row["expected_rev"], row["expected_rev"], rule, "期望口径偏了",
            "提交的期望变更说明与当日钉死说明不一致，本次不生效", operator,
            body.statement,
        )
        raise HTTPException(409, "期望口径偏了：提交的期望变更说明与当日钉死说明不一致")

    # 变更必须携带提交前读到的期望版本；缺失/串切片一律不生效
    if body.expected_rev < 1:
        with connect() as c:
            row = c.execute("SELECT * FROM slices WHERE id=?", (sid,)).fetchone()
        if not row:
            raise HTTPException(404, "切片不存在")
        log_expectation_change(
            sid, row["slice_no"], row["expected_count"], body.expected_count,
            body.expected_rev, row["expected_rev"], rule, "期望争用",
            "提交未携带读取到的期望版本，本次未生效", operator, policy["statement"],
        )
        raise HTTPException(409, "期望争用：提交未携带读取到的期望版本，请刷新后重试")

    if body.expected_count < 0:
        raise HTTPException(400, "期望行数不能为负")

    conn = write_connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM slices WHERE id=?", (sid,)).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            raise HTTPException(404, "切片不存在")
        if int(row["expected_rev"]) != body.expected_rev:
            # 后手：不可静默成功，落一条“期望争用”并在响应里明确拒绝
            conn.execute("ROLLBACK")
            log_expectation_change(
                sid, row["slice_no"], row["expected_count"], body.expected_count,
                body.expected_rev, row["expected_rev"], rule, "期望争用",
                f"提交期望版本v{body.expected_rev}与当前v{row['expected_rev']}不一致，本次未生效",
                operator, policy["statement"],
            )
            raise HTTPException(409, "期望争用：该切片期望已被他人变更，请刷新后重试")
        if body.expected_count == int(row["expected_count"]):
            conn.execute("ROLLBACK")
            raise HTTPException(400, "期望未变化")
        new_rev = int(row["expected_rev"]) + 1
        # 单一事务整行写入：期望值、期望版本、重裁基线、待重裁标记一次落定
        # 放行通过态一并作废；旧完整标记随期望版本失效
        conn.execute(
            """UPDATE slices SET expected_count=?, expected_rev=?, evaluation_rev=?,
               recut_required=1, completeness='待重裁', release_status='未放行',
               expected_updated_by=?, expected_updated_at=?
               WHERE id=? AND expected_rev=?""",
            (body.expected_count, new_rev, int(row["expected_rev"]), operator,
             now_text(), sid, int(row["expected_rev"])),
        )
        insert_expectation_change(
            conn, sid, row["slice_no"], int(row["expected_count"]), body.expected_count,
            int(row["expected_rev"]), new_rev, rule, "已生效",
            body.reason or "期望行数变更，旧完整作废并强制重裁", operator,
            policy["statement"],
        )
        # 重裁写入：先记一条作废基线，随后必须显式重裁才恢复结论
        insert_evaluation(conn, sid, row["slice_no"], new_rev, body.expected_count,
                          int(row["row_count"]), "待重裁", "期望变更", operator)
        conn.execute("COMMIT")
        return {
            "ok": True,
            "slice_id": sid,
            "expected_count": body.expected_count,
            "expected_rev": new_rev,
            "evaluation_rev": int(row["expected_rev"]),
            "recut_required": True,
            "completeness": "待重裁",
            "rule": rule,
            "statement": policy["statement"],
        }
    except HTTPException:
        raise
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


@app.get("/api/expectations")
def list_expectations(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM expectation_changes ORDER BY id DESC").fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/api/slices/{sid}/expectation")
def slice_expectation(sid: int, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        row = c.execute("SELECT * FROM slices WHERE id=?", (sid,)).fetchone()
        if not row:
            raise HTTPException(404, "切片不存在")
        changes = c.execute(
            "SELECT * FROM expectation_changes WHERE slice_id=? ORDER BY id DESC", (sid,)
        ).fetchall()
        evals = c.execute(
            "SELECT * FROM evaluations WHERE slice_id=? ORDER BY id DESC LIMIT 20", (sid,)
        ).fetchall()
        stale_wm = stale_watermarks(c, row["slice_no"])
        policy = ensure_policy(c)
    return {
        "slice": slice_view(row),
        "changes": [dict(r) for r in changes],
        "evaluations": [dict(r) for r in evals],
        "watermarks_below_expectation": [dict(w) for w in stale_wm],
        "policy": policy_view(policy),
    }


@app.get("/api/recut")
def recut_catalog(authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM slices ORDER BY id").fetchall()
        evals = c.execute(
            "SELECT * FROM evaluations ORDER BY id DESC LIMIT 30"
        ).fetchall()
        policy = ensure_policy(c)
    views = [slice_view(r) for r in rows]
    pending = [v for v in views if v["recut_required"]]
    recent = [dict(e) for e in evals
              if e["source"] in ("期望变更", "实收更新", "重裁", "放行前复核")]
    with connect() as c:
        stale_wm = stale_watermarks(c)
        changes = c.execute(
            """SELECT * FROM expectation_changes WHERE status <> '已生效'
               ORDER BY id DESC LIMIT 20"""
        ).fetchall()
    return {"items": pending, "count": len(pending), "slices": views,
            "recent": recent, "watermarks_below_expectation": [dict(w) for w in stale_wm],
            "rejected_changes": [dict(x) for x in changes],
            "policy": policy_view(policy)}


# ---------------------------------------------------------------------------
# 实收更新（迟到重灌）：实收一变就作废旧结论，必须重裁
# ---------------------------------------------------------------------------
class ArrivalIn(BaseModel):
    row_count: int


@app.post("/api/slices/{sid}/arrival")
def report_arrival(sid: int, body: ArrivalIn,
                   authorization: str | None = Header(default=None)):
    operator = require_user(authorization)
    if body.row_count < 0:
        raise HTTPException(400, "实收行数不能为负")
    conn = write_connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM slices WHERE id=?", (sid,)).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            raise HTTPException(404, "切片不存在")
        if int(row["row_count"]) == body.row_count:
            conn.execute("ROLLBACK")
            raise HTTPException(400, "实收未变化")
        conn.execute(
            """UPDATE slices SET row_count=?, completeness='待重裁', recut_required=1,
               release_status='未放行' WHERE id=?""",
            (body.row_count, sid),
        )
        insert_evaluation(conn, sid, row["slice_no"], int(row["expected_rev"]),
                          int(row["expected_count"]), body.row_count, "待重裁",
                          "实收更新", operator)
        conn.execute("COMMIT")
    except HTTPException:
        raise
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
    return {"ok": True, "row_count": body.row_count, "recut_required": True,
            "completeness": "待重裁"}


# ---------------------------------------------------------------------------
# 重裁（按当前期望版本写入结论）
# ---------------------------------------------------------------------------
@app.post("/api/slices/{sid}/evaluate")
def evaluate(sid: int, authorization: str | None = Header(default=None)):
    operator = require_user(authorization)
    conn = write_connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM slices WHERE id=?", (sid,)).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            raise HTTPException(404, "切片不存在")
        rev = int(row["expected_rev"])
        comp = compute_completeness(int(row["row_count"]), int(row["expected_count"]))
        cur = conn.execute(
            """UPDATE slices SET completeness=?, recut_required=0, evaluation_rev=?
               WHERE id=? AND expected_rev=?""",
            (comp, rev, sid, rev),
        )
        if cur.rowcount != 1:
            conn.execute("ROLLBACK")
            raise HTTPException(409, "期望已变更，请刷新后按新期望重裁")
        insert_evaluation(conn, sid, row["slice_no"], rev, int(row["expected_count"]),
                          int(row["row_count"]), comp, "重裁", operator)
        conn.execute("COMMIT")
    except HTTPException:
        raise
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
    return {"id": sid, "completeness": comp, "expected_rev": rev,
            "recut_required": False}


# ---------------------------------------------------------------------------
# 放行闸
# ---------------------------------------------------------------------------
class ReleaseIn(BaseModel):
    consumer: str


@app.post("/api/slices/{sid}/release")
def release(sid: int, body: ReleaseIn, authorization: str | None = Header(default=None)):
    operator = require_user(authorization)
    conn = write_connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM slices WHERE id=?", (sid,)).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            raise HTTPException(404, "切片不存在")
        rev = int(row["expected_rev"])
        stale = bool(row["recut_required"]) or int(row["evaluation_rev"]) != rev
        if stale:
            conn.execute(
                "INSERT INTO releases(slice_id,consumer,status,note,created_at,expected_rev) VALUES (?,?,?,?,?,?)",
                (sid, body.consumer, "拒绝", "期望变更后未重裁，禁止凭旧完整放行", now_text(), rev),
            )
            conn.execute("COMMIT")
            raise HTTPException(409, "期望变更后未重裁，禁止凭旧完整放行")
        fresh = compute_completeness(int(row["row_count"]), int(row["expected_count"]))
        if row["completeness"] != "完整" or fresh != "完整":
            conn.execute(
                "INSERT INTO releases(slice_id,consumer,status,note,created_at,expected_rev) VALUES (?,?,?,?,?,?)",
                (sid, body.consumer, "拒绝", "完整度未达标", now_text(), rev),
            )
            conn.execute("COMMIT")
            raise HTTPException(409, "未完整禁止放行")
        conn.execute("UPDATE slices SET release_status='已放行' WHERE id=?", (sid,))
        conn.execute(
            "INSERT INTO releases(slice_id,consumer,status,note,created_at,expected_rev) VALUES (?,?,?,?,?,?)",
            (sid, body.consumer, "成功", f"按期望v{rev}重裁完整放行", now_text(), rev),
        )
        insert_evaluation(conn, sid, row["slice_no"], rev, int(row["expected_count"]),
                          int(row["row_count"]), fresh, "放行前复核", operator)
        conn.execute(
            "INSERT INTO watermarks(consumer,biz_date,watermark,source_slice,expected_rev) VALUES (?,?,?,?,?)",
            (body.consumer, row["biz_date"], now_text(), row["slice_no"], rev),
        )
        conn.execute("COMMIT")
    except HTTPException:
        raise
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
    return {"ok": True, "expected_rev": rev}


@app.get("/api/releases")
def list_releases(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("""SELECT r.*, s.slice_no FROM releases r JOIN slices s ON s.id=r.slice_id ORDER BY r.id DESC""").fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/api/watermarks")
def list_wm(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM watermarks ORDER BY id DESC").fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
