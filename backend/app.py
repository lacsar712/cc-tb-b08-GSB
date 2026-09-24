import os
from datetime import datetime
from functools import wraps
from zoneinfo import ZoneInfo

import psycopg2
from flask import Flask, redirect, render_template, request, session, url_for
from psycopg2.extras import RealDictCursor

from rules import weigh

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "tea-cupping-dev-secret")

ACCOUNTS = {
    "taster": {"password": "tea123456", "role": "writer"},
    "observer": {"password": "look123456", "role": "reader"},
}

# 自然日按东八区折算，与审评现场同一日历
DAY_ZONE = ZoneInfo("Asia/Shanghai")
# 现场日账允许的结论筛选：只看通过 / 只看不通过 / 全部
VERDICT_FILTERS = {
    "all": None,
    "pass": "通过",
    "fail": "不通过",
}


def db():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def login_required(fn):
    @wraps(fn)
    def wrap(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return fn(*args, **kwargs)

    return wrap


def writer_required(fn):
    @wraps(fn)
    def wrap(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        if session.get("role") != "writer":
            return ("仅审评员可签发快照", 403)
        return fn(*args, **kwargs)

    return wrap


@app.get("/health")
def health():
    return {"status": "ok", "service": "tea-blend-cupping"}


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        name = request.form.get("username", "").strip()
        account = ACCOUNTS.get(name)
        if not account or account["password"] != request.form.get("password", ""):
            error = "用户名或密码错误"
        else:
            session["user"] = name
            session["role"] = account["role"]
            return redirect(url_for("home"))
    return render_template("login.html", error=error)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@login_required
def home():
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM cuppings ORDER BY id DESC")
        rows = cur.fetchall()
    return render_template("home.html", rows=rows, can_write=session.get("role") == "writer")


@app.post("/cuppings")
@login_required
def create():
    if session.get("role") != "writer":
        return ("仅审评员可提交拼配审评", 403)
    aroma = float(request.form["aroma"])
    taste = float(request.form["taste"])
    liquor = float(request.form["liquor"])
    lot = request.form["lot"].strip()
    verdict, note, score = weigh(aroma, taste, liquor)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """INSERT INTO cuppings (lot, aroma, taste, liquor, score, verdict, note, created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (lot, aroma, taste, liquor, score, verdict, note, session["user"]),
        )
        row = cur.fetchone()
        conn.commit()
    if request.headers.get("HX-Request"):
        return render_template("_row.html", row=row)
    return redirect(url_for("home"))


def daily_totals(cur, verdict):
    """按东八区自然日汇总通过/不通过条数与命中编号集合。

    verdict 为 None 时两种结论都返回；否则只由服务端聚合计入该结论，
    另一种结论不进入页面数据，绝不在浏览器里藏行。
    """
    sql = """
        SELECT (created_at AT TIME ZONE 'Asia/Shanghai')::date AS day,
               count(*) FILTER (WHERE verdict = '通过')::int AS pass_count,
               count(*) FILTER (WHERE verdict = '不通过')::int AS fail_count,
               coalesce(array_agg(id ORDER BY id) FILTER (WHERE verdict = '通过'), '{}') AS pass_ids,
               coalesce(array_agg(id ORDER BY id) FILTER (WHERE verdict = '不通过'), '{}') AS fail_ids
        FROM cuppings
    """
    params = ()
    if verdict is not None:
        sql += " WHERE verdict = %s"
        params = (verdict,)
    sql += " GROUP BY 1 ORDER BY 1 DESC"
    cur.execute(sql, params)
    return cur.fetchall()


@app.get("/ledger")
@login_required
def ledger():
    flt = request.args.get("verdict", "all")
    if flt not in VERDICT_FILTERS:
        flt = "all"
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        days = daily_totals(cur, VERDICT_FILTERS[flt])
        cur.execute("SELECT * FROM daily_snapshots ORDER BY day DESC")
        snapshots = cur.fetchall()
    signed_days = {s["day"] for s in snapshots}
    return render_template(
        "ledger.html",
        days=days,
        snapshots=snapshots,
        signed_days=signed_days,
        flt=flt,
        can_write=session.get("role") == "writer",
        today=datetime.now(DAY_ZONE).date(),
    )


@app.post("/snapshots")
@writer_required
def sign_snapshot():
    day_text = request.form.get("day", "").strip()
    try:
        day = datetime.strptime(day_text, "%Y-%m-%d").date() if day_text else datetime.now(DAY_ZONE).date()
    except ValueError:
        return ("日期格式应为 YYYY-MM-DD", 400)
    # 签发瞬间把当时的通过/不通过条数与命中编号整体拷贝冻结；
    # ON CONFLICT DO NOTHING 保证某日快照只此一份、互不覆盖。
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """INSERT INTO daily_snapshots
                   (day, pass_count, fail_count, pass_ids, fail_ids, signed_by)
               SELECT %s::date,
                      count(*) FILTER (WHERE verdict = '通过')::int,
                      count(*) FILTER (WHERE verdict = '不通过')::int,
                      coalesce(array_agg(id ORDER BY id) FILTER (WHERE verdict = '通过'), '{}'),
                      coalesce(array_agg(id ORDER BY id) FILTER (WHERE verdict = '不通过'), '{}'),
                      %s
               FROM cuppings
               WHERE (created_at AT TIME ZONE 'Asia/Shanghai')::date = %s::date
               ON CONFLICT (day) DO NOTHING
               RETURNING id""",
            (day.isoformat(), session["user"], day.isoformat()),
        )
        created = cur.fetchone()
        conn.commit()
    if not created:
        return ("该日快照已签发，不可覆盖", 409)
    return redirect(url_for("ledger"))
