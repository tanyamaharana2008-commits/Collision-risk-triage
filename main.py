"""
SPACE COLLISION RISK TRIAGE
Single-file setup + CelesTrak importer

Place this file in the SAME folder as:
    space_collision_risk_triage.sql

Then edit the PostgreSQL settings below and run:
    python main.py

What this program does:
1. Connects to PostgreSQL's default "postgres" database.
2. Creates the project database if it does not exist.
3. Runs your existing SQL schema file.
4. Removes the SQL file's synthetic TEST objects.
5. Downloads real current GP orbital data from CelesTrak.
6. Stores the real objects and orbital elements in PostgreSQL.
7. Prints a small verification report.

This is the DATA INGESTION stage.
It does NOT yet calculate collision risk or conjunctions.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
import psycopg2
from psycopg2 import sql


# ============================================================
# 1. EDIT THESE SETTINGS
# ============================================================

DB_HOST = "localhost"
DB_PORT = 5432
DB_USER = "postgres"
DB_PASSWORD = "space"

# This is the database that the program will create/use.
DB_NAME = "space_collision_db"

# Your SQL file must be in the same folder as main.py.
SQL_FILE = Path(__file__).with_name("space_collision_risk_triage.sql")

# CelesTrak GP JSON endpoint.
# ACTIVE is a large dataset. For the first test you can change
# GROUP=ACTIVE to GROUP=STATIONS.
CELESTRAK_URL = (
    "https://celestrak.org/NORAD/elements/gp.php"
    "?GROUP=ACTIVE&FORMAT=JSON"
)

# ============================================================
# 2. HELPER FUNCTIONS
# ============================================================

def die(message):
    print("\n❌ ERROR:")
    print(message)
    sys.exit(1)


def connect(database):
    try:
        return psycopg2.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            database=database,
        )
    except Exception as e:
        die(
            f"Could not connect to PostgreSQL database '{database}'.\n"
            f"Check that PostgreSQL is running and that your username/"
            f"password are correct.\n\nDetails: {e}"
        )


def database_exists():
    conn = connect("postgres")
    conn.autocommit = True

    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s;",
            (DB_NAME,),
        )
        exists = cur.fetchone() is not None
        cur.close()
        return exists
    finally:
        conn.close()


def create_database_if_needed():
    print("1/5 Checking PostgreSQL database...")

    if database_exists():
        print(f"   ✅ Database '{DB_NAME}' already exists.")
        return

    conn = connect("postgres")
    conn.autocommit = True

    try:
        cur = conn.cursor()
        # Database names cannot be passed as normal SQL parameters,
        # so psycopg2's identifier quoting is used here.
        cur.execute(
            sql.SQL("CREATE DATABASE {}").format(
                sql.Identifier(DB_NAME)
            )
        )
        cur.close()
        print(f"   ✅ Created database '{DB_NAME}'.")
    except Exception as e:
        die(f"Could not create database '{DB_NAME}'.\n\nDetails: {e}")
    finally:
        conn.close()


def setup_schema():
    print("2/5 Installing your database schema...")

    if not SQL_FILE.exists():
        die(
            f"Could not find:\n{SQL_FILE}\n\n"
            "Put main.py and space_collision_risk_triage.sql "
            "in the same folder."
        )

    sql_text = SQL_FILE.read_text(encoding="utf-8")

    # The supplied SQL file contains BEGIN/COMMIT.
    # Remove those transaction commands because this Python program
    # manages the transaction itself.
    sql_text = sql_text.replace("BEGIN;", "")
    sql_text = sql_text.replace("COMMIT;", "")

    conn = connect(DB_NAME)

    try:
        cur = conn.cursor()
        cur.execute(sql_text)
        conn.commit()
        cur.close()
        print("   ✅ Tables, indexes, triggers, sources and dashboard view are ready.")
    except Exception as e:
        conn.rollback()
        die(f"Could not execute the SQL schema.\n\nDetails: {e}")
    finally:
        conn.close()


def remove_test_data():
    """
    The SQL file contains two clearly labelled synthetic test objects.
    We remove them so the database contains real CelesTrak data only.
    """
    conn = connect(DB_NAME)

    try:
        cur = conn.cursor()

        cur.execute(
            """
            DELETE FROM space_objects
            WHERE norad_id IN (99901, 99902);
            """
        )

        conn.commit()
        cur.close()
    except Exception as e:
        conn.rollback()
        die(f"Could not remove synthetic test records.\n\nDetails: {e}")
    finally:
        conn.close()


def get_celestrak_data():
    print("3/5 Downloading real orbital data from CelesTrak...")
    print("   This can take a little time because ACTIVE is a large group.")

    try:
        response = requests.get(
            CELESTRAK_URL,
            timeout=60,
            headers={"User-Agent": "SpaceCollisionRiskTriage/1.0"},
        )
        response.raise_for_status()

        data = response.json()

        if not isinstance(data, list):
            die("CelesTrak returned an unexpected JSON format.")

        print(f"   ✅ Received {len(data):,} orbital records.")
        return data

    except requests.RequestException as e:
        die(f"Could not download data from CelesTrak.\n\nDetails: {e}")
    except json.JSONDecodeError as e:
        die(f"CelesTrak response was not valid JSON.\n\nDetails: {e}")


def parse_epoch(epoch_text):
    if not epoch_text:
        return None

    try:
        # CelesTrak GP JSON uses ISO-style timestamps.
        # Convert trailing Z to +00:00 for Python.
        value = epoch_text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(value)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt
    except ValueError:
        return None


def classify_object(name):
    """
    CelesTrak GP data does not provide a complete physical object
    classification for every record. We therefore use a simple
    name-based classification and fall back to UNKNOWN.
    """
    name = (name or "").upper()

    if "DEB" in name or "DEBRIS" in name:
        return "DEBRIS"

    if "R/B" in name or "ROCKET BODY" in name:
        return "ROCKET BODY"

    return "UNKNOWN"


def import_data(data):
    print("4/5 Importing real data into PostgreSQL...")

    conn = connect(DB_NAME)

    inserted_objects = 0
    updated_objects = 0
    inserted_orbits = 0
    skipped = 0

    try:
        cur = conn.cursor()

        # Find the CelesTrak source row created by your SQL file.
        cur.execute(
            """
            SELECT source_id
            FROM data_sources
            WHERE source_name = 'CelesTrak'
            LIMIT 1;
            """
        )

        row = cur.fetchone()

        if row is None:
            die("The CelesTrak data source was not found in data_sources.")

        celestrak_source_id = row[0]

        for obj in data:
            norad_id = obj.get("NORAD_CAT_ID")
            name = obj.get("OBJECT_NAME")

            if norad_id is None or not name:
                skipped += 1
                continue

            try:
                norad_id = int(norad_id)
            except (TypeError, ValueError):
                skipped += 1
                continue

            international_designator = obj.get("OBJECT_ID")
            object_type = classify_object(name)

            # Insert/update the catalog object.
            cur.execute(
                """
                INSERT INTO space_objects
                (
                    norad_id,
                    international_designator,
                    object_name,
                    object_type,
                    status,
                    source_id
                )
                VALUES (%s, %s, %s, %s, 'ACTIVE', %s)
                ON CONFLICT (norad_id)
                DO UPDATE SET
                    international_designator =
                        EXCLUDED.international_designator,
                    object_name = EXCLUDED.object_name,
                    object_type = EXCLUDED.object_type,
                    status = 'ACTIVE',
                    source_id = EXCLUDED.source_id,
                    updated_at = NOW()
                RETURNING object_id;
                """,
                (
                    norad_id,
                    international_designator,
                    name,
                    object_type,
                    celestrak_source_id,
                ),
            )

            object_id = cur.fetchone()[0]

            # Parse orbital epoch.
            epoch = parse_epoch(obj.get("EPOCH"))

            if epoch is None:
                skipped += 1
                continue

            # Insert the GP/OMM-derived orbital record.
            # UNIQUE(object_id, epoch) prevents duplicates.
            cur.execute(
                """
                INSERT INTO orbital_elements
                (
                    object_id,
                    epoch,
                    inclination_deg,
                    raan_deg,
                    eccentricity,
                    arg_perigee_deg,
                    mean_anomaly_deg,
                    mean_motion_rev_day,
                    bstar,
                    source_format,
                    source_id,
                    raw_data
                )
                VALUES
                (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    'OMM/GP', %s, %s::jsonb
                )
                ON CONFLICT (object_id, epoch)
                DO NOTHING;
                """,
                (
                    object_id,
                    epoch,
                    obj.get("INCLINATION"),
                    obj.get("RA_OF_ASC_NODE"),
                    obj.get("ECCENTRICITY"),
                    obj.get("ARG_OF_PERICENTER"),
                    obj.get("MEAN_ANOMALY"),
                    obj.get("MEAN_MOTION"),
                    obj.get("BSTAR"),
                    celestrak_source_id,
                    json.dumps(obj),
                ),
            )

            if cur.rowcount == 1:
                inserted_orbits += 1

        conn.commit()

        # Count objects after import.
        cur.execute("SELECT COUNT(*) FROM space_objects;")
        total_objects = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM orbital_elements;")
        total_orbits = cur.fetchone()[0]

        cur.close()

        print(f"   ✅ Database objects: {total_objects:,}")
        print(f"   ✅ Orbital records:  {total_orbits:,}")
        print(f"   ✅ New orbital records this run: {inserted_orbits:,}")
        print(f"   ℹ️ Skipped records: {skipped:,}")

    except Exception as e:
        conn.rollback()
        die(f"Could not import CelesTrak data.\n\nDetails: {e}")
    finally:
        conn.close()


def verify():
    print("5/5 Verifying the database...")

    conn = connect(DB_NAME)

    try:
        cur = conn.cursor()

        cur.execute(
            """
            SELECT
                so.norad_id,
                so.object_name,
                oe.epoch,
                oe.inclination_deg,
                oe.eccentricity,
                oe.mean_motion_rev_day
            FROM space_objects so
            JOIN orbital_elements oe
              ON oe.object_id = so.object_id
            ORDER BY oe.retrieved_at DESC
            LIMIT 5;
            """
        )

        rows = cur.fetchall()

        print("\n   SAMPLE REAL DATA:")
        print("   " + "-" * 75)

        for row in rows:
            print(
                f"   NORAD: {row[0]} | "
                f"Name: {row[1][:28]:28} | "
                f"Epoch: {row[2]} | "
                f"Inc: {row[3]}"
            )

        cur.close()

    finally:
        conn.close()


# ============================================================
# 3. MAIN PROGRAM
# ============================================================

def main():
    print("=" * 70)
    print(" SPACE COLLISION RISK TRIAGE - DATABASE + CELESTRAK IMPORT")
    print("=" * 70)

    if DB_PASSWORD == "YOUR_POSTGRES_PASSWORD":
        die(
            "Please edit main.py and replace:\n\n"
            'DB_PASSWORD = "YOUR_POSTGRES_PASSWORD"\n\n'
            "with your actual PostgreSQL password."
        )

    create_database_if_needed()
    setup_schema()
    remove_test_data()

    data = get_celestrak_data()
    import_data(data)
    verify()

    print("\n" + "=" * 70)
    print("✅ SETUP AND REAL DATA IMPORT COMPLETED SUCCESSFULLY")
    print("=" * 70)
    print("\nYour project now has:")
    print("  • PostgreSQL database")
    print("  • Real CelesTrak orbital data")
    print("  • Space-object records")
    print("  • Historical orbital-element storage")
    print("\nNEXT STEP:")
    print("  Orbit propagation + conjunction detection + risk scoring")


if __name__ == "__main__":
    main()
