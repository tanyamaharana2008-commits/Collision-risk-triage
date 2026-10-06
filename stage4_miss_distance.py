"""
Stage 4 - Miss Distance + TCA (prototype)

Finds the smallest separation between pairs of objects (miss distance) and
the time it happens (TCA, Time of Closest Approach).

Two modes
---------
1) GUIDE TEST (default) - exactly the first prototype from the Stage 4 guide:
   a few objects, a short window, 1-minute steps, every unique pair compared.
       python stage4_miss_distance.py

2) REFINE STAGE 3 RESULTS - takes the candidate list written by
   screen_conjunctions.py and finds the true closest approach of the closest
   candidates, to a precision of about a millisecond.
       python stage4_miss_distance.py --from-csv conjunction_candidates.csv

IMPORTANT: this is a PROTOTYPE. A small miss distance is NOT a collision
probability. Proper collision-risk analysis needs uncertainty (covariance)
data and validated methods, which public orbital data does not provide.
Nothing is written to the database in this stage.
"""

import argparse
import csv
import math
import time
from datetime import datetime, timezone, timedelta

import numpy as np
from sgp4.api import Satrec, WGS72, jday

# ----------------------------------------------------------------------
# DATABASE SETTINGS - same values you used in your other scripts
# ----------------------------------------------------------------------
DB_HOST = "localhost"
DB_PORT = 5432
DB_NAME = "space_collision_db"
DB_USER = "postgres"
DB_PASSWORD = "space"   # <-- put your real password here
# ----------------------------------------------------------------------

# ----------------------------------------------------------------------
# PROTOTYPE SETTINGS (guide test mode)
# ----------------------------------------------------------------------
MAX_OBJECTS = 20
WINDOW_MINUTES = 30
STEP_MINUTES = 1
# ----------------------------------------------------------------------

DEG2RAD = math.pi / 180.0
REV_PER_DAY_TO_RAD_PER_MIN = 2.0 * math.pi / 1440.0
SGP4_EPOCH_JD = 2433281.5


# ----------------------------------------------------------------------
# Database + satellite construction (same method as Stages 2 and 3)
# ----------------------------------------------------------------------
def connect():
    import psycopg2
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=DB_USER, password=DB_PASSWORD,
    )


