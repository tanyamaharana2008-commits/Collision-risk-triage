"""
Stage 5 - Prototype Risk Scoring

Turns the Stage 4 closest-approach results into a simple, project-defined
LOW / MEDIUM / HIGH score from 0 to 100:

    Miss distance       up to 60 points   (0 km = 60, falls to 0 at 2 km)
    Time to TCA         up to 25 points   (sooner = more points)
    Relative velocity   up to 15 points   (faster = more points)

    70-100 = HIGH     40-69 = MEDIUM     below 40 = LOW

!!! THIS IS NOT A COLLISION PROBABILITY !!!
It is a student-project prioritisation score. Real conjunction assessment
needs uncertainty (covariance) data and validated methods that public orbital
data does not provide. The point values and thresholds below are prototype
choices that you can change in the SETTINGS section.

Input : stage4_refined.csv  (written by stage4_miss_distance.py --from-csv ...)
Output: printed results + stage5_risk_scores.csv
This stage reads a file only: it needs no database password and writes nothing
to PostgreSQL (that is Stage 6).

Usage:
    python stage5_risk_scoring.py
"""

import argparse
import csv
from datetime import datetime, timezone, timedelta

# ----------------------------------------------------------------------
# PROTOTYPE SETTINGS - change these to tune the score
# ----------------------------------------------------------------------
MAX_POINTS_DISTANCE = 60
MAX_POINTS_TIME = 25
MAX_POINTS_VELOCITY = 15

DIST_ZERO_KM = 2.0        # miss distance at which distance points reach 0
                          # (0 km = full points, falls in a straight line).
                          # A pair that misses by more than this is always LOW,
                          # however soon or fast it is.
TIME_FULL_MIN = 60.0      # TCA this soon (or sooner) = full time points
TIME_ZERO_MIN = 1440.0    # TCA this far away (24 h) or more = 0 time points
SPEED_FULL_KM_S = 10.0    # relative speed at or above this = full velocity points

HIGH_THRESHOLD = 70
MEDIUM_THRESHOLD = 40
# ----------------------------------------------------------------------

IST = timezone(timedelta(hours=5, minutes=30))


def clamp01(x):
    return max(0.0, min(1.0, x))


def distance_points(miss_km):
    return MAX_POINTS_DISTANCE * clamp01(1.0 - miss_km / DIST_ZERO_KM)


def time_points(minutes_to_tca):
    frac = (TIME_ZERO_MIN - minutes_to_tca) / (TIME_ZERO_MIN - TIME_FULL_MIN)
    return MAX_POINTS_TIME * clamp01(frac)


def velocity_points(speed_km_s):
    return MAX_POINTS_VELOCITY * clamp01(speed_km_s / SPEED_FULL_KM_S)


def risk_level(score, dist_pts=1.0):
    if dist_pts <= 0:                  # missed by more than DIST_ZERO_KM
        return "LOW"
    if score >= HIGH_THRESHOLD:
        return "HIGH"
    if score >= MEDIUM_THRESHOLD:
        return "MEDIUM"
    return "LOW"


def pair_note(name_a, name_b):
    """Informational only - does not change the score."""
    a = name_a.upper().startswith("STARLINK")
    b = name_b.upper().startswith("STARLINK")
    if a and b:
        return "both Starlink (operator-managed)"
    if a or b:
        return "one Starlink"
    return ""


