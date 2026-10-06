"""
Stage 7 - dashboard and alerts.

Reads conjunction_events, risk_assessments, alerts and space_objects from
PostgreSQL (read-only, nothing is changed) and writes one file, dashboard.html.
Double-click dashboard.html to view it. Re-run this script to refresh it.

Usage:
    python stage7_dashboard.py            (builds and opens dashboard.html)
    python stage7_dashboard.py --no-open  (builds only)

This is a PROTOTYPE prioritisation score, NOT a collision probability.
"""

import argparse
import html
import os
import sys
import webbrowser
from datetime import datetime, timezone, timedelta

# ----------------------------- SETTINGS -----------------------------
DB_HOST = "localhost"
DB_PORT = 5432
DB_NAME = "space_collision_db"
DB_USER = "postgres"
DB_PASSWORD = "space"   # <-- same password as in main.py

OUTPUT_FILE = "dashboard.html"
TOP_EVENTS = 50      # rows in the "top HIGH events" table
SHOW_ALERTS = 100    # rows in the alert list
# --------------------------------------------------------------------

IST = timezone(timedelta(hours=5, minutes=30))
LEVELS = ["HIGH", "MEDIUM", "LOW"]
DISCLAIMER = ("Prototype prioritisation score, not a collision probability. Public orbital data "
              "(CelesTrak) is only accurate to roughly a kilometre or more, and many satellites "
              "(such as Starlink) manoeuvre automatically. Treat these as \"closest predicted "
              "approaches\" ranked for further study, not predicted collisions.")


# ------------------------------ DATA --------------------------------
def fetch_data():
    import psycopg2
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
                            user=DB_USER, password=DB_PASSWORD)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT risk_level, COUNT(*) FROM risk_assessments GROUP BY risk_level")
            levels = {k: int(v) for k, v in cur.fetchall()}

            cur.execute("SELECT COUNT(*) FROM conjunction_events")
            total_events = int(cur.fetchone()[0])

            cur.execute("SELECT COUNT(*), COUNT(*) FILTER (WHERE NOT is_read) FROM alerts")
            total_alerts, unread_alerts = (int(x) for x in cur.fetchone())

            cur.execute("SELECT MIN(tca), MAX(tca) FROM conjunction_events")
            tca_min, tca_max = cur.fetchone()

            cur.execute("SELECT MAX(assessed_at) FROM risk_assessments")
            assessed = cur.fetchone()[0]

            cur.execute("""
                SELECT o1.object_name, o1.norad_id, o2.object_name, o2.norad_id,
                       c.miss_distance_m, c.relative_velocity_mps, c.tca,
                       r.risk_score, r.risk_level
                FROM risk_assessments r
                JOIN conjunction_events c ON c.conjunction_id = r.conjunction_id
                JOIN space_objects o1 ON o1.object_id = c.primary_object_id
                JOIN space_objects o2 ON o2.object_id = c.secondary_object_id
                WHERE r.risk_level = 'HIGH'
                ORDER BY r.risk_score DESC, c.miss_distance_m ASC
                LIMIT %s""", (TOP_EVENTS,))
            top = cur.fetchall()

            cur.execute("""
                SELECT a.alert_id, a.severity, a.title, a.is_read, a.created_at
                FROM alerts a
                ORDER BY a.created_at DESC, a.alert_id DESC
                LIMIT %s""", (SHOW_ALERTS,))
            alerts = cur.fetchall()
    finally:
        conn.close()
    return {"levels": levels, "total_events": total_events, "total_alerts": total_alerts,
            "unread_alerts": unread_alerts, "tca_min": tca_min, "tca_max": tca_max,
            "assessed": assessed, "top": top, "alerts": alerts}


# ----------------------------- RENDER -------------------------------
def esc(x):
    return html.escape(str(x))


def fmt_utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def fmt_ist(dt):
    return dt.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")


def bar_chart(levels):
    total = sum(levels.get(k, 0) for k in LEVELS) or 1
    biggest = max([levels.get(k, 0) for k in LEVELS] + [1])
    rows = []
    for i, k in enumerate(LEVELS):
        n = levels.get(k, 0)
        width = max(2, round(560 * n / biggest)) if n else 0
        y = 10 + i * 44
        rows.append(
            f'<text x="0" y="{y + 20}" class="lbl">{k}</text>'
            f'<rect x="80" y="{y}" width="{width}" height="28" rx="4" class="bar {k.lower()}"/>'
            f'<text x="{80 + width + 8}" y="{y + 20}" class="val">{n:,} ({100 * n / total:.1f}%)</text>')
    return ('<svg viewBox="0 0 820 150" role="img" aria-label="Events by risk level" '
            'class="chart">' + "".join(rows) + "</svg>")