def find_mean_motion_column(cur):
    cur.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'orbital_elements' AND column_name LIKE 'mean_motion%'
        ORDER BY ordinal_position LIMIT 1
        """
    )
    row = cur.fetchone()
    if row is None:
        raise RuntimeError("No mean_motion column found in orbital_elements.")
    return row[0]


def fetch_rows(cur, mm_col, norad_ids=None):
    """Newest orbital record per object (optionally only some NORAD IDs)."""
    where = "WHERE o.norad_id = ANY(%s)" if norad_ids is not None else ""
    sql = f"""
        SELECT DISTINCT ON (o.object_id)
               o.norad_id, o.object_name, e.epoch, e.bstar, e.inclination_deg,
               e.raan_deg, e.eccentricity, e.arg_perigee_deg,
               e.mean_anomaly_deg, e.{mm_col}
        FROM orbital_elements e
        JOIN space_objects o ON o.object_id = e.object_id
        {where}
        ORDER BY o.object_id, e.epoch DESC
    """
    if norad_ids is not None:
        cur.execute(sql, (list(norad_ids),))
    else:
        cur.execute(sql)
    return cur.fetchall()


def to_jd_fr(dt):
    dt = dt.astimezone(timezone.utc)
    return jday(dt.year, dt.month, dt.day, dt.hour, dt.minute,
                dt.second + dt.microsecond / 1e6)


def satellite_from_row(row):
    norad, _name, epoch, bstar, incl, raan, ecc, argp, ma, mm = row
    jd, fr = to_jd_fr(epoch)
    sat = Satrec()
    sat.sgp4init(
        WGS72, "i", int(norad), (jd - SGP4_EPOCH_JD) + fr,
        float(bstar or 0.0), 0.0, 0.0, float(ecc),
        float(argp) * DEG2RAD, float(incl) * DEG2RAD, float(ma) * DEG2RAD,
        float(mm) * REV_PER_DAY_TO_RAD_PER_MIN, float(raan) * DEG2RAD,
    )
    return sat


# ----------------------------------------------------------------------
# Closest-approach refinement
# ----------------------------------------------------------------------
def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def closest_approach(sat_a, sat_b, base_dt, iters=8):
    """
    Refine the time of closest approach near base_dt.

    SGP4 gives velocity as well as position, so the time where the
    separation is smallest (relative position perpendicular to relative
    velocity) can be found by a few Newton steps. The result is never worse
    than the starting point.

    Returns (seconds after base_dt, miss distance km, relative speed km/s)
    or None if SGP4 fails.
    """
    jd, fr = to_jd_fr(base_dt)

    def state(t):
        ea, ra, va = sat_a.sgp4(jd, fr + t / 86400.0)
        eb, rb, vb = sat_b.sgp4(jd, fr + t / 86400.0)
        if ea or eb:
            return None
        r = (rb[0] - ra[0], rb[1] - ra[1], rb[2] - ra[2])
        v = (vb[0] - va[0], vb[1] - va[1], vb[2] - va[2])
        return r, v

    t = 0.0
    s = state(t)
    if s is None:
        return None
    best_t, best_d = 0.0, math.sqrt(_dot(s[0], s[0]))
    for _ in range(iters):
        r, v = s
        vv = _dot(v, v)
        if vv < 1e-10:
            break
        dt = max(-30.0, min(30.0, -_dot(r, v) / vv))
        t += dt
        s = state(t)
        if s is None:
            break
        d = math.sqrt(_dot(s[0], s[0]))
        if d < best_d:
            best_d, best_t = d, t
        if abs(dt) < 1e-3:
            break
    s = state(best_t)
    return best_t, best_d, math.sqrt(_dot(s[1], s[1]))


def fmt_time(dt):
    return f"{dt:%Y-%m-%d %H:%M:%S}.{dt.microsecond // 1000:03d} UTC"


# ----------------------------------------------------------------------
# Mode 1 - the guide's first prototype test
# ----------------------------------------------------------------------
def run_guide_test(args, cur, mm_col):
    print("=" * 70)
    print("STAGE 4 - MISS DISTANCE + TCA")
    print("=" * 70)

    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    cutoff = now - timedelta(days=30)
    rows = [r for r in fetch_rows(cur, mm_col)
            if r[2] is not None and r[2].astimezone(timezone.utc) >= cutoff]
    rows.sort(key=lambda r: r[0])                      # by NORAD ID, reproducible

    steps = int(round(args.minutes / args.step)) + 1
    offsets = np.arange(steps) * args.step * 60.0
    jd0, fr0 = to_jd_fr(now)
    jd_arr = np.full(steps, jd0)
    fr_arr = fr0 + offsets / 86400.0

    usable, positions = [], []
    for row in rows:
        if len(usable) >= args.objects:
            break
        try:
            sat = satellite_from_row(row)
            err, pos, _vel = sat.sgp4_array(jd_arr, fr_arr)
        except Exception:
            continue
        if err.any() or not np.isfinite(pos).all():
            continue
        usable.append((row, sat))
        positions.append(pos)

    print(f"\nLoaded usable satellites: {len(usable)}")
    print(f"Common start time: {now:%Y-%m-%d %H:%M:%S} UTC")
    print(f"Window: {args.minutes:g} minutes")
    print(f"Step: {args.step:g} minute(s)")

    if len(usable) < 2:
        print("\nNeed at least 2 usable satellites - no pairs to compare.")
        return

    R = np.stack(positions)                            # (objects, steps, 3)
    n = len(usable)
    ia, ib = np.triu_indices(n, 1)                     # each pair once, never (i, i)
    best = np.full(len(ia), np.inf)
    best_step = np.zeros(len(ia), dtype=int)
    for t in range(steps):
        P = R[:, t, :]
        d = np.sqrt(((P[ia] - P[ib]) ** 2).sum(axis=1))
        better = d < best
        best = np.where(better, d, best)
        best_step[better] = t

    print(f"Unique pairs compared: {len(ia):,}")
    print("\n" + "-" * 70)
    print("CLOSEST PAIRS")
    print("-" * 70)
    for k in np.argsort(best)[:args.top]:
        (ra, sa), (rb, sb) = usable[ia[k]], usable[ib[k]]
        tca = now + timedelta(seconds=float(offsets[best_step[k]]))
        print(f"\n{ra[0]} ({ra[1]}) <-> {rb[0]} ({rb[1]})")
        print(f"TCA: {tca:%Y-%m-%d %H:%M:%S} UTC | Miss distance: {best[k]:.2f} km"
              f"   (sampled every {args.step:g} min)")
        ref = closest_approach(sa, sb, tca)
        if ref is not None:
            t_off, d_ref, speed = ref
            print(f"Refined: TCA {fmt_time(tca + timedelta(seconds=t_off))} | "
                  f"Miss distance: {d_ref:.2f} km | relative speed {speed:.2f} km/s")

    print("\n" + "=" * 70)
    print("STAGE 4 TEST COMPLETED")
    print("=" * 70)
    print("Note: objects are chosen by NORAD ID, so they are usually far apart.")
    print("A sampled minimum can miss the true closest approach when objects move")
    print("at several km/s; the 'Refined' line shows the more accurate value.")
    print("This is a prototype result, not a collision probability.")


# ----------------------------------------------------------------------
# Mode 2 - refine the candidates produced by Stage 3
# ----------------------------------------------------------------------
def run_refinement(args, cur, mm_col):
    print("=" * 78)
    print("STAGE 4 - MISS DISTANCE + TCA (refining Stage 3 candidates)")
    print("=" * 78)
    t_start = time.time()

    print(f"1/3 Reading {args.from_csv} (keeping candidates with estimated miss "
          f"<= {args.max_miss:g} km)...")
    kept, total = [], 0
    with open(args.from_csv, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader)                                   # header
        for row in reader:
            total += 1
            try:
                d = float(row[6])
            except (ValueError, IndexError):
                continue
            if d <= args.max_miss:
                kept.append((d, row))
                if len(kept) > 2 * args.max_events:
                    kept.sort(key=lambda x: x[0])
                    del kept[args.max_events:]
    kept.sort(key=lambda x: x[0])
    kept = kept[:args.max_events]
    print(f"    {total:,} candidates in file, {len(kept):,} selected for refinement.")
    if not kept:
        print("Nothing to refine. Try a larger --max-miss.")
        return

    print("2/3 Loading orbital data for the objects involved...")
    ids = {int(r[0]) for _, r in kept} | {int(r[2]) for _, r in kept}
    sats = {}
    for row in fetch_rows(cur, mm_col, ids):
        try:
            sats[int(row[0])] = (row[1], satellite_from_row(row))
        except Exception:
            continue
    print(f"    {len(sats):,} satellite models built.")

    print("3/3 Refining closest approaches...")
    ks = np.arange(-75, 76, dtype=float)               # 1-second scan around Stage 3's TCA
    results, skipped = [], 0
    for n, (est, row) in enumerate(kept, 1):
        na, nb = int(row[0]), int(row[2])
        if na not in sats or nb not in sats:
            skipped += 1
            continue
        sa, sb = sats[na][1], sats[nb][1]
        try:
            tca0 = datetime.strptime(row[4], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            jd, fr = to_jd_fr(tca0)
            jd_arr = np.full(len(ks), jd)
            fr_arr = fr + ks / 86400.0
            ea, ra, _ = sa.sgp4_array(jd_arr, fr_arr)
            eb, rb, _ = sb.sgp4_array(jd_arr, fr_arr)
            ok = (ea == 0) & (eb == 0)
            if not ok.any():
                skipped += 1
                continue
            dist = np.where(ok, np.sqrt(((rb - ra) ** 2).sum(axis=1)), np.inf)
            k0 = float(ks[int(np.argmin(dist))])
            start = tca0 + timedelta(seconds=k0)
            ref = closest_approach(sa, sb, start)
        except Exception:
            skipped += 1
            continue
        if ref is None:
            skipped += 1
            continue
        t_off, d_ref, speed = ref
        results.append((d_ref, start + timedelta(seconds=t_off), speed, na, nb, est))
        if n % 5000 == 0:
            print(f"    {n:,} / {len(kept):,} refined", flush=True)

    results.sort(key=lambda r: r[0])

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["rank", "norad_a", "name_a", "norad_b", "name_b", "tca_utc",
                    "miss_distance_km", "relative_speed_km_s", "stage3_estimate_km"])
        for rank, (d, tca, speed, na, nb, est) in enumerate(results, 1):
            w.writerow([rank, na, sats[na][0], nb, sats[nb][0], fmt_time(tca),
                        f"{d:.4f}", f"{speed:.3f}", f"{est:.2f}"])

    print("\n" + "=" * 78)
    print(f"Refined {len(results):,} close approaches ({skipped} skipped).")
    for limit in (1, 2, 5, 10):
        print(f"  miss distance <= {limit:>2} km : {sum(1 for r in results if r[0] <= limit):,}")
    print("=" * 78)
    print(f"\nCLOSEST {min(args.top, len(results))} (of {len(results):,})\n")
    for d, tca, speed, na, nb, _est in results[:args.top]:
        print(f"{na} ({sats[na][0][:20]}) <-> {nb} ({sats[nb][0][:20]})")
        print(f"TCA: {fmt_time(tca)} | Miss distance: {d:.3f} km | "
              f"relative speed {speed:.2f} km/s\n")
    print(f"Saved all refined results to {args.out}")
    print(f"Done in {time.time() - t_start:.0f} seconds.")
    print("\nThese are prototype results, not collision probabilities. Public orbital")
    print("data has errors of roughly a kilometre or more, so very small miss")
    print("distances here do not mean a collision is likely.")


# ----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Stage 4 - miss distance + TCA")
    ap.add_argument("--objects", type=int, default=MAX_OBJECTS,
                    help=f"guide test: number of objects (default {MAX_OBJECTS})")
    ap.add_argument("--minutes", type=float, default=WINDOW_MINUTES,
                    help=f"guide test: window in minutes (default {WINDOW_MINUTES})")
    ap.add_argument("--step", type=float, default=STEP_MINUTES,
                    help=f"guide test: step in minutes (default {STEP_MINUTES})")
    ap.add_argument("--top", type=int, default=10, help="pairs to print (default 10)")
    ap.add_argument("--from-csv", default=None,
                    help="refine candidates from Stage 3 (conjunction_candidates.csv)")
    ap.add_argument("--max-miss", type=float, default=5.0,
                    help="refine only candidates with estimated miss <= this km (default 5)")
    ap.add_argument("--max-events", type=int, default=100000,
                    help="refine at most this many of the closest candidates (default 100000)")
    ap.add_argument("--out", default="stage4_refined.csv", help="refined results CSV")
    args = ap.parse_args()

    if DB_PASSWORD == "YOUR_POSTGRES_PASSWORD":
        print("Please edit stage4_miss_distance.py and set DB_PASSWORD first.")
        return

    conn = connect()
    cur = conn.cursor()
    mm_col = find_mean_motion_column(cur)
    try:
        if args.from_csv:
            run_refinement(args, cur, mm_col)
        else:
            run_guide_test(args, cur, mm_col)
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