def parse_tca(text):
    return datetime.strptime(text.replace(" UTC", "").strip(),
                             "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)


def main():
    ap = argparse.ArgumentParser(description="Stage 5 - prototype risk scoring")
    ap.add_argument("--input", default="stage4_refined.csv", help="Stage 4 results file")
    ap.add_argument("--out", default="stage5_risk_scores.csv", help="scored results file")
    ap.add_argument("--top", type=int, default=10, help="events to print (default 10)")
    ap.add_argument("--as-of", default=None,
                    help='score as of this UTC time, "YYYY-MM-DD HH:MM:SS" (default: now)')
    args = ap.parse_args()

    print("=" * 70)
    print("STAGE 5 - PROTOTYPE RISK SCORING")
    print("=" * 70)

    try:
        f = open(args.input, newline="", encoding="utf-8")
    except FileNotFoundError:
        print(f"\nCannot find {args.input}.")
        print("Run Stage 4 first:")
        print("    python stage4_miss_distance.py --from-csv conjunction_candidates.csv")
        return

    if args.as_of:
        now = datetime.strptime(args.as_of, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    else:
        now = datetime.now(timezone.utc)

    scored, past, objects = [], 0, set()
    with f:
        for row in csv.DictReader(f):
            try:
                tca = parse_tca(row["tca_utc"])
                miss = float(row["miss_distance_km"])
                speed = float(row["relative_speed_km_s"])
                na, nb = int(row["norad_a"]), int(row["norad_b"])
            except (KeyError, ValueError):
                continue
            objects.update((na, nb))
            minutes = (tca - now).total_seconds() / 60.0
            if minutes < 0:                       # closest approach already happened
                past += 1
                continue
            pd_, pt_, pv_ = distance_points(miss), time_points(minutes), velocity_points(speed)
            total = pd_ + pt_ + pv_
            scored.append({
                "na": na, "name_a": row["name_a"], "nb": nb, "name_b": row["name_b"],
                "tca": tca, "minutes": minutes, "miss": miss, "speed": speed,
                "pd": pd_, "pt": pt_, "pv": pv_, "score": total,
                "level": risk_level(total, pd_),
                "note": pair_note(row["name_a"], row["name_b"]),
            })

    print(f"\nObjects loaded: {len(objects):,}")
    print(f"Scored as of  : {now:%Y-%m-%d %H:%M:%S} UTC")
    print(f"Close approaches scored: {len(scored):,}"
          f"   (skipped {past:,} whose TCA has already passed)")
    if not scored:
        print("\nNothing left to score. Re-run Stage 3 and Stage 4 for a fresh time window.")
        return

    scored.sort(key=lambda e: (-e["score"], e["miss"]))

    counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for e in scored:
        counts[e["level"]] += 1

    print("\n" + "-" * 70)
    print("RISK RESULTS")
    print("-" * 70)
    print(f"  HIGH   : {counts['HIGH']:,}")
    print(f"  MEDIUM : {counts['MEDIUM']:,}")
    print(f"  LOW    : {counts['LOW']:,}")
    print("-" * 70)
    print(f"\nTop {min(args.top, len(scored))} by prototype score:\n")

    for e in scored[:args.top]:
        print(f"{e['na']} ({e['name_a']}) <-> {e['nb']} ({e['name_b']})")
        print(f"Miss distance     : {e['miss']:.3f} km")
        print(f"TCA               : {e['tca']:%Y-%m-%d %H:%M:%S} UTC "
              f"({e['tca'].astimezone(IST):%H:%M} IST)")
        print(f"Time to TCA       : {e['minutes']:.0f} min")
        print(f"Relative velocity : {e['speed']:.2f} km/s")
        print(f"Points            : distance {e['pd']:.1f}/{MAX_POINTS_DISTANCE} + "
              f"time {e['pt']:.1f}/{MAX_POINTS_TIME} + "
              f"velocity {e['pv']:.1f}/{MAX_POINTS_VELOCITY}")
        print(f"TOTAL SCORE       : {e['score']:.1f}/100")
        print(f"RISK LEVEL        : {e['level']}")
        if e["note"]:
            print(f"Note              : {e['note']}")
        print()

    with open(args.out, "w", newline="", encoding="utf-8") as out:
        w = csv.writer(out)
        w.writerow(["rank", "norad_a", "name_a", "norad_b", "name_b", "tca_utc", "tca_ist",
                    "time_to_tca_min", "miss_distance_km", "relative_speed_km_s",
                    "points_distance", "points_time", "points_velocity",
                    "total_score", "risk_level", "note"])
        for rank, e in enumerate(scored, 1):
            w.writerow([rank, e["na"], e["name_a"], e["nb"], e["name_b"],
                        f"{e['tca']:%Y-%m-%d %H:%M:%S}",
                        f"{e['tca'].astimezone(IST):%Y-%m-%d %H:%M:%S}",
                        f"{e['minutes']:.1f}", f"{e['miss']:.4f}", f"{e['speed']:.3f}",
                        f"{e['pd']:.1f}", f"{e['pt']:.1f}", f"{e['pv']:.1f}",
                        f"{e['score']:.1f}", e["level"], e["note"]])

    print("=" * 70)
    print(f"Saved all scored events to {args.out}")
    print("=" * 70)
    print("\nThis is a PROTOTYPE prioritisation score, NOT a collision probability.")
    print("Public orbital data has errors of roughly a kilometre or more, so these")
    print("levels rank pairs for further study; they do not mean a collision is likely.")


if __name__ == "__main__":
    main()
