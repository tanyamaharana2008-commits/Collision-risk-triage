-- ============================================================
-- SPACE CONJUNCTION COLLISION RISK TRIAGE DATABASE
-- PostgreSQL schema for a real-data backend
-- ============================================================

-- Create database separately if needed:
-- CREATE DATABASE space_collision_db;

-- Connect to the database before running the rest of this file.
-- Example with psql:
-- \c space_collision_db

BEGIN;

-- ============================================================
-- 1. DATA SOURCES
-- ============================================================

CREATE TABLE IF NOT EXISTS data_sources (
    source_id       SERIAL PRIMARY KEY,
    source_name     VARCHAR(100) NOT NULL UNIQUE,
    source_type     VARCHAR(50),
    base_url        TEXT,
    description     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- 2. SPACE OBJECTS
-- ============================================================

CREATE TABLE IF NOT EXISTS space_objects (
    object_id           BIGSERIAL PRIMARY KEY,
    norad_id            INTEGER UNIQUE,
    international_designator VARCHAR(30),
    object_name         VARCHAR(255) NOT NULL,
    object_type         VARCHAR(50),   -- PAYLOAD, DEBRIS, ROCKET BODY, UNKNOWN
    country             VARCHAR(100),
    launch_date         DATE,
    decay_date          DATE,
    mass_kg             DOUBLE PRECISION,
    length_m            DOUBLE PRECISION,
    width_m             DOUBLE PRECISION,
    height_m            DOUBLE PRECISION,
    status              VARCHAR(50) DEFAULT 'ACTIVE',
    source_id           INTEGER REFERENCES data_sources(source_id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_space_objects_norad
    ON space_objects(norad_id);

CREATE INDEX IF NOT EXISTS idx_space_objects_type
    ON space_objects(object_type);

-- ============================================================
-- 3. ORBITAL ELEMENTS
-- Supports TLE/GP/OMM-derived records.
-- Keep historical records instead of overwriting old orbits.
-- ============================================================

CREATE TABLE IF NOT EXISTS orbital_elements (
    orbital_id          BIGSERIAL PRIMARY KEY,
    object_id           BIGINT NOT NULL REFERENCES space_objects(object_id)
                        ON DELETE CASCADE,

    epoch               TIMESTAMPTZ NOT NULL,

    -- Classical orbital elements
    inclination_deg     DOUBLE PRECISION,
    raan_deg            DOUBLE PRECISION,
    eccentricity        DOUBLE PRECISION,
    arg_perigee_deg     DOUBLE PRECISION,
    mean_anomaly_deg    DOUBLE PRECISION,
    mean_motion_rev_day DOUBLE PRECISION,

    -- SGP4 / GP parameter
    bstar               DOUBLE PRECISION,

    -- Optional TLE storage
    tle_line1           TEXT,
    tle_line2           TEXT,

    -- Optional OMM/raw source payload
    source_format       VARCHAR(20), -- TLE, OMM, GP
    source_id           INTEGER REFERENCES data_sources(source_id),
    raw_data            JSONB,

    retrieved_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE(object_id, epoch)
);

CREATE INDEX IF NOT EXISTS idx_orbital_object_epoch
    ON orbital_elements(object_id, epoch DESC);

CREATE INDEX IF NOT EXISTS idx_orbital_epoch
    ON orbital_elements(epoch DESC);

-- ============================================================
-- 4. CONJUNCTION EVENTS
-- One event represents a possible close approach between two
-- catalogued objects.
-- ============================================================

CREATE TABLE IF NOT EXISTS conjunction_events (
    conjunction_id          BIGSERIAL PRIMARY KEY,

    primary_object_id       BIGINT NOT NULL
                            REFERENCES space_objects(object_id),

    secondary_object_id     BIGINT NOT NULL
                            REFERENCES space_objects(object_id),

    tca                     TIMESTAMPTZ NOT NULL,

    miss_distance_m         DOUBLE PRECISION,
    relative_velocity_mps   DOUBLE PRECISION,

    -- Collision probability, if supplied/calculated.
    collision_probability   DOUBLE PRECISION,

    radial_miss_distance_m  DOUBLE PRECISION,
    in_track_miss_distance_m DOUBLE PRECISION,
    cross_track_miss_distance_m DOUBLE PRECISION,

    screening_radius_m      DOUBLE PRECISION,

    -- Estimated combined physical dimensions
    combined_radius_m       DOUBLE PRECISION,

    -- Data provenance
    source_id               INTEGER REFERENCES data_sources(source_id),
    external_event_id       VARCHAR(255),

    status                  VARCHAR(50) DEFAULT 'OPEN',

    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CHECK (primary_object_id <> secondary_object_id),
    CHECK (
        collision_probability IS NULL
        OR (collision_probability >= 0
            AND collision_probability <= 1)
    ),
    CHECK (
        miss_distance_m IS NULL
        OR miss_distance_m >= 0
    )
);

CREATE INDEX IF NOT EXISTS idx_conjunction_tca
    ON conjunction_events(tca);

CREATE INDEX IF NOT EXISTS idx_conjunction_primary
    ON conjunction_events(primary_object_id);

CREATE INDEX IF NOT EXISTS idx_conjunction_secondary
    ON conjunction_events(secondary_object_id);

CREATE INDEX IF NOT EXISTS idx_conjunction_probability
    ON conjunction_events(collision_probability DESC);

-- ============================================================
-- 5. RISK ASSESSMENTS
-- Stores the output of your triage/risk engine.
-- ============================================================

CREATE TABLE IF NOT EXISTS risk_assessments (
    risk_id                 BIGSERIAL PRIMARY KEY,

    conjunction_id          BIGINT NOT NULL
                            REFERENCES conjunction_events(conjunction_id)
                            ON DELETE CASCADE,

    risk_score              DOUBLE PRECISION NOT NULL,

    risk_level              VARCHAR(20) NOT NULL,
    -- LOW / MEDIUM / HIGH / CRITICAL

    time_to_tca_seconds     DOUBLE PRECISION,

    miss_distance_score     DOUBLE PRECISION,
    collision_probability_score DOUBLE PRECISION,
    relative_velocity_score DOUBLE PRECISION,
    object_size_score       DOUBLE PRECISION,
    uncertainty_score       DOUBLE PRECISION,

    model_name              VARCHAR(100),
    model_version           VARCHAR(50),

    explanation             TEXT,

    assessed_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CHECK (risk_score >= 0),
    CHECK (risk_level IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL'))
);

CREATE INDEX IF NOT EXISTS idx_risk_level
    ON risk_assessments(risk_level);

CREATE INDEX IF NOT EXISTS idx_risk_score
    ON risk_assessments(risk_score DESC);

CREATE INDEX IF NOT EXISTS idx_risk_assessed_at
    ON risk_assessments(assessed_at DESC);

-- ============================================================
-- 6. ALERTS
-- Used by the backend/dashboard to notify users.
-- ============================================================

CREATE TABLE IF NOT EXISTS alerts (
    alert_id            BIGSERIAL PRIMARY KEY,

    conjunction_id      BIGINT NOT NULL
                        REFERENCES conjunction_events(conjunction_id)
                        ON DELETE CASCADE,

    risk_id             BIGINT REFERENCES risk_assessments(risk_id)
                        ON DELETE SET NULL,

    alert_type          VARCHAR(50) NOT NULL,
    severity            VARCHAR(20) NOT NULL,

    title               VARCHAR(255) NOT NULL,
    message             TEXT,

    is_read             BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at         TIMESTAMPTZ,

    CHECK (severity IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL'))
);

CREATE INDEX IF NOT EXISTS idx_alerts_unread
    ON alerts(is_read, created_at DESC);

-- ============================================================
-- 7. UPDATED_AT TRIGGER
-- ============================================================

CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_space_objects_updated
ON space_objects;

CREATE TRIGGER trg_space_objects_updated
BEFORE UPDATE ON space_objects
FOR EACH ROW
EXECUTE FUNCTION update_updated_at();

DROP TRIGGER IF EXISTS trg_conjunction_events_updated
ON conjunction_events;

CREATE TRIGGER trg_conjunction_events_updated
BEFORE UPDATE ON conjunction_events
FOR EACH ROW
EXECUTE FUNCTION update_updated_at();

-- ============================================================
-- 8. SEED DATA SOURCES
-- Replace/add credentials through your backend environment.
-- NEVER store API passwords in this database script.
-- ============================================================

INSERT INTO data_sources
    (source_name, source_type, base_url, description)
VALUES
    (
        'Space-Track',
        'ORBITAL_DATA',
        'https://www.space-track.org',
        'US Space Force catalog and orbital data service'
    ),
    (
        'CelesTrak',
        'ORBITAL_DATA',
        'https://celestrak.org',
        'Public satellite and orbital element data'
    ),
    (
        'ESA DISCOS',
        'OBJECT_CATALOG',
        'https://discosweb.esoc.esa.int',
        'ESA space object characteristics database'
    ),
    (
        'TraCSS',
        'CONJUNCTION_DATA',
        'https://www.space.commerce.gov/traffic-coordination-system-for-space/',
        'US Office of Space Commerce space traffic coordination resources'
    )
ON CONFLICT (source_name) DO NOTHING;

-- ============================================================
-- 9. TEST OBJECTS
-- These are ONLY for local database testing.
-- Replace with actual catalog data during ingestion.
-- ============================================================

INSERT INTO space_objects
    (norad_id, international_designator, object_name,
     object_type, country, status, source_id)
SELECT
    99901,
    'TEST-2026-A',
    'TEST OBJECT A',
    'PAYLOAD',
    'TEST',
    'ACTIVE',
    source_id
FROM data_sources
WHERE source_name = 'CelesTrak'
ON CONFLICT (norad_id) DO NOTHING;

INSERT INTO space_objects
    (norad_id, international_designator, object_name,
     object_type, country, status, source_id)
SELECT
    99902,
    'TEST-2026-B',
    'TEST OBJECT B',
    'DEBRIS',
    'TEST',
    'ACTIVE',
    source_id
FROM data_sources
WHERE source_name = 'CelesTrak'
ON CONFLICT (norad_id) DO NOTHING;

-- ============================================================
-- 10. TEST ORBITAL RECORDS
-- Synthetic test values. DO NOT treat these as real orbital data.
-- ============================================================

INSERT INTO orbital_elements
    (object_id, epoch, inclination_deg, raan_deg, eccentricity,
     arg_perigee_deg, mean_anomaly_deg, mean_motion_rev_day,
     source_format, source_id)
SELECT
    so.object_id,
    NOW(),
    51.6,
    120.0,
    0.001,
    80.0,
    20.0,
    15.5,
    'TEST',
    ds.source_id
FROM space_objects so
JOIN data_sources ds
  ON ds.source_name = 'CelesTrak'
WHERE so.norad_id = 99901
ON CONFLICT (object_id, epoch) DO NOTHING;

-- ============================================================
-- 11. USEFUL VIEW FOR DASHBOARD
-- ============================================================

CREATE OR REPLACE VIEW active_conjunction_dashboard AS
SELECT
    ce.conjunction_id,
    p.norad_id AS primary_norad_id,
    p.object_name AS primary_object,
    s.norad_id AS secondary_norad_id,
    s.object_name AS secondary_object,
    ce.tca,
    ce.miss_distance_m,
    ce.relative_velocity_mps,
    ce.collision_probability,
    ce.status,
    ra.risk_score,
    ra.risk_level,
    ra.explanation,
    ra.assessed_at
FROM conjunction_events ce
JOIN space_objects p
    ON p.object_id = ce.primary_object_id
JOIN space_objects s
    ON s.object_id = ce.secondary_object_id
LEFT JOIN LATERAL (
    SELECT *
    FROM risk_assessments r
    WHERE r.conjunction_id = ce.conjunction_id
    ORDER BY r.assessed_at DESC
    LIMIT 1
) ra ON TRUE
WHERE ce.status = 'OPEN';

-- ============================================================
-- 12. DASHBOARD QUERY EXAMPLES
-- ============================================================

-- All upcoming conjunctions:
-- SELECT * FROM active_conjunction_dashboard
-- WHERE tca > NOW()
-- ORDER BY tca;

-- Highest-risk conjunctions:
-- SELECT * FROM active_conjunction_dashboard
-- ORDER BY risk_score DESC NULLS LAST;

-- Critical alerts:
-- SELECT * FROM alerts
-- WHERE severity = 'CRITICAL'
-- AND is_read = FALSE
-- ORDER BY created_at DESC;

COMMIT;

-- ============================================================
-- END OF DATABASE SCHEMA
-- ============================================================
