"""
Stage 6 - store the scored close approaches in PostgreSQL.

Reads   : stage5_risk_scores.csv  (scores, levels, point breakdown)
          stage4_refined.csv      (exact TCA to the millisecond, exact miss distance)
Writes  : conjunction_events, risk_assessments, alerts

This is a PROTOTYPE prioritisation score, NOT a collision probability.
collision_probability is left empty on purpose.

Safe to re-run: every event it creates has an external_event_id starting with
"PROTO-". On each run it first deletes its own earlier rows (the database
cascades the delete to risk_assessments and alerts), then inserts fresh ones.
Rows from anything else are never touched.

Usage:
    python stage6_store_results.py              (real run)
    python stage6_store_results.py --dry-run    (reads the CSVs only, no database)
"""

import argparse
import csv
import sys
from datetime import datetime, timezone, timedelta

# ----------------------------- SETTINGS -----------------------------
DB_HOST = "localhost"
DB_PORT = 5432
DB_NAME = "space_collision_db"
DB_USER = "postgres"
DB_PASSWORD = "space"   # <-- same password as in main.py

STAGE4_CSV = "stage4_refined.csv"
STAGE5_CSV = "stage5_risk_scores.csv"

PROTO_PREFIX = "PROTO-"
SCREENING_RADIUS_M = 50_000.0   # Stage 3 screening radius (50 km), stored in metres
MODEL_NAME = "prototype-heuristic-score"
MODEL_VERSION = "stage5-v1"
ALERT_TYPE = "HIGH_RISK_CLOSE_APPROACH"
TCA_MATCH_TOLERANCE_S = 2.0     # how close Stage 4 and Stage 5 TCA must be to count as the same event
# --------------------------------------------------------------------

DISCLAIMER = ("Prototype prioritisation score, not a collision probability. "
              "Public orbital data is only accurate to roughly a kilometre or more.")


def parse_stage5_time(text):
    # e.g. '2026-10-06 04:28:03'
    return datetime.strptime(text.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def parse_stage4_time(text):
    # e.g. '2026-10-06 20:10:55.994 UTC'
    text = text.strip().replace(" UTC", "")
    fmt = "%Y-%m-%d %H:%M:%S.%f" if "." in text else "%Y-%m-%d %H:%M:%S"
    return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)


