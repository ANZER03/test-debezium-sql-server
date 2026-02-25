-- =============================================================================
-- init.sql — PostgreSQL production database setup for Debezium CDC
-- =============================================================================
-- This script runs automatically when the postgres container first starts
-- (mounted via /docker-entrypoint-initdb.d).
--
-- It creates the publication required by the Debezium pgoutput plugin.
-- Your application tables and data are expected to already exist (restored
-- from a backup or created by your application's migration framework).
--
-- If you are setting up the benchmark environment, the INSERT block at the
-- bottom populates 2,000,000 rows for load testing. Remove it for production.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Enable required extensions
-- -----------------------------------------------------------------------------

-- pgcrypto provides gen_random_uuid() used for rowguid columns.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- -----------------------------------------------------------------------------
-- Benchmark table (remove in production if your real table already exists)
-- -----------------------------------------------------------------------------
-- This mirrors the SQL Server SalesOrderDetailBig structure for a fair
-- apples-to-apples comparison between MSSQL and PostgreSQL CDC performance.
--
-- Decision: GENERATED ALWAYS AS STORED for computed columns
--   PostgreSQL does not support virtual computed columns (computed at read time).
--   STORED means the value is computed at write time and physically stored,
--   matching the SQL Server persisted computed column behavior.
--   This adds a small write overhead but is necessary for CDC — Debezium reads
--   the stored value directly from the WAL without re-evaluating the expression.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sales_order_detail_big (
    sales_order_detail_id   SERIAL          PRIMARY KEY,
    sales_order_id          INT             NOT NULL,
    carrier_tracking_number VARCHAR(25),
    order_qty               SMALLINT        NOT NULL,
    product_id              INT             NOT NULL,
    special_offer_id        INT             NOT NULL DEFAULT 1,
    unit_price              NUMERIC(19,4)   NOT NULL,
    unit_price_discount      NUMERIC(19,4)   NOT NULL DEFAULT 0.0,
    -- Computed column: stored so Debezium can read it from WAL
    line_total              NUMERIC(38,6)   GENERATED ALWAYS AS
                                (order_qty * unit_price * (1 - unit_price_discount)) STORED,
    rowguid                 UUID            NOT NULL DEFAULT gen_random_uuid(),
    modified_date           TIMESTAMP       NOT NULL DEFAULT NOW()
);

-- Index on sales_order_id for efficient range scans during snapshot partitioning
-- (used with snapshot.select.statement.overrides for large tables > 50M rows)
CREATE INDEX IF NOT EXISTS idx_sales_order_detail_big_order_id
    ON sales_order_detail_big (sales_order_id);

-- -----------------------------------------------------------------------------
-- Populate benchmark data (2,000,000 rows) — REMOVE IN PRODUCTION
-- -----------------------------------------------------------------------------
-- Uses generate_series for a single-pass bulk insert (fastest method).
-- generate_series is O(n) and runs entirely in PostgreSQL memory — no client
-- round trips. On a 4-core machine this completes in ~30-60 seconds.
-- -----------------------------------------------------------------------------
INSERT INTO sales_order_detail_big (
    sales_order_id,
    carrier_tracking_number,
    order_qty,
    product_id,
    special_offer_id,
    unit_price,
    unit_price_discount,
    rowguid,
    modified_date
)
SELECT
    (i % 100000) + 1,
    LPAD((i % 99999)::TEXT, 8, '0') || '-' || LPAD((i % 999)::TEXT, 4, '0'),
    (i % 44) + 1,
    (i % 516) + 1,
    (i % 16) + 1,
    ROUND((RANDOM() * 3500 + 1)::NUMERIC, 4),
    CASE WHEN i % 10 = 0 THEN ROUND((RANDOM() * 0.4)::NUMERIC, 4) ELSE 0.0 END,
    gen_random_uuid(),
    NOW() - (RANDOM() * INTERVAL '3 years')
FROM generate_series(1, 2000000) AS s(i);

-- -----------------------------------------------------------------------------
-- Debezium publication
-- -----------------------------------------------------------------------------
-- Decision: named publication (not FOR ALL TABLES)
--   FOR ALL TABLES publishes every table in the database including system
--   catalog changes. This increases WAL volume and replication slot lag.
--   A named publication for specific tables limits the WAL decoded by the
--   slot to only the tables being migrated, reducing overhead.
--
-- Decision: created AFTER data insert
--   Creating the publication before inserting 2M rows would cause the logical
--   replication slot (created by the connector) to retain WAL for all those
--   INSERTs before the connector reads them. Creating the publication after
--   data load means the connector only reads changes that happen after slot
--   creation — the initial snapshot is done via COPY, not WAL replay.
-- -----------------------------------------------------------------------------
CREATE PUBLICATION debezium_pub FOR TABLE sales_order_detail_big;
