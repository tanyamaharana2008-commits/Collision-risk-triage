#!/usr/bin/env python3
"""
Stage 8: Kessler Eye, the web frontend (orbital close-approach monitor).

Run:   python stage8_app.py
Opens: http://localhost:8000

Reads your PostgreSQL tables live (conjunction_events, risk_assessments,
alerts, space_objects). The only thing it ever writes is alerts.is_read,
when you click "Mark read" in the page.

No extra installs needed beyond what Stage 6 already used (psycopg2).
"""
import json
import os
import re
import sys
import threading
import traceback
import webbrowser
from datetime import datetime, date, timezone, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ---------------------------------------------------------------- settings
DB_PASSWORD = os.environ.get("DB_PASSWORD", "space")   # <- set this
DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_NAME = os.environ.get("DB_NAME", "space_collision_db")
DB_USER = os.environ.get("DB_USER", "postgres")
PORT = 8000
# "Did it collide?" verdict, from the predicted miss distance (metres).
HBR_M = 20.0       # assumed combined size of the two objects: closer than this = predicted collision
CLOSE_M = 200.0    # very close pass
NEAR_M = 1000.0    # near miss; anything farther is a clear pass
EVENT_TAG = "PROTO-%"          # the tag Stage 6 put on every event it stored
# ------------------------------------------------------------------------

try:
    import psycopg2 as pg
except ImportError:
    try:
        import psycopg as pg
    except ImportError:
        sys.exit("psycopg2 is not installed. Run:  pip install psycopg2-binary")

LEVELS = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}


def connect():
    return pg.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
                      user=DB_USER, password=DB_PASSWORD)


def q(sql, params=()):
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def execute(sql, params=()):
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        n = cur.rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def ident(s):
    return '"' + s.replace('"', '""') + '"'


def num(v):
    return None if v is None else float(v)


def key(v):
    """Ids from the URL: digits become ints so PostgreSQL compares them as numbers."""
    return int(v) if str(v).isdigit() else v


# ------------------------------------------------- schema auto-detection
SCH = {}


def pk(table):
    r = q("SELECT a.attname AS c FROM pg_index i "
          "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
          "WHERE i.indrelid = %s::regclass AND i.indisprimary", (table,))
    return r[0]["c"] if r else None