def load_stage4():
    """Index Stage 4 rows by (norad_a, norad_b) so Stage 5 rows can be matched to them."""
    index = {}
    with open(STAGE4_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (int(row["norad_a"]), int(row["norad_b"]))
            index.setdefault(key, []).append({
                "tca": parse_stage4_time(row["tca_utc"]),
                "miss_km": float(row["miss_distance_km"]),
                "speed_km_s": float(row["relative_speed_km_s"]),
            })
    return index


def load_events():
    stage4 = load_stage4()
    events, exact_matches = [], 0
    with open(STAGE5_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            a, b = int(row["norad_a"]), int(row["norad_b"])
            tca = parse_stage5_time(row["tca_utc"])
            miss_km = float(row["miss_distance_km"])
            speed = float(row["relative_speed_km_s"])

            # Prefer Stage 4's exact values (millisecond TCA, unrounded distance).
            best = None
            for cand in stage4.get((a, b), []):
                diff = abs((cand["tca"] - tca).total_seconds())
                if diff <= TCA_MATCH_TOLERANCE_S and (best is None or diff < best[0]):
                    best = (diff, cand)
            if best:
                tca, miss_km, speed = best[1]["tca"], best[1]["miss_km"], best[1]["speed_km_s"]
                exact_matches += 1

            events.append({
                "norad_a": a, "name_a": row["name_a"],
                "norad_b": b, "name_b": row["name_b"],
                "tca": tca,
                "miss_m": miss_km * 1000.0,
                "speed_mps": speed * 1000.0,
                "time_to_tca_s": float(row["time_to_tca_min"]) * 60.0,
                "pts_distance": float(row["points_distance"]),
                "pts_time": float(row["points_time"]),
                "pts_velocity": float(row["points_velocity"]),
                "score": float(row["total_score"]),
                "level": row["risk_level"].strip().upper(),
                "note": (row.get("note") or "").strip(),
                "external_id": f"{PROTO_PREFIX}{a}-{b}-{tca:%Y%m%dT%H%M%S}",
            })
    return events, exact_matches


def explanation(e):
    text = (f"Prototype score {e['score']:.1f}/100 = distance {e['pts_distance']:.1f}/60 "
            f"+ time {e['pts_time']:.1f}/25 + velocity {e['pts_velocity']:.1f}/15. ")
    if e["note"]:
        text += f"Note: {e['note']}. "
    return text + DISCLAIMER


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="read the CSVs only, do not touch the database")
    args = ap.parse_args()

    print("=" * 70)
    print("STAGE 6 - STORE RESULTS IN POSTGRESQL")
    print("=" * 70)

    try:
        events, exact = load_events()
    except FileNotFoundError as err:
        sys.exit(f"ERROR: {err}. Run Stage 4 and Stage 5 first, from this same folder.")
    print(f"Read {len(events)} scored events from {STAGE5_CSV}")
    print(f"  {exact} matched to Stage 4 for exact TCA and distance")

    # Duplicate guard: same pair and same second would give the same external id.
    unique = {}
    for e in events:
        unique.setdefault(e["external_id"], e)
    if len(unique) != len(events):
        print(f"  {len(events) - len(unique)} duplicate rows ignored")
    events = list(unique.values())

    levels = {}
    for e in events:
        levels[e["level"]] = levels.get(e["level"], 0) + 1
    print("  by level:", ", ".join(f"{k}={v}" for k, v in sorted(levels.items())))

    if args.dry_run:
        print("\nDRY RUN - nothing written. First event as it would be stored:")
        e = events[0]
        print(f"  {e['external_id']}")
        print(f"  miss_distance_m = {e['miss_m']:.1f}, relative_velocity_mps = {e['speed_mps']:.1f}")
        print(f"  tca = {e['tca'].isoformat()}")
        print(f"  risk_score = {e['score']}, risk_level = {e['level']}")
        print(f"  explanation = {explanation(e)}")
        return

    if DB_PASSWORD == "YOUR_POSTGRES_PASSWORD":
        sys.exit("ERROR: open this file in Notepad and set DB_PASSWORD first.")

    import psycopg2
    from psycopg2.extras import execute_values

    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
                            user=DB_USER, password=DB_PASSWORD)
    try:
        with conn, conn.cursor() as cur:
            # 1. NORAD id -> space_objects.object_id
            norads = sorted({e["norad_a"] for e in events} | {e["norad_b"] for e in events})
            cur.execute("SELECT norad_id, object_id FROM space_objects WHERE norad_id = ANY(%s)", (norads,))
            obj = {int(n): oid for n, oid in cur.fetchall()}

            rows, skipped_missing, skipped_same = [], 0, 0
            for e in events:
                pa, pb = obj.get(e["norad_a"]), obj.get(e["norad_b"])
                if pa is None or pb is None:
                    skipped_missing += 1
                elif pa == pb:
                    skipped_same += 1
                else:
                    rows.append((e, pa, pb))
            print(f"\nMatched objects in space_objects. Skipped: {skipped_missing} (object not found), "
                  f"{skipped_same} (same object)")

            # 2. Remove this script's earlier rows (cascades to risk_assessments and alerts)
            cur.execute("DELETE FROM conjunction_events WHERE external_event_id LIKE %s", (PROTO_PREFIX + "%",))
            print(f"Removed {cur.rowcount} earlier prototype events (and their scores and alerts)")

            # 3. conjunction_events
            ev_values = [(pa, pb, e["tca"], e["miss_m"], e["speed_mps"], SCREENING_RADIUS_M, e["external_id"])
                         for e, pa, pb in rows]
            ids = execute_values(
                cur,
                """INSERT INTO conjunction_events
                       (primary_object_id, secondary_object_id, tca, miss_distance_m,
                        relative_velocity_mps, screening_radius_m, external_event_id)
                   VALUES %s RETURNING external_event_id, conjunction_id""",
                ev_values, page_size=1000, fetch=True)
            conj_id = {ext: cid for ext, cid in ids}

            # 4. risk_assessments
            rk_values = [(conj_id[e["external_id"]], e["score"], e["level"], e["time_to_tca_s"],
                          e["pts_distance"], e["pts_velocity"], MODEL_NAME, MODEL_VERSION, explanation(e))
                         for e, _, _ in rows]
            ids = execute_values(
                cur,
                """INSERT INTO risk_assessments
                       (conjunction_id, risk_score, risk_level, time_to_tca_seconds,
                        miss_distance_score, relative_velocity_score,
                        model_name, model_version, explanation)
                   VALUES %s RETURNING conjunction_id, risk_id""",
                rk_values, page_size=1000, fetch=True)
            risk_id = {cid: rid for cid, rid in ids}

            # 5. alerts, one per HIGH event
            al_values = []
            for e, _, _ in rows:
                if e["level"] == "HIGH":
                    cid = conj_id[e["external_id"]]
                    title = (f"{e['name_a']} <-> {e['name_b']}: "
                             f"{e['miss_m'] / 1000:.3f} km at {e['tca']:%Y-%m-%d %H:%M} UTC")
                    msg = (f"Prototype score {e['score']:.1f}/100. Relative speed "
                           f"{e['speed_mps'] / 1000:.2f} km/s. " + DISCLAIMER)
                    al_values.append((cid, risk_id[cid], ALERT_TYPE, "HIGH", title, msg))
            if al_values:
                execute_values(
                    cur,
                    """INSERT INTO alerts (conjunction_id, risk_id, alert_type, severity, title, message)
                       VALUES %s""",
                    al_values, page_size=1000)

            print(f"\nInserted: {len(ev_values)} conjunction_events, "
                  f"{len(rk_values)} risk_assessments, {len(al_values)} alerts")

            # 6. Read the totals back from the database as a check
            cur.execute("""SELECT
                  (SELECT COUNT(*) FROM conjunction_events WHERE external_event_id LIKE %s),
                  (SELECT COUNT(*) FROM risk_assessments r JOIN conjunction_events c USING (conjunction_id)
                       WHERE c.external_event_id LIKE %s),
                  (SELECT COUNT(*) FROM alerts a JOIN conjunction_events c USING (conjunction_id)
                       WHERE c.external_event_id LIKE %s)""",
                        (PROTO_PREFIX + "%",) * 3)
            ce, ra, al = cur.fetchone()
            print(f"In the database now: {ce} conjunction_events, {ra} risk_assessments, {al} alerts")
    except Exception as err:
        print(f"\nERROR - nothing was saved (the whole run was rolled back):\n{err}")
        sys.exit(1)
    finally:
        conn.close()

    print("\n" + "=" * 70)
    print("STAGE 6 COMPLETED SUCCESSFULLY")
    print("=" * 70)
    print(DISCLAIMER)


if __name__ == "__main__":
    main()
