import os
from datetime import datetime
from functools import wraps

import psycopg2
import psycopg2.errors
from flask import Flask, redirect, render_template, request, session, url_for
from psycopg2.extras import RealDictCursor

from rules import weigh

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "tea-cupping-dev-secret")

ACCOUNTS = {
    "taster": {"password": "tea123456", "role": "writer"},
    "observer": {"password": "look123456", "role": "reader"},
}

# 现场日账结论筛选：交给服务端 SQL 计算，不在浏览器里藏行
VERDICT_FILTER = {"pass": "通过", "fail": "不通过"}


def db():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def login_required(fn):
    @wraps(fn)
    def wrap(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
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


def chart_series(days, key):
    """把日账折点换算成 SVG 坐标，返回 (折线点串, 圆点列表)。"""
    width, height, pad = 560.0, 160.0, 24.0
    peak = max([day[key] for day in days] + [1])
    n = len(days)
    dots = []
    for i, day in enumerate(days):
        x = pad + (width - 2 * pad) * (i / (n - 1) if n > 1 else 0.5)
        y = height - pad - (height - 2 * pad) * (day[key] / peak)
        dots.append({"x": round(x, 1), "y": round(y, 1), "day": day["day"], "count": day[key]})
    line = " ".join(f"{p['x']},{p['y']}" for p in dots)
    return {"line": line, "dots": dots}


@app.get("/ledger")
@login_required
def ledger():
    filter_key = request.args.get("verdict", "all")
    verdict = VERDICT_FILTER.get(filter_key)
    if verdict is None:
        filter_key = "all"
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """SELECT created_at::date AS day,
                      COUNT(*) FILTER (WHERE verdict = '通过') AS pass_count,
                      COUNT(*) FILTER (WHERE verdict = '不通过') AS fail_count
               FROM cuppings
               GROUP BY created_at::date
               ORDER BY day"""
        )
        days = cur.fetchall()
        if verdict:
            cur.execute(
                "SELECT * FROM cuppings WHERE verdict = %s ORDER BY id DESC", (verdict,)
            )
        else:
            cur.execute("SELECT * FROM cuppings ORDER BY id DESC")
        rows = cur.fetchall()
        cur.execute("SELECT * FROM day_snapshots ORDER BY day DESC")
        snapshots = cur.fetchall()
    signed_days = {snap["day"] for snap in snapshots}
    for day in days:
        day["signed"] = day["day"] in signed_days
    chart = None
    if days:
        chart = {
            "pass": chart_series(days, "pass_count"),
            "fail": chart_series(days, "fail_count"),
        }
    return render_template(
        "ledger.html",
        days=days,
        rows=rows,
        snapshots=snapshots,
        chart=chart,
        filter_key=filter_key,
        can_write=session.get("role") == "writer",
    )


@app.post("/ledger/snapshots")
@login_required
def sign_snapshot():
    if session.get("role") != "writer":
        return ("仅审评员可签发快照", 403)
    try:
        day = datetime.strptime(request.form.get("day", "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return ("日期格式应为 YYYY-MM-DD", 400)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """SELECT COUNT(*) FILTER (WHERE verdict = '通过') AS pass_count,
                      COUNT(*) FILTER (WHERE verdict = '不通过') AS fail_count,
                      COALESCE(array_agg(id ORDER BY id) FILTER (WHERE verdict = '通过'), '{}') AS pass_ids,
                      COALESCE(array_agg(id ORDER BY id) FILTER (WHERE verdict = '不通过'), '{}') AS fail_ids
               FROM cuppings
               WHERE created_at::date = %s""",
            (day,),
        )
        agg = cur.fetchone()
        try:
            cur.execute(
                """INSERT INTO day_snapshots (day, pass_count, fail_count, pass_ids, fail_ids, signed_by)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (
                    day,
                    agg["pass_count"],
                    agg["fail_count"],
                    agg["pass_ids"],
                    agg["fail_ids"],
                    session["user"],
                ),
            )
            conn.commit()
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
            return ("该日快照已签发，已签发快照一律不可改动", 409)
    return redirect(url_for("ledger"))
