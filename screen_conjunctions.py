"""
Stage 3 - Conjunction Screening (prototype)

Reads the orbital elements stored in PostgreSQL, propagates every object with
SGP4 over a time window, and lists pairs of objects that come within the
screening radius (default 50 km) of each other.

This is a PROTOTYPE screening stage. It is not an operational collision-safety
tool, and the 50 km radius is a prototype choice, not a validated threshold.
It is READ-ONLY: nothing is written to the database. Candidates are printed
and saved to conjunction_candidates.csv for Stage 4 (miss distance).

How it avoids comparing every pair blindly:
  1. All objects are propagated in bulk (numpy).
  2. At each time step a KD-tree finds only the pairs that are near each other.
  3. For each near pair, the closest approach between two time steps is
     estimated from relative position + velocity (straight-line motion).
  4. Pairs that stay close but barely move relative to each other (relative
     speed below --min-rel-speed, e.g. satellites flying in formation or
     docked together) are counted separately - they are not "conjunctions".

Usage (from the project folder):
    python screen_conjunctions.py                  -> all objects, next 24 hours
    python screen_conjunctions.py --hours 3        -> quick test, next 3 hours
    python screen_conjunctions.py --limit 3000     -> random subset of 3000 objects
"""

import argparse
import csv
import math
import time
from datetime import datetime, timezone, timedelta

import numpy as np
from scipy.spatial import cKDTree
from sgp4.api import Satrec, SatrecArray, WGS72, jday

# ----------------------------------------------------------------------
# DATABASE SETTINGS - same values you used in main.py
# ----------------------------------------------------------------------
DB_HOST = "localhost"
DB_PORT = 5432
DB_NAME = "space_collision_db"
DB_USER = "postgres"
DB_PASSWORD = "space"   # <-- put your real password here
# ----------------------------------------------------------------------

MU_EARTH = 398600.4418                     # km^3 / s^2
DEG2RAD = math.pi / 180.0
REV_PER_DAY_TO_RAD_PER_MIN = 2.0 * math.pi / 1440.0
SGP4_EPOCH_JD = 2433281.5                  # SGP4 counts days from 1949-12-31
IST = timezone(timedelta(hours=5, minutes=30))


# ----------------------------------------------------------------------
# Database + satellite construction (same method as Stage 2)
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


def build_satellite(norad_id, epoch, bstar, incl_deg, raan_deg, ecc,
                    argp_deg, mean_anom_deg, mean_motion_rev_day):
    epoch_utc = epoch.astimezone(timezone.utc)
    jd, fr = jday(
        epoch_utc.year, epoch_utc.month, epoch_utc.day,
        epoch_utc.hour, epoch_utc.minute,
        epoch_utc.second + epoch_utc.microsecond / 1e6,
    )
    sat = Satrec()
    sat.sgp4init(
        WGS72, "i", int(norad_id), (jd - SGP4_EPOCH_JD) + fr,
        float(bstar), 0.0, 0.0, float(ecc),
        float(argp_deg) * DEG2RAD, float(incl_deg) * DEG2RAD,
        float(mean_anom_deg) * DEG2RAD,
        float(mean_motion_rev_day) * REV_PER_DAY_TO_RAD_PER_MIN,
        float(raan_deg) * DEG2RAD,
    )
    return sat


def perigee_speed(mean_motion_rev_day, ecc):
    """Fastest speed an object on this orbit can reach (km/s)."""
    n = float(mean_motion_rev_day) * 2.0 * math.pi / 86400.0
    a = (MU_EARTH / (n * n)) ** (1.0 / 3.0)
    e = min(max(float(ecc), 0.0), 0.99)
    return math.sqrt(MU_EARTH / a * (1.0 + e) / (1.0 - e))