def render(d, generated):
    now = generated
    lv = d["levels"]
    cards = [
        ("Close approaches stored", f"{d['total_events']:,}", ""),
        ("HIGH", f"{lv.get('HIGH', 0):,}", "high"),
        ("MEDIUM", f"{lv.get('MEDIUM', 0):,}", "medium"),
        ("LOW", f"{lv.get('LOW', 0):,}", "low"),
        ("Unread alerts", f"{d['unread_alerts']:,}", "high"),
    ]
    cards_html = "".join(
        f'<div class="card {cls}"><div class="n">{n}</div><div class="t">{esc(t)}</div></div>'
        for t, n, cls in cards)

    window = ""
    if d["tca_min"] and d["tca_max"]:
        window = (f"Events cover closest approaches from {fmt_utc(d['tca_min'])} UTC "
                  f"to {fmt_utc(d['tca_max'])} UTC.")
    scored = f" Scored at {fmt_utc(d['assessed'])} UTC." if d["assessed"] else ""

    top_rows = []
    for i, (na, ida, nb, idb, miss_m, vel, tca, score, level) in enumerate(d["top"], 1):
        status = "Passed" if tca < now else "Upcoming"
        both_sl = ("STARLINK" in str(na).upper()) and ("STARLINK" in str(nb).upper())
        note = "both Starlink" if both_sl else ""
        miss_txt = f"{miss_m:,.0f} m" if miss_m < 1000 else f"{miss_m / 1000:.3f} km"
        top_rows.append(
            f"<tr><td>{i}</td><td>{esc(na)} <span class='id'>#{esc(ida)}</span><br>"
            f"{esc(nb)} <span class='id'>#{esc(idb)}</span></td>"
            f"<td class='num'>{miss_txt}</td><td class='num'>{vel / 1000:.2f} km/s</td>"
            f"<td>{fmt_utc(tca)}<br><span class='id'>{fmt_ist(tca)} IST</span></td>"
            f"<td class='num'>{score:.1f}</td>"
            f"<td><span class='pill {status.lower()}'>{status}</span> "
            f"<span class='id'>{esc(note)}</span></td></tr>")
    top_html = "".join(top_rows) or "<tr><td colspan='7'>No HIGH events stored.</td></tr>"

    alert_rows = []
    for aid, sev, title, is_read, created in d["alerts"]:
        alert_rows.append(
            f"<tr><td>{aid}</td><td><span class='pill {esc(sev).lower()}'>{esc(sev)}</span></td>"
            f"<td>{esc(title)}</td><td>{'Read' if is_read else '<b>Unread</b>'}</td>"
            f"<td>{fmt_utc(created)}</td></tr>")
    alerts_html = "".join(alert_rows) or "<tr><td colspan='5'>No alerts stored.</td></tr>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Space Collision Risk Triage - Dashboard</title>
<style>
:root {{ --bg:#f6f7f9; --panel:#fff; --text:#1c2330; --muted:#5d6878; --line:#e1e5eb;
        --high:#c62828; --medium:#e08a00; --low:#2e7d52; --accent:#1f4e9c; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#10141b; --panel:#1a202b; --text:#e7ebf1; --muted:#9aa6b8; --line:#2b3442;
          --high:#ef6b6b; --medium:#f0b04a; --low:#5cc08c; --accent:#7fa7e8; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
       background:var(--bg); color:var(--text); }}
main {{ max-width:1100px; margin:0 auto; padding:24px 16px 48px; }}
h1 {{ font-size:24px; margin:0 0 4px; }}
h2 {{ font-size:18px; margin:32px 0 10px; }}
.sub {{ color:var(--muted); margin:0 0 16px; }}
.notice {{ background:var(--panel); border:1px solid var(--line); border-left:4px solid var(--medium);
          border-radius:6px; padding:12px 14px; margin:16px 0; font-size:14px; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; }}
