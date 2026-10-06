"""
Stage 2 - Orbit Propagation (test script)

Reads the orbital elements already stored in PostgreSQL (OMM/GP format),
builds an SGP4 model from them, and prints position + velocity of one
satellite. It does NOT change the database and does NOT touch main.py.

Usage (from the project folder):
    python propagate_orbits.py            -> tests CALSPHERE 1 (NORAD 900)
    python propagate_orbits.py 1361       -> tests another NORAD ID
"""

import sys
import math
from datetime import datetime, timezone, timedelta

from sgp4.api import Satrec, WGS72, jday

# ----------------------------------------------------------------------
# DATABASE SETTINGS - same values you used in main.py
# ----------------------------------------------------------------------
DB_HOST = "localhost"
DB_PORT = 5432
DB_NAME = "space_collision_db"
DB_USER = "postgres"
DB_PASSWORD = "space"   # <-- put your real password here
# ----------------------------------------------------------------------

DEFAULT_NORAD = 900
EARTH_RADIUS_KM = 6378.137
DEG2RAD = math.pi / 180.0
REV_PER_DAY_TO_RAD_PER_MIN = 2.0 * math.pi / 1440.0
SGP4_EPOCH_JD = 2433281.5          # SGP4 counts days from 1949-12-31 00:00 UTC


def connect():
    import psycopg2
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=DB_USER, password=DB_PASSWORD,
    )


def find_mean_motion_column(cur):
    """The mean-motion column name was cut off on screen, so look it up."""
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
    """Step 2.4 - turn stored orbital elements into an SGP4 model."""
    epoch_utc = epoch.astimezone(timezone.utc)
    jd, fr = jday(
        epoch_utc.year, epoch_utc.month, epoch_utc.day,
        epoch_utc.hour, epoch_utc.minute,
        epoch_utc.second + epoch_utc.microsecond / 1e6,
    )
    epoch_days = (jd - SGP4_EPOCH_JD) + fr

    sat = Satrec()
    sat.sgp4init(
        WGS72,                                   # gravity model used by TLE/GP data
        "i",                                     # improved operation mode
        int(norad_id),
        epoch_days,
        float(bstar),
        0.0,                                     # ndot  (not used by SGP4 itself)
        0.0,                                     # nddot (not used by SGP4 itself)
        float(ecc),
        float(argp_deg) * DEG2RAD,
        float(incl_deg) * DEG2RAD,
        float(mean_anom_deg) * DEG2RAD,
        float(mean_motion_rev_day) * REV_PER_DAY_TO_RAD_PER_MIN,
        float(raan_deg) * DEG2RAD,
    )
    return sat


def propagate(sat, when_utc):
    """Steps 2.5/2.6 - position (km) and velocity (km/s) at a given UTC time."""
    jd, fr = jday(
        when_utc.year, when_utc.month, when_utc.day,
        when_utc.hour, when_utc.minute,
        when_utc.second + when_utc.microsecond / 1e6,
    )
    return sat.sgp4(jd, fr)          # (error_code, position, velocity)


def norm(vec):
    return math.sqrt(sum(c * c for c in vec))


def main():
    if DB_PASSWORD == "YOUR_POSTGRES_PASSWORD":
        print("Please edit propagate_orbits.py and set DB_PASSWORD first.")
        return

    norad_id = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_NORAD

    conn = connect()
    cur = conn.cursor()
    mm_col = find_mean_motion_column(cur)

    # Step 2.3 - read the newest orbital record for one object
    cur.execute(
        f"""
        SELECT o.norad_id, o.object_name, e.epoch, e.bstar,
               e.inclination_deg, e.raan_deg, e.eccentricity,
               e.arg_perigee_deg, e.mean_anomaly_deg, e.{mm_col}
        FROM space_objects o
        JOIN orbital_elements e ON e.object_id = o.object_id
        WHERE o.norad_id = %s
        ORDER BY e.epoch DESC
        LIMIT 1
        """,
        (norad_id,),
    )
    row = cur.fetchone()
    if row is None:
        print(f"NORAD {norad_id} not found in the database.")
        print("Try another ID, for example:  python propagate_orbits.py 1361")
        conn.close()
        return

    norad, name, epoch, bstar, incl, raan, ecc, argp, ma, mm = row
    print("=" * 74)
    print(f"Object      : {name} (NORAD {norad})")
    print(f"Epoch       : {epoch}")
    print(f"Inclination : {incl:.4f} deg   Eccentricity: {ecc:.7f}   "
          f"Mean motion: {mm:.6f} rev/day")
    print("=" * 74)

    sat = build_satellite(norad, epoch, bstar or 0.0, incl, raan, ecc, argp, ma, mm)

    # Step 2.5/2.6 - propagate: at the epoch, now, and a few steps ahead
    now = datetime.now(timezone.utc)
    epoch_utc = epoch.astimezone(timezone.utc)
    times = [("Epoch", epoch_utc), ("Now", now)]
    for minutes in (5, 10, 30, 90):
        times.append((f"Now + {minutes} min", now + timedelta(minutes=minutes)))

    print(f"{'When':<16}{'Position X, Y, Z (km)':<44}{'Speed km/s':<12}{'Alt km'}")
    all_ok = True
    for label, t in times:
        error, pos, vel = propagate(sat, t)
        if error != 0:
            print(f"{label:<16}SGP4 error code {error}")
            all_ok = False
            continue
        pos_text = f"{pos[0]:10.1f}, {pos[1]:10.1f}, {pos[2]:10.1f}"
        altitude = norm(pos) - EARTH_RADIUS_KM   # approximate (spherical Earth)
        print(f"{label:<16}{pos_text:<44}{norm(vel):<12.3f}{altitude:.1f}")

    age_hours = (now - epoch_utc).total_seconds() / 3600
    print(f"\nData age: {age_hours:.1f} hours")

    # Step 2.7 - sanity checks
    print("\nSanity check:")
    if all_ok:
        print("  OK  SGP4 returned valid positions (no error codes).")
    else:
        print("  !!  Some SGP4 calls failed - see error codes above.")

    # Extra: health check on a sample of the catalogue (newest row per object)
    cur.execute(
        f"""
        SELECT DISTINCT ON (o.object_id)
               o.norad_id, e.epoch, e.bstar, e.inclination_deg, e.raan_deg,
               e.eccentricity, e.arg_perigee_deg, e.mean_anomaly_deg, e.{mm_col}
        FROM orbital_elements e
        JOIN space_objects o ON o.object_id = e.object_id
        ORDER BY o.object_id, e.epoch DESC
        LIMIT 1000
        """
    )
    sample = cur.fetchall()
    ok = 0
    for r in sample:
        try:
            s = build_satellite(r[0], r[1], r[2] or 0.0, *r[3:])
            err, _, _ = propagate(s, r[1].astimezone(timezone.utc))
            if err == 0:
                ok += 1
        except Exception:
            pass
    print(f"  Sample test: {ok} of {len(sample)} objects propagated without error.")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
