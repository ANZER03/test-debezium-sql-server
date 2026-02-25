-- =============================================================================
-- init.sql — PostgreSQL benchmark database setup
-- Creates a 2,000,000-row table mirroring the SQL Server benchmark table
-- (SalesOrderDetailBig) for a fair apples-to-apples comparison.
-- =============================================================================

-- Publication for Debezium logical replication (pgoutput plugin)
-- Created before the table so Debezium can use snapshot + streaming.

CREATE TABLE IF NOT EXISTS sales_order_detail_big (
    sales_order_detail_id  SERIAL          PRIMARY KEY,
    sales_order_id         INT             NOT NULL,
    carrier_tracking_number VARCHAR(25),
    order_qty              SMALLINT        NOT NULL,
    product_id             INT             NOT NULL,
    special_offer_id       INT             NOT NULL DEFAULT 1,
    unit_price             NUMERIC(19,4)   NOT NULL,
    unit_price_discount     NUMERIC(19,4)   NOT NULL DEFAULT 0.0,
    line_total             NUMERIC(38,6)   GENERATED ALWAYS AS
                               (order_qty * unit_price * (1 - unit_price_discount)) STORED,
    rowguid                UUID            NOT NULL DEFAULT gen_random_uuid(),
    modified_date          TIMESTAMP       NOT NULL DEFAULT NOW()
);

-- Populate 2,000,000 rows using generate_series for speed
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

-- Publication for Debezium pgoutput plugin
CREATE PUBLICATION debezium_pub FOR TABLE sales_order_detail_big;