.card {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; }}
.card .n {{ font-size:28px; font-weight:700; }}
.card .t {{ color:var(--muted); font-size:13px; }}
.card.high .n {{ color:var(--high); }} .card.medium .n {{ color:var(--medium); }}
.card.low .n {{ color:var(--low); }}
.panel {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; }}
.chart {{ width:100%; max-width:820px; height:auto; }}
.chart .lbl {{ fill:var(--text); font-size:14px; font-weight:600; }}
.chart .val {{ fill:var(--muted); font-size:13px; }}
.bar.high {{ fill:var(--high); }} .bar.medium {{ fill:var(--medium); }} .bar.low {{ fill:var(--low); }}
.scroll {{ overflow-x:auto; }}
table {{ width:100%; border-collapse:collapse; background:var(--panel); font-size:14px; }}
th,td {{ text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); vertical-align:top; }}
th {{ color:var(--muted); font-weight:600; font-size:13px; position:sticky; top:0; background:var(--panel); }}
td.num {{ text-align:right; white-space:nowrap; }}
.id {{ color:var(--muted); font-size:12px; }}
.pill {{ display:inline-block; padding:1px 8px; border-radius:10px; font-size:12px; font-weight:600;
        border:1px solid var(--line); }}
.pill.high {{ color:var(--high); border-color:var(--high); }}
.pill.medium {{ color:var(--medium); border-color:var(--medium); }}
.pill.low,.pill.upcoming {{ color:var(--low); border-color:var(--low); }}
.pill.passed {{ color:var(--muted); }}
footer {{ color:var(--muted); font-size:13px; margin-top:32px; }}
</style>
</head>
<body>
<main>
<h1>Space Collision Risk Triage</h1>
<p class="sub">Real CelesTrak orbital data, propagated with SGP4 and screened for close approaches.
Dashboard built {fmt_utc(now)} UTC ({fmt_ist(now)} IST). {esc(window)}{esc(scored)}</p>

<div class="notice"><b>Read this first.</b> {esc(DISCLAIMER)}</div>

<div class="cards">{cards_html}</div>

<h2>Events by prototype risk level</h2>
<div class="panel">{bar_chart(lv)}
<p class="id">Bars share one linear scale, so the small HIGH group is deliberately hard to see:
most close approaches are low priority.</p></div>

<h2>Top {len(d['top'])} HIGH events (highest prototype score first)</h2>
<div class="scroll"><table>
<thead><tr><th>#</th><th>Objects</th><th>Miss distance</th><th>Relative speed</th>
<th>Closest approach</th><th>Score /100</th><th>Status</th></tr></thead>
<tbody>{top_html}</tbody></table></div>
<p class="id">Status compares the closest-approach time with the moment this dashboard was built.
After the 24-hour window passes, re-run Stage 3, 4, 5 and 6 to refresh the data.</p>

<h2>Latest alerts ({len(d['alerts'])} of {d['total_alerts']:,} shown)</h2>
<div class="scroll"><table>
<thead><tr><th>ID</th><th>Severity</th><th>Alert</th><th>Read?</th><th>Created (UTC)</th></tr></thead>
<tbody>{alerts_html}</tbody></table></div>

<footer>Score = distance (up to 60) + time to closest approach (up to 25) + relative velocity (up to 15).
HIGH is 70 or above, MEDIUM 40-69, LOW below 40. No collision probability is calculated.</footer>
</main>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-open", action="store_true", help="build the file but do not open it")
    args = ap.parse_args()

    print("=" * 70)
    print("STAGE 7 - DASHBOARD")
    print("=" * 70)

    if DB_PASSWORD == "YOUR_POSTGRES_PASSWORD":
        sys.exit("ERROR: open this file in Notepad and set DB_PASSWORD first.")

    try:
        data = fetch_data()
    except Exception as err:
        sys.exit(f"ERROR reading the database:\n{err}")

    page = render(data, datetime.now(timezone.utc))
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(page)

    path = os.path.abspath(OUTPUT_FILE)
    lv = data["levels"]
    print(f"Events: {data['total_events']:,}  "
          f"(HIGH {lv.get('HIGH', 0):,}, MEDIUM {lv.get('MEDIUM', 0):,}, LOW {lv.get('LOW', 0):,})")
    print(f"Alerts: {data['total_alerts']:,} ({data['unread_alerts']:,} unread)")
    print(f"\nSaved: {path}")
    if not args.no_open:
        webbrowser.open("file:///" + path.replace("\\", "/"))
    print("\nSTAGE 7 COMPLETED SUCCESSFULLY")
    print(DISCLAIMER)


if __name__ == "__main__":
    main()