# ----------------------------------------------------------------------
# Core screening
# ----------------------------------------------------------------------
def run_screening(sats, start_utc, hours, step_s, radius_km, min_rel_speed,
                  vrel_max, chunk=30, progress=None):
    """
    Returns raw candidate records (arrays) plus counters.

    A pair whose true closest approach is <= radius_km must be within
    radius_km + vrel_max*step/2 at the nearest time sample, so the KD-tree
    search uses that larger "coarse" radius and each hit is then refined.
    """
    n = len(sats)
    sat_array = SatrecArray(sats)
    total_s = hours * 3600.0
    n_steps = int(total_s // step_s) + 1
    half = step_s / 2.0
    coarse_radius = radius_km + vrel_max * half

    jd0, fr0 = jday(
        start_utc.year, start_utc.month, start_utc.day,
        start_utc.hour, start_utc.minute,
        start_utc.second + start_utc.microsecond / 1e6,
    )

    out_a, out_b, out_d, out_t, out_s, coorbit_keys = [], [], [], [], [], []
    pair_checks = 0

    for c0 in range(0, n_steps, chunk):
        ks = np.arange(c0, min(c0 + chunk, n_steps))
        fr = fr0 + ks * step_s / 86400.0
        jd = np.full(len(ks), jd0)
        err, pos_all, vel_all = sat_array.sgp4(jd, fr)

        for j, k in enumerate(ks):
            ok = (err[:, j] == 0) & np.isfinite(pos_all[:, j, :]).all(axis=1)
            idx = np.flatnonzero(ok)
            if len(idx) < 2:
                continue
            pos = pos_all[idx, j, :]
            vel = vel_all[idx, j, :]

            # Only pairs near each other; each pair appears once, never (i, i)
            pairs = cKDTree(pos).query_pairs(coarse_radius, output_type="ndarray")
            if len(pairs) == 0:
                continue
            pair_checks += len(pairs)
            ia, ib = pairs[:, 0], pairs[:, 1]

            rel_r = pos[ib] - pos[ia]
            rel_v = vel[ib] - vel[ia]
            vv = np.einsum("ij,ij->i", rel_v, rel_v)
            rv = np.einsum("ij,ij->i", rel_r, rel_v)
            with np.errstate(divide="ignore", invalid="ignore"):
                tau = np.where(vv > 1e-12, -rv / vv, 0.0)
            # stay inside this sample's window AND inside the requested time window
            tau = np.clip(tau, max(-half, -k * step_s),
                          min(half, total_s - k * step_s))
            closest = rel_r + rel_v * tau[:, None]
            dmin = np.sqrt(np.einsum("ij,ij->i", closest, closest))
            speed = np.sqrt(vv)

            hit = dmin <= radius_km
            if not hit.any():
                continue
            fast = hit & (speed >= min_rel_speed)
            slow = hit & ~fast
            if slow.any():
                coorbit_keys.append(
                    idx[ia[slow]].astype(np.int64) * n + idx[ib[slow]])
            if fast.any():
                out_a.append(idx[ia[fast]])
                out_b.append(idx[ib[fast]])
                out_d.append(dmin[fast])
                out_t.append(k * step_s + tau[fast])
                out_s.append(speed[fast])

        if progress:
            progress(min(c0 + chunk, n_steps) / n_steps)

    def cat(parts, dtype):
        return np.concatenate(parts) if parts else np.array([], dtype=dtype)

    return {
        "a": cat(out_a, np.int64), "b": cat(out_b, np.int64),
        "d": cat(out_d, float), "t": cat(out_t, float), "s": cat(out_s, float),
        "coorbit_pairs": int(np.unique(np.concatenate(coorbit_keys)).size)
        if coorbit_keys else 0,
        "pair_checks": pair_checks, "n_steps": n_steps, "n": n,
        "coarse_radius": coarse_radius,
    }


def merge_events(a, b, d, t, s, step_s):
    """Neighbouring time samples see the same encounter twice - keep the closest."""
    if len(a) == 0:
        return []
    order = np.lexsort((t, b, a))
    a, b, d, t, s = a[order], b[order], d[order], t[order], s[order]
    events = []
    cur = [int(a[0]), int(b[0]), float(d[0]), float(t[0]), float(s[0])]
    last_t = float(t[0])
    for i in range(1, len(a)):
        same = (a[i] == cur[0] and b[i] == cur[1]
                and t[i] - last_t <= 1.5 * step_s)
        if same:
            if d[i] < cur[2]:
                cur[2], cur[3], cur[4] = float(d[i]), float(t[i]), float(s[i])
            last_t = float(t[i])
        else:
            events.append(tuple(cur))
            cur = [int(a[i]), int(b[i]), float(d[i]), float(t[i]), float(s[i])]
            last_t = float(t[i])
    events.append(tuple(cur))
    events.sort(key=lambda e: e[2])
    return events


# ----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Stage 3 conjunction screening")
    ap.add_argument("--hours", type=float, default=24.0, help="time window (default 24)")
    ap.add_argument("--step", type=float, default=60.0, help="time step in seconds (default 60)")
    ap.add_argument("--radius", type=float, default=50.0, help="screening radius km (default 50)")
    ap.add_argument("--min-rel-speed", type=float, default=0.1,
                    help="ignore pairs moving slower than this relative to each other, km/s (default 0.1)")
    ap.add_argument("--max-age-days", type=float, default=30.0,
                    help="skip objects whose orbital data is older than this (default 30)")
    ap.add_argument("--limit", type=int, default=None, help="use a random subset of N objects")
    ap.add_argument("--top", type=int, default=25, help="rows to print (default 25)")
    ap.add_argument("--csv", default="conjunction_candidates.csv", help="output CSV file")
    args = ap.parse_args()

    if DB_PASSWORD == "YOUR_POSTGRES_PASSWORD":
        print("Please edit screen_conjunctions.py and set DB_PASSWORD first.")
        return

    t_start = time.time()
    now = datetime.now(timezone.utc)

    print("1/4 Loading orbital data from PostgreSQL...")
    conn = connect()
    cur = conn.cursor()
    mm = find_mean_motion_column(cur)
    cur.execute(
        f"""
        SELECT DISTINCT ON (o.object_id)
               o.norad_id, o.object_name, e.epoch, e.bstar, e.inclination_deg,
               e.raan_deg, e.eccentricity, e.arg_perigee_deg,
               e.mean_anomaly_deg, e.{mm}
        FROM orbital_elements e
        JOIN space_objects o ON o.object_id = e.object_id
        ORDER BY o.object_id, e.epoch DESC
        """
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    loaded = len(rows)

    cutoff = now - timedelta(days=args.max_age_days)
    rows = [r for r in rows if r[2] is not None and r[2].astimezone(timezone.utc) >= cutoff]
    too_old = loaded - len(rows)
    if args.limit and args.limit < len(rows):
        rng = np.random.default_rng(42)
        keep = sorted(rng.choice(len(rows), size=args.limit, replace=False))
        rows = [rows[i] for i in keep]
    print(f"    {loaded} objects loaded, {too_old} skipped (data older than "
          f"{args.max_age_days:g} days), {len(rows)} used.")

    print("2/4 Building SGP4 models...")
    sats, meta, speeds = [], [], []
    for r in rows:
        try:
            sats.append(build_satellite(r[0], r[2], r[3] or 0.0, *r[4:]))
            meta.append((r[0], r[1]))
            speeds.append(perigee_speed(r[9], r[6]))
        except Exception:
            continue
    top2 = sorted(speeds)[-2:]
    vrel_max = (sum(top2) if len(top2) == 2 else 16.0) * 1.02
    print(f"    {len(sats)} models built. Max possible relative speed: {vrel_max:.1f} km/s")

    print(f"3/4 Screening {args.hours:g} h from now, {args.step:g}-second steps, "
          f"radius {args.radius:g} km...")

    def progress(frac):
        print(f"\r    progress: {int(frac * 100):3d}%", end="", flush=True)

    res = run_screening(sats, now, args.hours, args.step, args.radius,
                        args.min_rel_speed, vrel_max, progress=progress)
    print()

    events = merge_events(res["a"], res["b"], res["d"], res["t"], res["s"], args.step)

    print("4/4 Results")
    n = res["n"]
    brute = n * (n - 1) // 2 * res["n_steps"]
    print("=" * 96)
    print(f"Objects screened          : {n}")
    print(f"Time window (UTC)         : {now:%Y-%m-%d %H:%M} -> "
          f"{now + timedelta(hours=args.hours):%Y-%m-%d %H:%M}")
    print(f"Pair checks (brute force) : {brute:,}")
    print(f"Pair checks (KD-tree)     : {res['pair_checks']:,}")
    print(f"Candidate close approaches (<= {args.radius:g} km): {len(events)}")
    print(f"Close pairs ignored as formation/docked (rel. speed < {args.min_rel_speed:g} km/s): "
          f"{res['coorbit_pairs']}")
    print("=" * 96)

    if events:
        print(f"{'#':>3}  {'Object A':<26}{'Object B':<26}{'TCA (UTC)':<18}"
              f"{'Miss km':>8}{'Rel km/s':>10}")
        for rank, (ia, ib, d, t, s) in enumerate(events[:args.top], 1):
            tca = now + timedelta(seconds=t)
            la = f"{meta[ia][1][:17]} ({meta[ia][0]})"
            lb = f"{meta[ib][1][:17]} ({meta[ib][0]})"
            print(f"{rank:>3}  {la:<26}{lb:<26}{tca:%m-%d %H:%M:%S}   "
                  f"{d:>8.1f}{s:>10.2f}")
        if len(events) > args.top:
            print(f"... and {len(events) - args.top} more (see {args.csv})")

        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["norad_a", "name_a", "norad_b", "name_b", "tca_utc", "tca_ist",
                        "approx_miss_distance_km", "relative_speed_km_s"])
            for ia, ib, d, t, s in events:
                tca = now + timedelta(seconds=t)
                w.writerow([meta[ia][0], meta[ia][1], meta[ib][0], meta[ib][1],
                            f"{tca:%Y-%m-%d %H:%M:%S}",
                            f"{tca.astimezone(IST):%Y-%m-%d %H:%M:%S}",
                            f"{d:.2f}", f"{s:.3f}"])
        print(f"\nSaved all candidates to {args.csv}")
    else:
        print("No candidates found in this window.")

    print(f"\nDone in {time.time() - t_start:.0f} seconds.")
    print("Miss distances here are rough estimates for screening only;")
    print("Stage 4 computes the closest approach properly.")


if __name__ == "__main__":
    main()
