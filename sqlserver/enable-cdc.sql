-- =============================================================================
-- enable-cdc.sql
-- Purpose: Enable Change Data Capture (CDC) on the AdventureWorks2019 database
--          and on specific tables that we want to track changes for.
--
-- CDC allows SQL Server to capture row-level INSERT, UPDATE, DELETE changes
-- into special "change tables" that Debezium reads from.
--
-- Prerequisites:
--   - AdventureWorks2019 database must be restored before running this script
--   - SQL Server Agent must be running (MSSQL_AGENT_ENABLED=true in Docker)
-- =============================================================================

-- Switch to the AdventureWorks2019 database context
-- All subsequent commands will execute against this database
USE AdventureWorks2019;
GO

-- =============================================================================
-- STEP 1: Enable CDC at the DATABASE level
-- This is required before enabling CDC on any individual table.
-- It creates the cdc schema and several system tables in the database.
-- It also creates two SQL Server Agent jobs:
--   - cdc.AdventureWorks2019_capture (reads transaction log for changes)
--   - cdc.AdventureWorks2019_cleanup (removes old change data)
-- =============================================================================
EXEC sys.sp_cdc_enable_db;
GO

-- Verify CDC is enabled on the database
-- is_cdc_enabled should return 1
SELECT name, is_cdc_enabled
FROM sys.databases
WHERE name = 'AdventureWorks2019';
GO

-- =============================================================================
-- STEP 2: Enable CDC on individual tables
-- For each table, sp_cdc_enable_table creates:
--   - A change table (cdc.<capture_instance>_CT) to store row changes
--   - Functions to query changes (cdc.fn_cdc_get_all_changes_*, cdc.fn_cdc_get_net_changes_*)
--
-- Parameters explained:
--   @source_schema   = The schema name of the source table (e.g., 'Person', 'Sales')
--   @source_name     = The table name to track
--   @role_name       = Database role that can access change data (NULL = no role gating)
--   @supports_net_changes = 1 means we can query "net changes" (collapsed view of all
--                           changes between two LSNs), requires a unique index/PK
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Table 1: Person.Person
-- Contains core person data (names, person types).
-- This is a large table (~19,000 rows) - good for testing CDC at scale.
-- Primary key: BusinessEntityID
-- -----------------------------------------------------------------------------
EXEC sys.sp_cdc_enable_table
    @source_schema = N'Person',          -- Schema containing the table
    @source_name   = N'Person',          -- Table name to enable CDC on
    @role_name     = NULL,               -- No role restriction (any user can read changes)
    @supports_net_changes = 1;           -- Enable net changes (requires PK/unique index)
GO

-- -----------------------------------------------------------------------------
-- Table 2: Sales.Customer
-- Contains customer records linked to Person and Store.
-- Primary key: CustomerID
-- Good for testing CDC with foreign key relationships.
-- -----------------------------------------------------------------------------
EXEC sys.sp_cdc_enable_table
    @source_schema = N'Sales',           -- Schema: Sales
    @source_name   = N'Customer',        -- Table: Customer
    @role_name     = NULL,               -- No role restriction
    @supports_net_changes = 1;           -- Enable net changes
GO

-- -----------------------------------------------------------------------------
-- Table 3: Sales.SalesOrderHeader
-- Contains order header information (order date, customer, totals).
-- Primary key: SalesOrderID
-- High-volume table - perfect for testing CDC event throughput.
-- -----------------------------------------------------------------------------
EXEC sys.sp_cdc_enable_table
    @source_schema = N'Sales',           -- Schema: Sales
    @source_name   = N'SalesOrderHeader',-- Table: SalesOrderHeader
    @role_name     = NULL,               -- No role restriction
    @supports_net_changes = 1;           -- Enable net changes
GO

-- -----------------------------------------------------------------------------
-- Table 4: Sales.SalesOrderDetail
-- Contains order line items (product, quantity, unit price).
-- Primary key: SalesOrderID + SalesOrderDetailID (composite)
-- Tests CDC with composite primary keys.
-- -----------------------------------------------------------------------------
EXEC sys.sp_cdc_enable_table
    @source_schema = N'Sales',           -- Schema: Sales
    @source_name   = N'SalesOrderDetail',-- Table: SalesOrderDetail
    @role_name     = NULL,               -- No role restriction
    @supports_net_changes = 1;           -- Enable net changes
GO

-- -----------------------------------------------------------------------------
-- Table 5: Production.Product
-- Contains product catalog data (name, number, color, price).
-- Primary key: ProductID
-- Good for testing CDC with UPDATE operations (price changes, etc.)
-- -----------------------------------------------------------------------------
EXEC sys.sp_cdc_enable_table
    @source_schema = N'Production',      -- Schema: Production
    @source_name   = N'Product',         -- Table: Product
    @role_name     = NULL,               -- No role restriction
    @supports_net_changes = 1;           -- Enable net changes
GO

-- =============================================================================
-- STEP 3: Verify CDC is enabled on all tables
-- This query lists all tables with CDC enabled and their capture instances.
-- Each capture instance corresponds to a change table (cdc.*_CT).
-- =============================================================================
SELECT
    s.name AS schema_name,               -- Source table schema
    t.name AS table_name,                -- Source table name
    t.is_tracked_by_cdc                  -- 1 = CDC is tracking this table
FROM sys.tables t
JOIN sys.schemas s ON t.schema_id = s.schema_id
WHERE t.is_tracked_by_cdc = 1            -- Only show CDC-enabled tables
ORDER BY s.name, t.name;                 -- Sort by schema then table name
GO

-- =============================================================================
-- STEP 4: Verify CDC capture instances are created
-- Each enabled table gets a "capture instance" that defines what columns to track.
-- The capture_instance name is used by Debezium to identify the CDC source.
-- We join with sys.objects to get the source table name for each capture instance.
-- =============================================================================
SELECT
    ct.capture_instance,                  -- Name of the capture instance (e.g., Sales_Customer)
    OBJECT_SCHEMA_NAME(ct.source_object_id) AS source_schema,  -- Derive schema name from object ID
    OBJECT_NAME(ct.source_object_id) AS source_name,           -- Derive table name from object ID
    ct.supports_net_changes,              -- Whether net changes are supported
    ct.create_date                        -- When CDC was enabled on this table
FROM cdc.change_tables ct;                -- System table listing all CDC-enabled tables
GO
