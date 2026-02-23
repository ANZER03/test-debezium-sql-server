-- =============================================================================
-- create-signal-table.sql
-- Purpose: Create and CDC-enable the Debezium signaling table
--
-- This table is required for incremental snapshot watermarking.
-- Even when triggering snapshots via Kafka signals, the SQL Server signaling
-- table is needed because Debezium writes watermark markers into it.
-- These markers flow through the CDC transaction log, allowing the connector
-- to know when a snapshot chunk's "window" opens and closes.
-- =============================================================================

USE AdventureWorks2019;
GO

-- Create the Debezium signaling table
-- This table is used by the incremental snapshot watermark mechanism
CREATE TABLE dbo.debezium_signal (
    id   VARCHAR(42)   PRIMARY KEY,   -- unique signal ID
    type VARCHAR(32)   NOT NULL,      -- signal type: 'execute-snapshot', 'stop-snapshot'
    data VARCHAR(2048) NULL           -- signal payload (JSON)
);
GO

-- Enable CDC on the signaling table (REQUIRED for watermarking)
EXEC sys.sp_cdc_enable_table
    @source_schema = N'dbo',
    @source_name   = N'debezium_signal',
    @role_name     = NULL,
    @supports_net_changes = 1;
GO

-- Verify CDC is enabled on the signal table
SELECT 
    s.name AS [schema_name],
    t.name AS [table_name],
    t.is_tracked_by_cdc
FROM sys.tables t
INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
WHERE t.name = 'debezium_signal';
GO

PRINT 'Signal table created and CDC enabled successfully!';
GO
