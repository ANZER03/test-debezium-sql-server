-- =============================================================================
-- create-benchmark-table.sql
-- Purpose: Create Sales.SalesOrderDetailBig with exactly 2,000,000 rows by
--          selecting from SalesOrderDetail 40 times via CROSS JOIN.
--
-- Strategy:
--   1. CREATE TABLE with correct NOT NULL / PK constraints
--   2. INSERT ... SELECT TOP 2000000 with ROW_NUMBER() as synthetic PK
--   3. Enable CDC on the new table
--
-- Runtime estimate: 2-5 minutes on a dev machine.
-- =============================================================================

USE AdventureWorks2019;
GO

-- =============================================================================
-- STEP 1: Drop table if it already exists (idempotent re-run)
-- =============================================================================
IF OBJECT_ID('Sales.SalesOrderDetailBig', 'U') IS NOT NULL
BEGIN
    PRINT 'Dropping existing Sales.SalesOrderDetailBig...';

    -- Must disable CDC first if it was already enabled
    IF EXISTS (
        SELECT 1 FROM cdc.change_tables
        WHERE OBJECT_NAME(source_object_id) = 'SalesOrderDetailBig'
    )
    BEGIN
        EXEC sys.sp_cdc_disable_table
            @source_schema = N'Sales',
            @source_name   = N'SalesOrderDetailBig',
            @capture_instance = N'all';
    END

    DROP TABLE Sales.SalesOrderDetailBig;
    PRINT '  Dropped.';
END
GO

-- =============================================================================
-- STEP 2: Create table with proper types and NOT NULL constraints
--         (mirrors SalesOrderDetail schema, SalesOrderDetailID is synthetic PK)
-- =============================================================================
PRINT 'Creating table Sales.SalesOrderDetailBig...';
CREATE TABLE Sales.SalesOrderDetailBig (
    SalesOrderDetailID   INT              NOT NULL,   -- Synthetic surrogate PK (ROW_NUMBER)
    SalesOrderID         INT              NOT NULL,
    CarrierTrackingNumber NVARCHAR(25)    NULL,
    OrderQty             SMALLINT         NOT NULL,
    ProductID            INT              NOT NULL,
    SpecialOfferID       INT              NOT NULL,
    UnitPrice            MONEY            NOT NULL,
    UnitPriceDiscount    MONEY            NOT NULL,
    LineTotal            NUMERIC(38,6)    NOT NULL,
    rowguid              UNIQUEIDENTIFIER NOT NULL,
    ModifiedDate         DATETIME         NOT NULL,
    CONSTRAINT PK_SalesOrderDetailBig_ID PRIMARY KEY CLUSTERED (SalesOrderDetailID)
);
GO
PRINT '  Table created.';

-- =============================================================================
-- STEP 3: Populate with 2,000,000 rows
--
-- SalesOrderDetail has ~121,317 rows.
-- CROSS JOIN with a 40-row values table => ~4.85M rows.
-- TOP 2000000 limits to exactly 2M rows.
-- ROW_NUMBER() gives each row a unique SalesOrderDetailID.
-- =============================================================================
PRINT 'Inserting 2,000,000 rows (this may take 2-5 minutes)...';
GO

INSERT INTO Sales.SalesOrderDetailBig
    (SalesOrderDetailID, SalesOrderID, CarrierTrackingNumber,
     OrderQty, ProductID, SpecialOfferID, UnitPrice, UnitPriceDiscount,
     LineTotal, rowguid, ModifiedDate)
SELECT TOP 2000000
    CAST(ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS INT) AS SalesOrderDetailID,
    sod.SalesOrderID,
    sod.CarrierTrackingNumber,
    sod.OrderQty,
    sod.ProductID,
    sod.SpecialOfferID,
    sod.UnitPrice,
    sod.UnitPriceDiscount,
    sod.LineTotal,
    sod.rowguid,
    sod.ModifiedDate
FROM Sales.SalesOrderDetail AS sod
CROSS JOIN (
    VALUES (1),(2),(3),(4),(5),(6),(7),(8),(9),(10),
           (11),(12),(13),(14),(15),(16),(17),(18),(19),(20),
           (21),(22),(23),(24),(25),(26),(27),(28),(29),(30),
           (31),(32),(33),(34),(35),(36),(37),(38),(39),(40)
) AS multiplier(n);
GO

-- =============================================================================
-- STEP 4: Verify row count
-- =============================================================================
SELECT
    'Sales.SalesOrderDetailBig' AS TableName,
    COUNT(*)                    AS TotalRows,
    MIN(SalesOrderDetailID)     AS MinID,
    MAX(SalesOrderDetailID)     AS MaxID
FROM Sales.SalesOrderDetailBig;
GO

-- =============================================================================
-- STEP 5: Enable CDC on the new table
-- =============================================================================
PRINT 'Enabling CDC on Sales.SalesOrderDetailBig...';
EXEC sys.sp_cdc_enable_table
    @source_schema        = N'Sales',
    @source_name          = N'SalesOrderDetailBig',
    @role_name            = NULL,
    @supports_net_changes = 1;
GO

-- =============================================================================
-- STEP 6: Verify CDC is enabled
-- =============================================================================
SELECT
    s.name AS schema_name,
    t.name AS table_name,
    t.is_tracked_by_cdc
FROM sys.tables  t
JOIN sys.schemas s ON t.schema_id = s.schema_id
WHERE t.name = 'SalesOrderDetailBig';
GO

PRINT 'Done. Sales.SalesOrderDetailBig is ready for benchmarking.';
GO