def schema():
    if SCH:
        return SCH
    cols = [r["column_name"] for r in q(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = %s ORDER BY ordinal_position", ("space_objects",))]
    low = {c.lower(): c for c in cols}

    def pick(cands, contains):
        for c in cands:
            if c in low:
                return low[c]
        for c in cols:
            if contains in c.lower():
                return c
        return None

    SCH["cpk"] = pk("conjunction_events") or "conjunction_id"
    SCH["apk"] = pk("alerts") or "alert_id"
    SCH["name"] = pick(["name", "object_name", "satellite_name", "sat_name", "official_name"], "name")
    SCH["norad"] = pick(["norad_id", "norad_cat_id", "norad_catalog_id", "norad_number"], "norad")
    return SCH


def parts():
    s = schema()
    cpk = ident(s["cpk"])
    pn = ("po." + ident(s["name"])) if s["name"] else "NULL"
    sn = ("so." + ident(s["name"])) if s["name"] else "NULL"
    pr = ("po." + ident(s["norad"])) if s["norad"] else "NULL"
    sr = ("so." + ident(s["norad"])) if s["norad"] else "NULL"
    select = (
        f"c.{cpk} AS id, EXTRACT(EPOCH FROM c.tca) * 1000 AS tca_ms, "
        "c.miss_distance_m AS miss_m, c.relative_velocity_mps AS vel_mps, "
        "c.status AS status, c.external_event_id AS ext_id, "
        "r.risk_score AS score, r.risk_level AS level, "
        f"{pn}::text AS p_name, {pr}::text AS p_norad, "
        f"{sn}::text AS s_name, {sr}::text AS s_norad"
    )
    frm = (
        "FROM conjunction_events c "
        f"JOIN risk_assessments r ON r.conjunction_id = c.{cpk} "
        "JOIN space_objects po ON po.object_id = c.primary_object_id "
        "JOIN space_objects so ON so.object_id = c.secondary_object_id"
    )
    return select, frm, pn, sn, pr, sr


def where(scope, levels, text, now_ms, st=""):
    s = schema()
    _, _, pn, sn, pr, sr = parts()
    w = ["c.external_event_id LIKE %s"]
    p = [EVENT_TAG]
    if scope == "upcoming":
        w.append("EXTRACT(EPOCH FROM c.tca) * 1000 > %s")
        p.append(now_ms)
    elif scope == "passed":
        w.append("EXTRACT(EPOCH FROM c.tca) * 1000 <= %s")
        p.append(now_ms)
    levels = [l for l in levels if l in LEVELS]
    if levels:
        w.append("r.risk_level IN (" + ",".join(["%s"] * len(levels)) + ")")
        p.extend(levels)
    if st == "OPEN":
        w.append("(c.status IS NULL OR c.status = 'OPEN')")
    elif st in STATUSES:
        w.append("c.status = %s")
        p.append(st)
    if text:
        like = "%" + text + "%"
        ors = []
        for expr in (pn, sn, pr, sr):
            if expr != "NULL":
                ors.append(f"{expr}::text ILIKE %s")
                p.append(like)
        if ors:
            w.append("(" + " OR ".join(ors) + ")")
    return " AND ".join(w), p


def now_ms():
    return datetime.now().timestamp() * 1000


# ----------------------------------------------- what-if slider + decisions
WI = {}
STATUSES = ("OPEN", "ESCALATED", "MONITORING", "DISMISSED")


def get_m(qs):
    """Uncertainty multiplier from the slider (0.5 to 3.0). 1.0 means 'as stored'."""
    try:
        m = float(qs.get("m", 1))
    except (TypeError, ValueError):
        return 1.0
    m = max(0.5, min(3.0, m))
    return 1.0 if abs(m - 1.0) < 1e-9 else round(m, 2)


# Stage 5 scoring rules, copied from stage5_risk_scoring.py.
# If you change the settings there, change these four numbers to match.
MAX_POINTS_DISTANCE = 60
DIST_ZERO_KM = 2.0
HIGH_THRESHOLD = 70
MEDIUM_THRESHOLD = 40


def whatif_model():
    """The slider uses Stage 5's own distance rule, so x1.0 reproduces your stored scores."""
    if not WI:
        WI["ok"] = True
    return WI


def distance_points(miss_km):
    return MAX_POINTS_DISTANCE * max(0.0, min(1.0, 1.0 - miss_km / DIST_ZERO_KM))


def rescore(d_m, score, level, m):
    """What-if: assume the true miss distance could be (reported distance / m).
    Only the distance points change; time and velocity points stay as stored.
    Levels follow Stage 5: no distance points = LOW, then 70+ HIGH, 40+ MEDIUM."""
    if m == 1.0:
        return score, level
    old_pts = distance_points(d_m / 1000.0)
    new_pts = distance_points(d_m / 1000.0 / m)
    ns = max(0.0, min(100.0, score - old_pts + new_pts))
    if new_pts <= 0:
        return ns, "LOW"
    return ns, ("HIGH" if ns >= HIGH_THRESHOLD else "MEDIUM" if ns >= MEDIUM_THRESHOLD else "LOW")


def verdict(miss_m, m=1.0):
    d = float(miss_m) / m
    return "COLLISION" if d <= HBR_M else "CLOSE" if d <= CLOSE_M else "NEAR" if d <= NEAR_M else "CLEAR"


def decorate(rows, m):
    for r in rows:
        r["verdict"] = verdict(r["miss_m"], m)
    return rows


def wi_rows(scope, text, m, st=""):
    whatif_model()
    _, frm, *_ = parts()
    w, p = where(scope, [], text, now_ms(), st)
    rows = q(f"SELECT c.{ident(schema()['cpk'])} AS id, EXTRACT(EPOCH FROM c.tca) AS t, "
             f"c.miss_distance_m AS d, r.risk_score AS s, r.risk_level AS l {frm} WHERE {w}", p)
    out = []
    for r in rows:
        d, s = float(r["d"]), float(r["s"])
        ns, nl = rescore(d, s, r["l"], m)
        out.append((r["id"], float(r["t"]), d, s, ns, nl))
    return out


def api_set_status(body):
    st = str(body.get("status", "")).upper()
    if st not in STATUSES:
        raise ValueError("Unknown status")
    n = execute(f"UPDATE conjunction_events SET status = %s WHERE {ident(schema()['cpk'])} = %s",
                (st, key(body["id"])))
    return {"updated": n, "status": st}


# ------------------------------------------------------------------- API
def api_summary(m=1.0):
    out = _summary_stored(m)
    ok = whatif_model()["ok"]
    out["whatif"] = ok
    if m != 1.0 and ok:
        counts = {"upcoming": {}, "passed": {}}
        for _id, t, _d, _o, _ns, nl in wi_rows("all", "", m):
            k = "upcoming" if t * 1000 > out["now"] else "passed"
            counts[k][nl] = counts[k].get(nl, 0) + 1
        out["counts"] = counts
    return out


def _summary_stored(m=1.0):
    s = schema()
    cpk = ident(s["cpk"])
    n = now_ms()
    rows = q(
        "SELECT r.risk_level AS lvl, (EXTRACT(EPOCH FROM c.tca) * 1000 > %s) AS upcoming, COUNT(*) AS n "
        f"FROM conjunction_events c JOIN risk_assessments r ON r.conjunction_id = c.{cpk} "
        "WHERE c.external_event_id LIKE %s GROUP BY 1, 2", (n, EVENT_TAG))
    counts = {"upcoming": {}, "passed": {}}
    for r in rows:
        counts["upcoming" if r["upcoming"] else "passed"][r["lvl"]] = int(r["n"])
    unread = q("SELECT COUNT(*) AS n FROM alerts WHERE NOT is_read")[0]["n"]
    total_alerts = q("SELECT COUNT(*) AS n FROM alerts")[0]["n"]
    ex = q("SELECT MIN(c.miss_distance_m) AS dmin, MAX(c.relative_velocity_mps) AS vmax, "
           "COUNT(*) FILTER (WHERE EXTRACT(EPOCH FROM c.tca)*1000 <= %(n)s) AS passed, "
           "COUNT(*) FILTER (WHERE EXTRACT(EPOCH FROM c.tca)*1000 <= %(n)s AND c.miss_distance_m <= %(a)s) AS collision, "
           "COUNT(*) FILTER (WHERE EXTRACT(EPOCH FROM c.tca)*1000 <= %(n)s AND c.miss_distance_m > %(a)s AND c.miss_distance_m <= %(b)s) AS close, "
           "COUNT(*) FILTER (WHERE EXTRACT(EPOCH FROM c.tca)*1000 <= %(n)s AND c.miss_distance_m > %(b)s AND c.miss_distance_m <= %(c)s) AS near "
           "FROM conjunction_events c WHERE c.external_event_id LIKE %(tag)s",
           {"n": n, "tag": EVENT_TAG, "a": HBR_M * m, "b": CLOSE_M * m, "c": NEAR_M * m})[0]
    tri = {r["s"]: int(r["n"]) for r in q(
        "SELECT COALESCE(c.status, 'OPEN') AS s, COUNT(*) AS n FROM conjunction_events c "
        "WHERE c.external_event_id LIKE %s GROUP BY 1", (EVENT_TAG,))}
    select, frm, *_ = parts()
    base = (f"SELECT {select} {frm} WHERE c.external_event_id LIKE %s "
            "AND EXTRACT(EPOCH FROM c.tca)*1000 > %s ")
    nx = (q(base + "AND r.risk_level IN ('HIGH','CRITICAL') ORDER BY c.tca ASC LIMIT 1", (EVENT_TAG, n))
          or q(base + "ORDER BY c.tca ASC LIMIT 1", (EVENT_TAG, n)))
    return {"now": n, "counts": counts, "unread": int(unread), "alerts_total": int(total_alerts),
            "stats": {"dmin": num(ex["dmin"]), "vmax": num(ex["vmax"])},
            "outcomes": {k: int(ex[k] or 0) for k in ("passed", "collision", "close", "near")},
            "triage": tri, "next": clean(nx[0]) if nx else None,
            "thr": {"hbr": HBR_M, "close": CLOSE_M, "near": NEAR_M}}


def api_points(qs, m=1.0):
    scope = qs.get("scope", "upcoming")
    if m != 1.0 and whatif_model()["ok"]:
        rows = wi_rows(scope, "", m)
        return {
            "id": [r[0] for r in rows],
            "t": [round(r[1]) for r in rows],
            "d": [round(r[2], 1) for r in rows],
            "s": [round(r[4], 1) for r in rows],
            "l": [LEVELS.get(r[5], 0) for r in rows],
        }
    select, frm, *_ = parts()
    w, p = where(scope, [], "", now_ms())
    rows = q(f"SELECT c.{ident(schema()['cpk'])} AS id, EXTRACT(EPOCH FROM c.tca) AS t, "
             f"c.miss_distance_m AS d, r.risk_score AS s, r.risk_level AS l {frm} WHERE {w}", p)
    return {
        "id": [r["id"] for r in rows],
        "t": [round(float(r["t"])) for r in rows],
        "d": [round(float(r["d"]), 1) for r in rows],
        "s": [round(float(r["s"]), 1) for r in rows],
        "l": [LEVELS.get(r["l"], 0) for r in rows],
    }


def api_events(qs, m=1.0):
    scope = qs.get("scope", "upcoming")
    levels = [x for x in qs.get("levels", "").split(",") if x]
    text = qs.get("q", "").strip()[:60]
    sort = qs.get("sort", "score")
    st = qs.get("st", "")
    limit = 100000 if qs.get("export") == "1" else max(1, min(int(qs.get("limit", 50)), 500))
    select, frm, *_ = parts()
    cpk = "c." + ident(schema()["cpk"])
    if m != 1.0 and whatif_model()["ok"]:
        rows = wi_rows(scope, text, m, st)
        if levels:
            rows = [r for r in rows if r[5] in levels]
        keyfn = {"soonest": lambda r: (r[1], r[0]),
                 "closest": lambda r: (r[2], r[0])}.get(sort, lambda r: (-r[4], r[0]))
        rows.sort(key=keyfn)
        top = rows[:limit]
        det = {}
        if top:
            for r in q(f"SELECT {select} {frm} WHERE {cpk} = ANY(%s)", ([r[0] for r in top],)):
                det[r["id"]] = clean(r)
        out = []
        for r in top:
            d = det.get(r[0])
            if d:
                d["stored_score"], d["stored_level"] = d["score"], d["level"]
                d["score"], d["level"] = r[4], r[5]
                out.append(d)
        return {"total": len(rows), "rows": decorate(out, m)}
    order = {"score": f"r.risk_score DESC, {cpk}",
             "soonest": f"c.tca ASC, {cpk}",
             "closest": f"c.miss_distance_m ASC, {cpk}"}.get(sort, "r.risk_score DESC")
    w, p = where(scope, levels, text, now_ms(), st)
    total = q(f"SELECT COUNT(*) AS n {frm} WHERE {w}", p)[0]["n"]
    rows = q(f"SELECT {select} {frm} WHERE {w} ORDER BY {order} LIMIT {limit}", p)
    return {"total": int(total), "rows": decorate([clean(r) for r in rows], m)}


def api_event(eid, m=1.0):
    select, frm, *_ = parts()
    cpk = "c." + ident(schema()["cpk"])
    rows = q(f"SELECT {select}, r.explanation AS explanation, r.time_to_tca_seconds AS ttca {frm} "
             f"WHERE {cpk} = %s", (key(eid),))
    if not rows:
        return None
    out = clean(rows[0])
    out["verdict"] = verdict(out["miss_m"], m)
    if m != 1.0 and whatif_model()["ok"]:
        out["stored_score"], out["stored_level"] = out["score"], out["level"]
        out["score"], out["level"] = rescore(out["miss_m"], out["score"], out["level"], m)
    out["alerts"] = [clean(a) for a in q(
        f"SELECT {ident(schema()['apk'])} AS id, severity, title, is_read FROM alerts "
        "WHERE conjunction_id = %s ORDER BY 1", (key(eid),))]
    return out


VERDICT_TXT = {"COLLISION": "Collision predicted", "CLOSE": "Very close pass",
               "NEAR": "Near miss", "CLEAR": "Clear pass"}


def _safe(v):
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


def api_export(qs, m):
    import csv
    import io
    rows = api_events(dict(qs, export="1"), m)["rows"]
    ist = timezone(timedelta(hours=5, minutes=30))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["event_id", "object_1", "norad_1", "object_2", "norad_2", "closest_approach_utc",
                "closest_approach_ist", "miss_distance_m", "relative_speed_mps", "score", "risk_level",
                "decision", "outcome"])
    n = now_ms()
    for r in rows:
        t = datetime.fromtimestamp(r["tca_ms"] / 1000, timezone.utc)
        past = r["tca_ms"] <= n
        w.writerow([_safe(r["ext_id"]), _safe(r["p_name"]), _safe(r["p_norad"]), _safe(r["s_name"]),
                    _safe(r["s_norad"]), t.strftime("%Y-%m-%d %H:%M:%S"),
                    t.astimezone(ist).strftime("%Y-%m-%d %H:%M:%S"), round(r["miss_m"], 1),
                    round(r["vel_mps"], 1), round(r["score"], 1), r["level"], r["status"] or "OPEN",
                    VERDICT_TXT[r["verdict"]] if past else "Upcoming"])
    name = "kessler-eye-" + datetime.now().strftime("%Y%m%d-%H%M") + ".csv"
    return ("\ufeff" + buf.getvalue()).encode("utf-8"), name


def api_alerts(qs):
    unread_only = qs.get("unread") == "1"
    limit = max(1, min(int(qs.get("limit", 100)), 500))
    s = schema()
    apk = "a." + ident(s["apk"])
    cpk = "c." + ident(s["cpk"])
    cond = "WHERE NOT a.is_read" if unread_only else ""
    rows = q(
        f"SELECT {apk} AS id, a.conjunction_id AS conj, a.severity, a.title, a.message, a.is_read, "
        "EXTRACT(EPOCH FROM a.created_at) * 1000 AS created_ms, EXTRACT(EPOCH FROM c.tca) * 1000 AS tca_ms "
        f"FROM alerts a LEFT JOIN conjunction_events c ON {cpk} = a.conjunction_id {cond} "
        f"ORDER BY a.created_at DESC, {apk} DESC LIMIT {limit}")
    return {"rows": [clean(r) for r in rows]}


def api_mark_read(body):
    s = schema()
    if body.get("all"):
        n = execute("UPDATE alerts SET is_read = TRUE WHERE NOT is_read")
    else:
        n = execute(f"UPDATE alerts SET is_read = TRUE WHERE {ident(s['apk'])} = %s", (key(body["id"]),))
    return {"updated": n}


def clean(row):
    out = {}
    for k, v in row.items():
        if isinstance(v, Decimal):
            v = float(v)
        elif isinstance(v, (datetime, date)):
            v = v.isoformat()
        out[k] = v
    return out


# ---------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, payload, ctype="application/json; charset=utf-8", extra=None):
        body = payload if isinstance(payload, bytes) else json.dumps(payload, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/":
                return self.reply(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            mm = get_m(qs)
            if u.path == "/api/export":
                body, fn = api_export(qs, mm)
                return self.reply(200, body, "text/csv; charset=utf-8",
                                  {"Content-Disposition": f'attachment; filename="{fn}"'})
            if u.path == "/api/summary":
                return self.reply(200, api_summary(mm))
            if u.path == "/api/points":
                return self.reply(200, api_points(qs, mm))
            if u.path == "/api/events":
                return self.reply(200, api_events(qs, mm))
            if u.path == "/api/alerts":
                return self.reply(200, api_alerts(qs))
            m = re.fullmatch(r"/api/event/([^/]+)", u.path)
            if m:
                ev = api_event(m.group(1), mm)
                return self.reply(200 if ev else 404, ev or {"error": "Event not found"})
            return self.reply(404, {"error": "Not found"})
        except Exception as e:
            traceback.print_exc()
            return self.reply(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        try:
            if urlparse(self.path).path == "/api/event/status":
                n = int(self.headers.get("Content-Length", 0))
                return self.reply(200, api_set_status(json.loads(self.rfile.read(n) or b"{}")))
            if urlparse(self.path).path == "/api/alerts/read":
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                return self.reply(200, api_mark_read(body))
            return self.reply(404, {"error": "Not found"})
        except Exception as e:
            traceback.print_exc()
            return self.reply(500, {"error": f"{type(e).__name__}: {e}"})


def main():
    global PORT
    args = sys.argv[1:]
    if "--port" in args:
        PORT = int(args[args.index("--port") + 1])
    try:
        s = schema()
        total = q("SELECT COUNT(*) AS n FROM conjunction_events WHERE external_event_id LIKE %s", (EVENT_TAG,))[0]["n"]
    except Exception as e:
        print("Could not read the database.")
        print(f"  {type(e).__name__}: {e}")
        print("Check that PostgreSQL is running and that DB_PASSWORD is set at the top of this file.")
        sys.exit(1)
    print(f"Connected to {DB_NAME}. Found {total} stored close approaches.")
    print(f"Object name column: {s['name'] or 'not found (showing IDs only)'}; "
          f"NORAD column: {s['norad'] or 'not found'}")
    if "--check" in args:
        return
    server = None
    tried = [PORT] if "--port" in args else [PORT, 8080, 8765, 5055, 5000, 3000, 8888, 9000, 0]
    for p in tried:
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            break
        except OSError:
            print(f"Port {p} is not available, trying another.")
    if server is None:
        sys.exit("No port was available. Try:  python stage8_app.py --port 7123")
    PORT = server.server_address[1]
    url = f"http://localhost:{PORT}"
    print(f"Kessler Eye running at {url}   (press Ctrl+C to stop)")
    if "--no-browser" not in args:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


PAGE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kessler Eye · orbital close-approach monitor</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;600;700&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{
  --void:#0A0F1E; --panel:#10172E; --raise:#18213F; --line:#222D4D;
  --ink:#E8EDF7; --mute:#8C98B6; --faint:#5C6988;
  --low:#6F94D6; --med:#FFB547; --high:#FF5C8A; --focus:#8FD3FF;
  --display:'Bricolage Grotesque','IBM Plex Sans',system-ui,sans-serif;
  --body:'IBM Plex Sans',system-ui,-apple-system,'Segoe UI',sans-serif;
}
*{box-sizing:border-box}
html{color-scheme:dark}
body{margin:0;background:var(--void);color:var(--ink);font:400 15px/1.55 var(--body);font-variant-numeric:tabular-nums;-webkit-font-smoothing:antialiased}
button,input,select{font:inherit;color:inherit}
[hidden]{display:none!important}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px;border-radius:6px}
.wrap{max-width:1240px;margin:0 auto;padding:22px 24px 48px}
.mute{color:var(--mute)}

/* header */
.top{display:flex;align-items:center;justify-content:space-between;gap:16px}
.brand{display:flex;align-items:center;gap:10px;font:600 17px/1 var(--display);letter-spacing:-.01em}
.top-r{display:flex;align-items:center;gap:14px;font-size:13px}
.btn{background:transparent;border:1px solid var(--line);border-radius:8px;padding:7px 14px;cursor:pointer;transition:background .15s,border-color .15s}
.btn:hover{background:var(--raise);border-color:#33426b}
.btn.small{padding:4px 10px;font-size:12.5px}

.banner{margin-top:18px;padding:12px 16px;border:1px solid #5a2a40;background:#2a1220;border-radius:10px;font-size:14px}
.banner b{font-weight:600}

/* hero */
.hero{padding:44px 0 26px}
h1{margin:0;font:600 clamp(30px,4.6vw,56px)/1.03 var(--display);letter-spacing:-.025em;max-width:20ch}
.lede{margin:16px 0 0;max-width:62ch;color:var(--mute);font-size:16px}

/* controls */
.controls{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;margin-bottom:14px}
.chips,.seg{display:flex;flex-wrap:wrap;gap:8px}
.chip{display:inline-flex;align-items:center;gap:9px;padding:8px 15px;border:1px solid var(--line);border-radius:999px;background:var(--panel);cursor:pointer;transition:opacity .15s,background .15s,border-color .15s}
.chip .dot{width:9px;height:9px;border-radius:50%;background:var(--c)}
.chip b{font-weight:600}
.chip[aria-pressed="true"]{border-color:var(--c);background:var(--raise)}
.chip[aria-pressed="false"]{opacity:.5}
.seg{gap:0;border:1px solid var(--line);border-radius:10px;padding:3px;background:var(--panel)}
.seg button{border:0;background:transparent;padding:6px 14px;border-radius:7px;cursor:pointer;color:var(--mute)}
.seg button[aria-pressed="true"]{background:var(--raise);color:var(--ink)}

/* chart */
.chart-card{background:var(--panel);border:1px solid var(--line);border-radius:22px;padding:18px 18px 12px}
.plot{position:relative;height:clamp(300px,46vh,460px)}
.plot canvas{position:absolute;inset:0}
#fx{cursor:crosshair}
#tip{position:absolute;pointer-events:none;background:#060a16;border:1px solid var(--line);border-radius:8px;padding:8px 11px;font-size:12.5px;line-height:1.45;white-space:nowrap;opacity:0;transition:opacity .08s;z-index:3}
#empty{position:absolute;inset:0;display:grid;place-items:center;text-align:center;color:var(--mute);padding:24px}
.hint{margin:8px 4px 2px;font-size:12.5px;color:var(--faint)}

/* panels */
.grid{display:grid;grid-template-columns:minmax(0,1fr) 380px;gap:20px;margin-top:20px;align-items:start}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;overflow:hidden}
.panel-h{display:flex;flex-wrap:wrap;gap:10px 14px;align-items:center;justify-content:space-between;padding:16px 18px 12px}
h2{margin:0;font:600 19px/1.2 var(--display);letter-spacing:-.01em}
.tools{display:flex;gap:8px;flex-wrap:wrap}
.tools input,.tools select{background:var(--void);border:1px solid var(--line);border-radius:8px;padding:7px 11px;font-size:13.5px}
.tools input{width:230px}
.tools input::placeholder{color:var(--faint)}

table{width:100%;border-collapse:collapse}
th{font-weight:500;font-size:12.5px;color:var(--faint);text-align:left;padding:8px 18px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:11px 18px;border-bottom:1px solid #1a2342;vertical-align:middle}
tbody tr{cursor:pointer}
tbody tr:hover{background:var(--raise)}
tbody tr.past{opacity:.55}
.obj{display:flex;gap:11px;align-items:center;min-width:0}
.obj .dot{flex:none;width:9px;height:9px;border-radius:50%;background:var(--c)}
.obj .nm{font-weight:500;line-height:1.3;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:230px}
.obj .nm2{color:var(--mute);font-size:13px;line-height:1.3;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:230px}
.sub{color:var(--mute);font-size:12.5px}
.score{display:flex;align-items:center;gap:10px}
.score .n{width:38px;font-weight:600}
.bar{display:block;width:64px;height:5px;border-radius:3px;background:#1b2544;overflow:hidden}
.bar i{display:block;height:100%;border-radius:3px}
.tag{display:inline-block;margin-left:6px;padding:0 7px;border-radius:999px;font-size:11.5px;border:1px solid var(--line);color:var(--mute)}
.more{display:flex;justify-content:space-between;align-items:center;padding:12px 18px;color:var(--mute);font-size:13px}
.state{padding:32px 18px;color:var(--mute);text-align:center}

/* alerts */
.tabs{display:flex;gap:4px}
.tabs button{border:0;background:transparent;padding:5px 11px;border-radius:7px;color:var(--mute);cursor:pointer;font-size:13.5px}
.tabs button[aria-pressed="true"]{background:var(--raise);color:var(--ink)}
.alist{max-height:640px;overflow:auto}
.al{display:grid;grid-template-columns:12px 1fr auto;gap:11px;padding:13px 18px;border-top:1px solid #1a2342;cursor:pointer}
.al:hover{background:var(--raise)}
.al .dot{width:9px;height:9px;border-radius:50%;margin-top:7px;background:var(--c)}
.al.read .dot{background:transparent;border:1.5px solid var(--faint)}
.al.read{opacity:.6}
.al .t{font-weight:500;line-height:1.35}
.al .m{font-size:12.5px;color:var(--mute);margin-top:2px}

footer{margin-top:34px;color:var(--faint);font-size:12.5px;max-width:80ch}

/* drawer */
#scrim{position:fixed;inset:0;background:rgba(4,7,16,.6);opacity:0;pointer-events:none;transition:opacity .2s;z-index:10}
#scrim.on{opacity:1;pointer-events:auto}
#drawer{position:fixed;top:0;right:0;bottom:0;width:min(460px,100vw);background:var(--panel);border-left:1px solid var(--line);transform:translateX(102%);transition:transform .24s cubic-bezier(.2,.8,.2,1);z-index:11;overflow:auto;padding:22px 24px 32px}
#drawer.on{transform:none}
.d-top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.pill{display:inline-flex;align-items:center;gap:8px;padding:4px 12px;border-radius:999px;border:1px solid var(--c);font-size:13px;font-weight:500}
.pill .dot{width:8px;height:8px;border-radius:50%;background:var(--c)}
#drawer h3{margin:16px 0 4px;font:600 26px/1.15 var(--display);letter-spacing:-.02em}
.big{font:600 56px/1 var(--display);letter-spacing:-.03em;margin:18px 0 2px}
dl{margin:20px 0 0;display:grid;grid-template-columns:130px 1fr;gap:12px 14px}
dt{color:var(--mute);font-size:13.5px}
dd{margin:0}
.expl{margin-top:22px;padding:14px 16px;background:var(--void);border:1px solid var(--line);border-radius:10px;white-space:pre-wrap;font-size:13.5px;color:var(--mute);line-height:1.6}
.d-note{margin-top:16px;font-size:12.5px;color:var(--faint)}

/* what-if slider + decisions */
.whatif{margin:0 0 14px;padding:14px 18px 12px;background:var(--panel);border:1px solid var(--line);border-radius:14px}
.wi-top{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:8px;min-height:30px}
.wi-top label{font-weight:500}
.whatif input[type=range]{display:block;width:100%;accent-color:var(--focus);margin:4px 0}
.whatif input[type=range]:disabled{opacity:.4}
.wi-help{margin:6px 0 0;font-size:12.5px;color:var(--faint)}
.acts{display:flex;flex-wrap:wrap;gap:8px;margin-top:20px}
.acts .btn[aria-pressed="true"]{background:var(--raise);border-color:var(--focus)}

body{padding-bottom:54px}
.strip{display:flex;flex-wrap:wrap;gap:10px;margin-top:20px}
.stat{padding:10px 16px;border:1px solid var(--line);border-radius:12px;background:var(--panel);min-width:140px}
.stat b{display:block;font:600 22px/1.25 var(--display)}.stat span{font-size:12.5px;color:var(--mute)}
.next{margin-top:18px;display:inline-flex;flex-wrap:wrap;gap:6px 12px;align-items:baseline;padding:9px 18px;border:1px solid var(--line);border-radius:999px;background:var(--panel);cursor:pointer}
.next:hover{background:var(--raise)}.cd{font:600 22px var(--display);color:var(--high);letter-spacing:.02em}
.vt{display:inline-block;padding:2px 10px;border-radius:999px;border:1px solid var(--c);color:var(--c);font-size:12.5px;white-space:nowrap}
.out{margin-top:18px;padding:14px 16px;border:1px solid var(--c);border-radius:12px}
.out h4{margin:0;font:500 13px var(--body);color:var(--mute)}.out .ans{font:600 22px/1.25 var(--display);margin:4px 0 6px}
tbody tr:focus-visible{background:var(--raise)}
.fkeys{position:fixed;left:0;right:0;bottom:0;display:flex;gap:4px;padding:6px 8px;background:#070b18;border-top:1px solid var(--line);z-index:9;overflow-x:auto}
.fkeys button{flex:1 0 auto;min-width:78px;display:flex;gap:7px;align-items:center;justify-content:center;border:1px solid var(--line);background:var(--panel);border-radius:6px;padding:6px 8px;cursor:pointer;font-size:12.5px;color:var(--mute)}
.fkeys button:hover{background:var(--raise);color:var(--ink)}
kbd{font:600 11.5px var(--body);color:var(--focus)}
#toast{position:fixed;left:50%;bottom:64px;transform:translateX(-50%);background:#060a16;border:1px solid var(--line);padding:8px 16px;border-radius:8px;z-index:20;opacity:0;transition:opacity .2s;pointer-events:none}
#toast.on{opacity:1}
#help{position:fixed;inset:0;display:grid;place-items:center;background:rgba(4,7,16,.72);z-index:30;padding:16px}
.hbox{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:22px 26px;max-width:460px;width:100%}
.hbox dl{margin:14px 0}
@media (max-width:980px){.grid{grid-template-columns:1fr}.alist{max-height:none}}
@media (max-width:720px){
  .wrap{padding:16px 16px 40px}
  .hero{padding:28px 0 20px}
  .tools input{width:100%}.tools{width:100%}
  .hide-s{display:none}
  td,th{padding-left:12px;padding-right:12px}
  .obj .nm,.obj .nm2{max-width:150px}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div class="brand">
      <svg width="26" height="26" viewBox="0 0 26 26" aria-hidden="true"><ellipse cx="13" cy="13" rx="11" ry="5.5" transform="rotate(-28 13 13)" fill="none" stroke="#8C98B6" stroke-width="1.5"/><circle cx="13" cy="13" r="3.4" fill="#E8EDF7"/><circle cx="21.6" cy="8.6" r="2.4" fill="#FF5C8A"/></svg>
      <span>Kessler Eye</span>
    </div>
    <div class="top-r"><span id="updated" class="mute"></span><button class="btn" id="exportBtn">Export CSV</button><button class="btn" id="helpBtn" aria-label="Keyboard shortcuts">Shortcuts</button><button class="btn" id="refresh">Refresh</button></div>
  </header>

  <div id="banner" class="banner" hidden></div>

  <section class="hero">
    <h1 id="headline">Loading close approaches</h1>
    <p class="lede" id="lede"></p>
    <div class="next" id="next" role="button" tabindex="0" hidden></div>
    <div class="strip" id="strip"></div>
  </section>

  <section class="controls">
    <div class="chips" id="chips"></div>
    <div class="seg" id="scope" role="group" aria-label="Time window">
      <button data-s="upcoming" aria-pressed="true">Upcoming</button>
      <button data-s="passed" aria-pressed="false">Passed</button>
      <button data-s="all" aria-pressed="false">All</button>
    </div>
  </section>

  <section class="whatif" id="whatif">
    <div class="wi-top"><label for="wi" id="wiLabel">What-if: uncertainty ×1.0</label><button class="btn small" id="wiReset" hidden>Reset to ×1.0</button></div>
    <input type="range" id="wi" min="0.5" max="3" step="0.1" value="1" aria-describedby="wiHelp">
    <p class="wi-help" id="wiHelp">Higher values assume the true miss distance could be proportionally smaller than reported, and re-rank everything on this page. Nothing is saved. Alerts keep their original severity.</p>
  </section>

  <section class="chart-card">
    <div class="plot" id="plot">
      <canvas id="cv"></canvas><canvas id="fx" role="img" aria-label="Scatter chart of predicted closest approaches: time against miss distance"></canvas>
      <div id="tip"></div><div id="empty" hidden></div>
    </div>
    <p class="hint">Each dot is one predicted closest approach. Left is sooner, lower is a smaller miss distance. Click a dot to see its details.</p>
  </section>

  <div class="grid">
    <section class="panel">
      <div class="panel-h">
        <h2>Highest-ranked approaches</h2>
        <div class="tools">
          <input id="search" type="search" placeholder="Search object name or NORAD ID" aria-label="Search objects">
          <select id="dec" aria-label="Filter by decision"><option value="">All decisions</option><option value="OPEN">Pending</option><option value="ESCALATED">Escalated</option><option value="MONITORING">Monitoring</option><option value="DISMISSED">Dismissed</option></select>
          <select id="sort" aria-label="Sort"><option value="score">Highest score</option><option value="soonest">Soonest</option><option value="closest">Smallest miss</option></select>
        </div>
      </div>
      <div style="overflow-x:auto">
        <table>
          <thead><tr><th>Objects</th><th>Closest approach</th><th>Miss distance</th><th class="hide-s">Speed</th><th>Score</th><th class="hide-s">Did it collide?</th></tr></thead>
          <tbody id="rows"></tbody>
        </table>
      </div>
      <div id="rowstate" class="state" hidden></div>
      <div class="more" id="more" hidden><span id="count"></span><button class="btn small" id="moreBtn">Show 50 more</button></div>
    </section>

    <section class="panel">
      <div class="panel-h">
        <h2>Alerts</h2>
        <div class="tabs" id="atabs"><button data-t="unread" aria-pressed="true">Unread</button><button data-t="all" aria-pressed="false">All</button></div>
      </div>
      <div style="padding:0 18px 12px;display:flex;justify-content:space-between;align-items:center"><span class="sub" id="acount"></span><button class="btn small" id="markAll">Mark all read</button></div>
      <div class="alist" id="alist"></div>
    </section>
  </div>

  <footer>Scores come from a prototype model that weighs miss distance, time to closest approach and relative speed. They rank predicted closest approaches. They are not collision probabilities. Times are shown in IST with UTC alongside. Data is read live from your PostgreSQL database. Collision verdicts compare each predicted miss distance with an assumed combined object size, so they are predictions, not confirmed impacts.</footer>
</div>

<nav class="fkeys" id="fkeys" aria-label="Function keys"></nav>
<div id="toast" role="status" aria-live="polite"></div>
<div id="help" hidden></div>
<div id="scrim"></div>
<aside id="drawer" role="dialog" aria-modal="true" aria-label="Event details" tabindex="-1"></aside>

<script>
const LV=['LOW','MEDIUM','HIGH','CRITICAL'], LN=['Low','Medium','High','Critical'];
const COL=['#6F94D6','#FFB547','#FF5C8A','#FF5C8A'];
const $=s=>document.querySelector(s);
const fmtN=n=>Number(n).toLocaleString('en-US');
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const S={m:1,wseq:0,scope:'upcoming',lv:new Set([0,1,2]),sort:'score',q:'',st:'',cur:null,limit:50,summary:null,pts:null,sc:null,
  events:null,alerts:null,alertTab:'unread',sel:null,hover:-1,pos:new Map(),buckets:[[],[],[]],anim:0};

/* ---------- formatting ---------- */
const tIST=new Intl.DateTimeFormat('en-GB',{timeZone:'Asia/Kolkata',day:'numeric',month:'short',hour:'2-digit',minute:'2-digit',hour12:false});
const tUTC=new Intl.DateTimeFormat('en-GB',{timeZone:'UTC',hour:'2-digit',minute:'2-digit',hour12:false});
const fmtDist=m=>m<1000?(m<100?m.toFixed(1):Math.round(m))+' m':(m/1000).toFixed(m<10000?2:1)+' km';
const fmtSpeed=v=>(v/1000).toFixed(2)+' km/s';
function rel(ms){const d=ms-Date.now(),a=Math.abs(d)/60000;let t;
  if(a<1)t='under a minute';else if(a<60)t=Math.round(a)+'m';else{const h=Math.floor(a/60);t=h+'h '+Math.round(a%60)+'m'}
  return d>=0?'in '+t:t+' ago'}
const istStr=ms=>tIST.format(new Date(ms)).replace(',','')+' IST';
const utcStr=ms=>tUTC.format(new Date(ms))+' UTC';
const nm=(n,id)=>n&&n!=='None'?n:('Object '+(id||'unknown'));
const lvl=l=>Math.max(0,LV.indexOf(l));
const bucket=l=>Math.min(l,2);
const STN={OPEN:'Pending',ESCALATED:'Escalated',MONITORING:'Monitoring',DISMISSED:'Dismissed'};
const stName=s=>STN[s]||'Pending';
const actsHtml=(id,st)=>`<div class="acts" id="acts" role="group" aria-label="Triage decision">
  <button class="btn" data-st="ESCALATED" data-id="${esc(id)}" aria-pressed="${st==='ESCALATED'}">Escalate</button>
  <button class="btn" data-st="MONITORING" data-id="${esc(id)}" aria-pressed="${st==='MONITORING'}">Monitor</button>
  <button class="btn" data-st="DISMISSED" data-id="${esc(id)}" aria-pressed="${st==='DISMISSED'}">Dismiss</button>
  <button class="btn small" id="actrs" data-st="OPEN" data-id="${esc(id)}" ${!st||st==='OPEN'?'hidden':''}>Reset to pending</button></div>
  <div class="d-note" id="actmsg" aria-live="polite"></div>`;
async function setStatus(id,st){
  const msg=$('#actmsg');
  try{
    await api('/api/event/status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id,status:st})});
    if($('#acts')){document.querySelectorAll('#acts [data-st]:not(#actrs)').forEach(b=>b.setAttribute('aria-pressed',b.dataset.st===st));
      $('#actrs').hidden=st==='OPEN';$('#dst').textContent=stName(st);
      msg.textContent=st==='OPEN'?'Reset to pending.':stName(st)+'.'}
    toast(st==='OPEN'?'Reset to pending':stName(st));
    api('/api/summary?m='+S.m).then(x=>{S.summary=x;renderStrip()}).catch(()=>{});
    if(S.events){const row=S.events.rows.find(x=>String(x.id)===String(id));if(row){row.status=st;renderRows()}}
  }catch(e){if(msg)msg.textContent='Could not save this decision. '+e.message;toast('Could not save. '+e.message)}}

async function api(path,opt){
  const r=await fetch(path,opt); let j; try{j=await r.json()}catch(e){j={}}
  if(!r.ok)throw new Error(j.error||('Request failed ('+r.status+')')); return j}
const levelsParam=()=>[...S.lv].map(i=>LV[i]).join(',');

/* ---------- loading ---------- */
function banner(msg){const b=$('#banner'); if(!msg){b.hidden=true;return}
  b.hidden=false;b.innerHTML='<b>Could not load data.</b> '+esc(msg)+' Check that PostgreSQL is running and that DB_PASSWORD is set in stage8_app.py, then press Refresh.'}

async function loadAll(animate){
  try{
    const [sum,pts,ev,al]=await Promise.all([api('/api/summary?m='+S.m),api('/api/points?scope='+S.scope+'&m='+S.m),eventsReq(),alertsReq()]);
    S.summary=sum; setPts(pts); S.events=ev; S.alerts=al; banner('');
    $('#wi').disabled=!sum.whatif;
    if(!sum.whatif)$('#wiHelp').textContent='The what-if slider needs the distance scores stored in risk_assessments, and none were found.';
    renderHero(); renderChips(); renderRows(); renderAlerts(); fit(); run(animate);
    $('#updated').textContent='Updated '+new Date().toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'});
  }catch(e){banner(e.message)}
}
const eventsReq=()=>api('/api/events?'+new URLSearchParams({scope:S.scope,levels:levelsParam(),q:S.q,sort:S.sort,limit:S.limit,m:S.m,st:S.st}));
const alertsReq=()=>api('/api/alerts?unread='+(S.alertTab==='unread'?1:0));
async function loadEvents(){try{S.events=await eventsReq();renderRows();banner('')}catch(e){banner(e.message)}}
async function loadScope(){try{setPts(await api('/api/points?scope='+S.scope+'&m='+S.m));S.events=await eventsReq();renderChips();renderRows();run(true)}catch(e){banner(e.message)}}
async function loadAlerts(){try{S.alerts=await alertsReq();S.summary=await api('/api/summary?m='+S.m);renderAlerts();renderHero()}catch(e){banner(e.message)}}

/* ---------- hero + chips ---------- */
function scopeCounts(scope){const c=S.summary.counts,out={};
  for(const k of (scope==='all'?['upcoming','passed']:[scope]))for(const l in c[k])out[l]=(out[l]||0)+c[k][l];return out}
function renderHero(){renderStrip();tickNext();
  const up=S.summary.counts.upcoming,tot=Object.values(up).reduce((a,b)=>a+b,0),hi=(up.HIGH||0)+(up.CRITICAL||0);
  if(!tot){$('#headline').textContent='No close approaches left in this window';
    $('#lede').textContent='Every predicted approach in the database has already passed. Re-run Stage 3 to Stage 6 to load a fresh 24-hour window.'}
  else{$('#headline').textContent=fmtN(hi)+' of '+fmtN(tot)+' upcoming close approaches rank high';
    $('#lede').textContent='Ranked by a prototype score built from miss distance, time to closest approach and relative speed. These are predicted closest approaches, not predicted collisions.'+(S.m!==1?' What-if view at uncertainty ×'+S.m.toFixed(1)+'. Nothing is saved.':'')}
}
function renderChips(){
  const c=scopeCounts(S.scope);
  $('#chips').innerHTML=[2,1,0].map(i=>{const n=i===2?(c.HIGH||0)+(c.CRITICAL||0):(c[LV[i]]||0);
    return `<button class="chip" data-l="${i}" style="--c:${COL[i]}" aria-pressed="${S.lv.has(i)}"><span class="dot"></span>${LN[i]} <b>${fmtN(n)}</b></button>`}).join('');
  document.querySelectorAll('#scope button').forEach(b=>b.setAttribute('aria-pressed',b.dataset.s===S.scope));
}

/* ---------- table ---------- */
function renderRows(){
  const E=S.events,tb=$('#rows'),st=$('#rowstate'),more=$('#more');
  if(!E.rows.length){tb.innerHTML='';st.hidden=false;more.hidden=true;
    st.textContent=S.q?'No approaches match that search. Try a different name or NORAD ID.':'No approaches in this view. Turn a risk level back on or switch the time window.';return}
  st.hidden=true;
  const now=Date.now();
  tb.innerHTML=E.rows.map(r=>{const l=lvl(r.level),past=r.tca_ms<=now;
    return `<tr tabindex="0" data-id="${esc(r.id)}" class="${past?'past':''}">
      <td><div class="obj" style="--c:${COL[l]}"><span class="dot" title="${LN[l]}"></span><div style="min-width:0"><div class="nm">${esc(nm(r.p_name,r.p_norad))}</div><div class="nm2">with ${esc(nm(r.s_name,r.s_norad))}</div></div></div></td>
      <td>${esc(istStr(r.tca_ms))}<div class="sub">${esc(utcStr(r.tca_ms))}, ${rel(r.tca_ms)}${past?'<span class="tag">Passed</span>':''}${r.status&&r.status!=='OPEN'?'<span class="tag">'+esc(stName(r.status))+'</span>':''}</div></td>
      <td>${fmtDist(r.miss_m)}</td><td class="hide-s">${fmtSpeed(r.vel_mps)}</td>
      <td><div class="score"><span class="n">${r.score.toFixed(1)}</span><span class="bar"><i style="width:${Math.max(3,Math.min(100,r.score))}%;background:${COL[l]}"></i></span></div>${r.stored_score!=null?'<div class="sub">was '+r.stored_score.toFixed(1)+'</div>':''}</td><td class="hide-s">${vtag(r)}</td></tr>`}).join('');
  more.hidden=false;$('#count').textContent='Showing '+fmtN(E.rows.length)+' of '+fmtN(E.total);
  $('#moreBtn').hidden=E.rows.length>=E.total;
}

/* ---------- alerts ---------- */
function renderAlerts(){
  const A=S.alerts.rows,L=$('#alist');
  $('#acount').textContent=fmtN(S.summary.unread)+' unread of '+fmtN(S.summary.alerts_total);
  $('#markAll').hidden=!S.summary.unread;
  if(!A.length){L.innerHTML='<div class="state">'+(S.alertTab==='unread'?'You are all caught up. No unread alerts.':'No alerts yet. Alerts appear when an approach ranks high.')+'</div>';return}
  L.innerHTML=A.slice(0,100).map(a=>{const l=Math.max(0,LV.indexOf(a.severity));
    return `<div class="al ${a.is_read?'read':''}" tabindex="0" data-c="${esc(a.conj)}" style="--c:${COL[l]}"><span class="dot"></span>
      <div><div class="t">${esc(a.title)}</div><div class="m">${a.tca_ms?'Closest approach '+esc(istStr(a.tca_ms))+', '+rel(a.tca_ms):'Severity '+esc(a.severity)}</div></div>
      ${a.is_read?'':`<button class="btn small" data-read="${esc(a.id)}">Mark read</button>`}</div>`}).join('');
}
async function markRead(body){
  try{await api('/api/alerts/read',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});await loadAlerts()}catch(e){banner(e.message)}}

/* ---------- chart ---------- */
const cv=$('#cv'),fx=$('#fx'),ctx=cv.getContext('2d'),fctx=fx.getContext('2d');
let W=0,H=0,DPR=1;const PAD={l:62,r:18,t:16,b:38};
function fit(){const r=$('#plot').getBoundingClientRect();W=r.width;H=r.height;DPR=window.devicePixelRatio||1;
  for(const c of [cv,fx]){c.width=Math.round(W*DPR);c.height=Math.round(H*DPR);c.style.width=W+'px';c.style.height=H+'px'}}
function setPts(P){
  S.pts=P;S.pos=new Map();S.buckets=[[],[],[]];S.hover=-1;
  P.id.forEach((id,i)=>{S.pos.set(String(id),i);S.buckets[bucket(P.l[i])].push(i)});
  const now=Date.now()/1000;let x0=Infinity,x1=-Infinity,d0=Infinity,d1=0;
  for(let i=0;i<P.t.length;i++){const x=(P.t[i]-now)/3600;if(x<x0)x0=x;if(x>x1)x1=x;if(P.d[i]<d0)d0=P.d[i];if(P.d[i]>d1)d1=P.d[i]}
  if(!P.t.length){S.sc=null;return}
  if(S.scope==='upcoming')x0=0; if(S.scope==='passed')x1=0; if(x1-x0<1)x1=x0+1;
  let y0=Math.floor(Math.log10(Math.max(d0,1))),y1=Math.ceil(Math.log10(Math.max(d1,10))); if(y1<=y0)y1=y0+1;
  S.sc={x0,x1,y0,y1};
}
const X=(t,sc)=>PAD.l+8+((t-Date.now()/1000)/3600-sc.x0)/(sc.x1-sc.x0)*(W-PAD.l-PAD.r-16);
const Y=(d,sc)=>H-PAD.b-(Math.log10(Math.max(d,1))-sc.y0)/(sc.y1-sc.y0)*(H-PAD.t-PAD.b);
function axis(g,sc){
  g.font='12px '+getComputedStyle(document.body).fontFamily;g.textBaseline='middle';
  g.strokeStyle='rgba(140,152,182,.14)';g.fillStyle='#8C98B6';g.lineWidth=1;g.textAlign='right';
  for(let k=sc.y0;k<=sc.y1;k++){const y=Math.round(Y(Math.pow(10,k),sc))+.5;g.beginPath();g.moveTo(PAD.l,y);g.lineTo(W-PAD.r,y);g.stroke();g.fillText(fmtDist(Math.pow(10,k)),PAD.l-10,y)}
  const span=sc.x1-sc.x0,steps=[1,2,3,4,6,12,24],step=steps.find(s=>span/s<=8)||24;
  g.textAlign='center';g.textBaseline='top';
  for(let h=Math.ceil(sc.x0/step)*step;h<=sc.x1;h+=step){
    const x=Math.round(PAD.l+8+(h-sc.x0)/(sc.x1-sc.x0)*(W-PAD.l-PAD.r-16))+.5;
    g.strokeStyle='rgba(140,152,182,.08)';g.beginPath();g.moveTo(x,PAD.t);g.lineTo(x,H-PAD.b);g.stroke();
    g.fillText(h===0?'now':(h>0?'+':'')+h+'h',x,H-PAD.b+10)}
  if(sc.x0<0&&sc.x1>0){const x=Math.round(PAD.l+8+(0-sc.x0)/(sc.x1-sc.x0)*(W-PAD.l-PAD.r-16))+.5;
    g.setLineDash([4,4]);g.strokeStyle='rgba(232,237,247,.45)';g.beginPath();g.moveTo(x,PAD.t);g.lineTo(x,H-PAD.b);g.stroke();g.setLineDash([])}
}
function plot(progress){
  const g=ctx,sc=S.sc,P=S.pts;g.setTransform(DPR,0,0,DPR,0,0);g.clearRect(0,0,W,H);
  $('#empty').hidden=!!(sc&&P.t.length);
  if(!sc||!P.t.length){$('#empty').textContent=S.summary&&Object.keys(S.summary.counts[S.scope==='all'?'upcoming':S.scope]||{}).length?'':'No approaches in this window yet.';$('#empty').hidden=false;return}
  axis(g,sc);
  const now=Date.now()/1000,sweep=sc.x0+progress*(sc.x1-sc.x0);
  for(const b of [0,1,2]){
    if(!S.lv.has(b)&&!(b===2&&S.lv.has(3)))continue;
    for(const i of S.buckets[b]){
      if(!S.lv.has(P.l[i]))continue;
      if((P.t[i]-now)/3600>sweep)continue;
      const x=X(P.t[i],sc),y=Y(P.d[i],sc);
      if(b===0){g.fillStyle='rgba(111,148,214,.42)';g.fillRect(x-.9,y-.9,1.8,1.8)}
      else if(b===1){g.fillStyle='rgba(255,181,71,.85)';g.beginPath();g.arc(x,y,2.5,0,6.283);g.fill()}
    }
  }
  g.shadowColor='#FF5C8A';g.shadowBlur=12;g.fillStyle='#FF5C8A';
  for(const i of S.buckets[2]){
    if(!S.lv.has(P.l[i])||(P.t[i]-now)/3600>sweep)continue;
    g.beginPath();g.arc(X(P.t[i],sc),Y(P.d[i],sc),3.8,0,6.283);g.fill()}
  g.shadowBlur=0;
}
function run(animate){
  cancelAnimationFrame(S.anim);drawFx();
  const reduce=matchMedia('(prefers-reduced-motion:reduce)').matches;
  if(!animate||reduce){plot(1);return}
  const t0=performance.now(),dur=1100;
  const step=t=>{const p=Math.min(1,(t-t0)/dur);plot(1-Math.pow(1-p,3));if(p<1)S.anim=requestAnimationFrame(step)};
  S.anim=requestAnimationFrame(step);
}
function ring(g,i,col){const sc=S.sc,P=S.pts;if(i<0||!sc)return;const x=X(P.t[i],sc),y=Y(P.d[i],sc);
  g.strokeStyle=col;g.lineWidth=1.6;g.beginPath();g.arc(x,y,9,0,6.283);g.stroke()}
function drawFx(){fctx.setTransform(DPR,0,0,DPR,0,0);fctx.clearRect(0,0,W,H);
  if(S.sel!=null){const i=S.pos.get(String(S.sel));if(i!==undefined)ring(fctx,i,'#E8EDF7')}
  ring(fctx,S.hover,'#8FD3FF')}
function hit(mx,my){
  const sc=S.sc,P=S.pts;if(!sc||!P)return -1;let best=-1,bd=1e9;
  for(let i=0;i<P.t.length;i++){if(!S.lv.has(P.l[i]))continue;
    const dx=X(P.t[i],sc)-mx,dy=Y(P.d[i],sc)-my,d=Math.sqrt(dx*dx+dy*dy)-P.l[i]*1.5;
    if(d<9&&d<bd){bd=d;best=i}}
  return best}
fx.addEventListener('mousemove',e=>{
  const r=fx.getBoundingClientRect(),mx=e.clientX-r.left,my=e.clientY-r.top,i=hit(mx,my),tip=$('#tip');
  if(i!==S.hover){S.hover=i;drawFx()}
  if(i<0){tip.style.opacity=0;return}
  const P=S.pts,ms=P.t[i]*1000,l=P.l[i];
  tip.innerHTML=`<b style="color:${COL[l]}">${LN[l]}</b> &nbsp;score ${P.s[i].toFixed(1)}<br>Miss distance ${fmtDist(P.d[i])}<br>${esc(istStr(ms))}, ${rel(ms)}`;
  tip.style.opacity=1;const tw=tip.offsetWidth;
  tip.style.left=Math.min(Math.max(8,mx+14),W-tw-8)+'px';tip.style.top=Math.max(4,my-64)+'px'});
fx.addEventListener('mouseleave',()=>{S.hover=-1;$('#tip').style.opacity=0;drawFx()});
fx.addEventListener('click',e=>{const r=fx.getBoundingClientRect(),i=hit(e.clientX-r.left,e.clientY-r.top);if(i>=0)openEvent(S.pts.id[i])});

/* ---------- drawer ---------- */
let lastFocus=null;
async function openEvent(id){
  lastFocus=document.activeElement;S.sel=id;drawFx();
  const d=$('#drawer');d.innerHTML='<div class="state">Loading details</div>';d.classList.add('on');$('#scrim').classList.add('on');d.focus();
  try{
    const r=await api('/api/event/'+encodeURIComponent(id)+'?m='+S.m),l=lvl(r.level),past=r.tca_ms<=Date.now();
    d.innerHTML=`<div class="d-top"><span class="pill" style="--c:${COL[l]}"><span class="dot"></span>${LN[l]} risk</span><button class="btn small" id="dclose" aria-label="Close details">Close</button></div>
      <h3>${esc(nm(r.p_name,r.p_norad))} and ${esc(nm(r.s_name,r.s_norad))}</h3>
      <div class="mute">${r.p_norad?'NORAD '+esc(r.p_norad):''}${r.p_norad&&r.s_norad?' and ':''}${r.s_norad?'NORAD '+esc(r.s_norad):''}</div>
      <div class="big">${r.score.toFixed(1)}</div><div class="mute">Prototype score</div>
      ${r.stored_score!=null?'<div class="sub">Stored score '+r.stored_score.toFixed(1)+' at uncertainty ×1.0</div>':''}
      ${outcomeHtml(r,past)}
      ${actsHtml(r.id,r.status)}
      <dl><dt>Closest approach</dt><dd>${esc(istStr(r.tca_ms))}<div class="sub">${esc(utcStr(r.tca_ms))}, ${rel(r.tca_ms)}${past?'<span class="tag">Passed</span>':''}</div></dd>
      <dt>Miss distance</dt><dd>${fmtDist(r.miss_m)}</dd>
      <dt>Relative speed</dt><dd>${fmtSpeed(r.vel_mps)}</dd>
      <dt>Status</dt><dd id="dst">${esc(stName(r.status))}</dd>
      <dt>Alerts</dt><dd>${r.alerts.length?r.alerts.map(a=>esc(a.title)+(a.is_read?'':' (unread)')).join('<br>'):'None for this event'}</dd>
      <dt>Event ID</dt><dd class="sub">${esc(r.ext_id)}</dd></dl>
      ${r.explanation?`<div class="expl">${esc(r.explanation)}</div>`:''}
      <p class="d-note">This is a ranking score for a predicted closest approach. It is not a collision probability.</p>`;
    $('#dclose').focus();
  }catch(e){d.innerHTML=`<div class="d-top"><span></span><button class="btn small" id="dclose">Close</button></div><div class="state">${esc(e.message)}</div>`}
}
function closeDrawer(){$('#drawer').classList.remove('on');$('#scrim').classList.remove('on');S.sel=null;drawFx();if(lastFocus&&lastFocus.focus)lastFocus.focus()}
$('#scrim').addEventListener('click',closeDrawer);
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&$('#drawer').classList.contains('on'))closeDrawer()});
$('#drawer').addEventListener('click',e=>{if(e.target.id==='dclose')closeDrawer();
  const b=e.target.closest('[data-st]');if(b)setStatus(b.dataset.id,b.dataset.st)});

/* what-if slider */
let wt;
async function loadWhatIf(){
  const my=++S.wseq;
  try{
    const [sum,pts,ev]=await Promise.all([api('/api/summary?m='+S.m),api('/api/points?scope='+S.scope+'&m='+S.m),eventsReq()]);
    if(my!==S.wseq)return;
    S.summary=sum;setPts(pts);S.events=ev;banner('');renderHero();renderChips();renderRows();run(false);
  }catch(e){if(my===S.wseq)banner(e.message)}}
$('#wi').addEventListener('input',e=>{
  S.m=Math.round(parseFloat(e.target.value)*10)/10;
  $('#wiLabel').textContent='What-if: uncertainty ×'+S.m.toFixed(1);$('#wiReset').hidden=S.m===1;
  clearTimeout(wt);wt=setTimeout(loadWhatIf,220)});
$('#wiReset').addEventListener('click',()=>{const w=$('#wi');w.value=1;w.dispatchEvent(new Event('input'))});

/* ---------- wiring ---------- */
$('#chips').addEventListener('click',e=>{const b=e.target.closest('.chip');if(!b)return;const i=+b.dataset.l;
  if(S.lv.has(i)){if(S.lv.size===1)return;S.lv.delete(i);if(i===2)S.lv.delete(3)}else{S.lv.add(i);if(i===2)S.lv.add(3)}
  S.limit=50;renderChips();plot(1);drawFx();loadEvents()});
$('#scope').addEventListener('click',e=>{const b=e.target.closest('button');if(!b||b.dataset.s===S.scope)return;S.scope=b.dataset.s;S.limit=50;
  if(S.scope==='passed'&&S.sort==='soonest'){}loadScope()});
$('#sort').addEventListener('change',e=>{S.sort=e.target.value;S.limit=50;loadEvents()});
let tm;$('#search').addEventListener('input',e=>{clearTimeout(tm);tm=setTimeout(()=>{S.q=e.target.value.trim();S.limit=50;loadEvents()},250)});
$('#moreBtn').addEventListener('click',()=>{S.limit+=50;loadEvents()});
$('#rows').addEventListener('click',e=>{const tr=e.target.closest('tr');if(tr)openEvent(tr.dataset.id)});
$('#rows').addEventListener('keydown',e=>{if(e.key==='Enter'){const tr=e.target.closest('tr');if(tr)openEvent(tr.dataset.id)}});
$('#atabs').addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;S.alertTab=b.dataset.t;
  document.querySelectorAll('#atabs button').forEach(x=>x.setAttribute('aria-pressed',x===b));loadAlerts()});
$('#alist').addEventListener('click',e=>{const rb=e.target.closest('[data-read]');
  if(rb){e.stopPropagation();markRead({id:rb.dataset.read});return}
  const al=e.target.closest('.al');if(al)openEvent(al.dataset.c)});
$('#alist').addEventListener('keydown',e=>{if(e.key==='Enter'){const al=e.target.closest('.al');if(al)openEvent(al.dataset.c)}});
$('#markAll').addEventListener('click',()=>markRead({all:true}));
$('#refresh').addEventListener('click',()=>loadAll(true));
let rt;window.addEventListener('resize',()=>{clearTimeout(rt);rt=setTimeout(()=>{fit();plot(1);drawFx()},120)});
setInterval(()=>{if(S.events&&!document.hidden){renderRows();if(S.alerts)renderAlerts()}},60000);

/* ---------- verdicts, countdown, function keys, export ---------- */
const VL={COLLISION:{t:'Collision predicted',c:'#FF5C8A'},CLOSE:{t:'Very close pass',c:'#FFB547'},NEAR:{t:'Near miss',c:'#FFD98A'},CLEAR:{t:'Clear pass',c:'#5FD0A0'}};
const vtag=r=>{if(r.tca_ms>Date.now())return '<span class="sub">Not yet</span>';const v=VL[r.verdict]||VL.CLEAR;
  return `<span class="vt" style="--c:${v.c}">${r.verdict==='COLLISION'?'Yes, predicted':'No, '+v.t.toLowerCase()}</span>`};
function outcomeHtml(r,past){const v=VL[r.verdict]||VL.CLEAR,d=r.miss_m/S.m,hit=r.verdict==='COLLISION',
  hb=S.summary&&S.summary.thr?S.summary.thr.hbr:20;
  const ans=past?(hit?'Yes. The model predicted an impact.':'No. They did not collide.'):(hit?'Impact predicted if nothing changes.':'No collision predicted.');
  return `<div class="out" style="--c:${v.c}"><h4>${past?'Did it collide?':'Will it collide?'}</h4><div class="ans">${ans}</div>
  <div>${v.t}: ${fmtDist(d)} apart${S.m!==1?' at uncertainty ×'+S.m.toFixed(1):''}. An impact needs under ${fmtDist(hb)}, the assumed combined size of the two objects.</div>
  <div class="d-note">${past?'Based on the last stored prediction. This app has no tracking data from after the event.':'A prediction from stored data, not a certainty.'}</div></div>`}
function renderStrip(){const s=S.summary;if(!s||!s.outcomes){$('#strip').innerHTML='';return}
  const o=s.outcomes,t=s.triage||{},x=s.stats;
  const c=[[fmtN(o.passed),'approaches already passed'],[fmtN(o.collision),'predicted collisions among them'],[fmtN(o.close+o.near),'near misses among them'],
    [x.dmin==null?'none':fmtDist(x.dmin/S.m),'closest predicted pass'],[x.vmax==null?'none':fmtSpeed(x.vmax),'fastest closing speed'],
    [fmtN(t.ESCALATED||0)+' / '+fmtN(t.MONITORING||0),'escalated / monitoring']];
  $('#strip').innerHTML=c.map(a=>`<div class="stat"><b>${a[0]}</b><span>${a[1]}</span></div>`).join('')}
function tickNext(){const n=S.summary&&S.summary.next,el=$('#next');if(!n){el.hidden=true;return}
  const left=n.tca_ms-Date.now(),sec=Math.max(0,Math.floor(left/1000)),p=v=>String(v).padStart(2,'0'),hi=n.level==='HIGH'||n.level==='CRITICAL';
  el.hidden=false;el.dataset.id=n.id;
  el.innerHTML=`<span>Next ${hi?'high-risk ':''}approach in</span><span class="cd">${p(Math.floor(sec/3600))}:${p(Math.floor(sec%3600/60))}:${p(sec%60)}</span><span class="mute">${esc(nm(n.p_name,n.p_norad))} with ${esc(nm(n.s_name,n.s_norad))}</span>`;
  if(left<-2000&&S.nid!==n.id){S.nid=n.id;loadAlerts()}}
setInterval(()=>{if(!document.hidden)tickNext()},1000);
$('#next').addEventListener('click',e=>{const id=e.currentTarget.dataset.id;if(id)openEvent(id)});
$('#next').addEventListener('keydown',e=>{if(e.key==='Enter')e.currentTarget.click()});
function exportCsv(){location.href='/api/export?'+new URLSearchParams({scope:S.scope,levels:levelsParam(),q:S.q,sort:S.sort,m:S.m,st:S.st});toast('Exporting this view to CSV')}
let tt;function toast(m){const t=$('#toast');t.textContent=m;t.classList.add('on');clearTimeout(tt);tt=setTimeout(()=>t.classList.remove('on'),2400)}
function setScope(x){if(x!==S.scope){S.scope=x;S.limit=50;loadScope()}}
function decide(st){const id=$('#drawer').classList.contains('on')?S.sel:S.cur;
  if(id==null){toast('Open an approach first: click a row, or press j / k to move');return}setStatus(id,st)}
function toggleHelp(){$('#help').hidden=!$('#help').hidden}
const FK=[['F1','Help',toggleHelp,'Show this list'],['F2','Upcoming',()=>setScope('upcoming'),'Upcoming approaches'],['F3','Passed',()=>setScope('passed'),'Passed approaches and their outcomes'],
  ['F4','All',()=>setScope('all'),'Upcoming and passed together'],['F5','Refresh',()=>loadAll(true),'Reload everything'],['F6','Search',()=>$('#search').focus(),'Search by name or NORAD ID'],
  ['F7','Export',exportCsv,'Download this view as CSV'],['F8','Escalate',()=>decide('ESCALATED'),'Escalate the open or selected approach'],
  ['F9','Monitor',()=>decide('MONITORING'),'Monitor it'],['F10','Dismiss',()=>decide('DISMISSED'),'Dismiss it']];
$('#fkeys').innerHTML=FK.map(f=>`<button data-k="${f[0]}"><kbd>${f[0]}</kbd>${f[1]}</button>`).join('');
$('#fkeys').addEventListener('click',e=>{const b=e.target.closest('button');if(b)FK.find(f=>f[0]===b.dataset.k)[2]()});
$('#help').innerHTML=`<div class="hbox" role="dialog" aria-label="Keyboard shortcuts"><h2>Keyboard shortcuts</h2><dl>${FK.map(f=>`<dt><kbd>${f[0]}</kbd></dt><dd>${f[3]}</dd>`).join('')}<dt><kbd>j</kbd> <kbd>k</kbd></dt><dd>Next or previous row</dd><dt><kbd>/</kbd></dt><dd>Search</dd><dt><kbd>Esc</kbd></dt><dd>Close a panel</dd></dl><p class="sub">F8 to F10 act on the open approach, or on the row you last selected.</p></div>`;
$('#help').addEventListener('click',e=>{if(e.target.id==='help')toggleHelp()});
$('#helpBtn').addEventListener('click',toggleHelp);$('#exportBtn').addEventListener('click',exportCsv);
$('#dec').addEventListener('change',e=>{S.st=e.target.value;S.limit=50;loadEvents()});
$('#rows').addEventListener('focusin',e=>{const tr=e.target.closest('tr');if(tr)S.cur=tr.dataset.id});
document.addEventListener('keydown',e=>{
  const f=FK.find(x=>x[0]===e.key);if(f){e.preventDefault();f[2]();return}
  if(e.key==='Escape'&&!$('#help').hidden){toggleHelp();return}
  if(e.target.matches('input,select,textarea')||e.ctrlKey||e.metaKey||e.altKey)return;
  if(e.key==='?'){toggleHelp()}
  else if(e.key==='/'){e.preventDefault();$('#search').focus()}
  else if(e.key==='j'||e.key==='k'){const t=[...document.querySelectorAll('#rows tr')];if(!t.length)return;
    let i=t.indexOf(document.activeElement);i=Math.max(0,Math.min(t.length-1,i+(e.key==='j'?1:-1)));t[i].focus()}});

fit();loadAll(true);
</script>
</body>
</html>
'''

if __name__ == "__main__":
    main()
