#!/bin/bash
# =============================================================================
# setup-adventureworks.sh
# Purpose: Download, restore AdventureWorks2019, and enable CDC on SQL Server.
#
# This script runs INSIDE the SQL Server container (via docker exec).
# It performs 3 steps:
#   1. Wait for SQL Server to be ready
#   2. Download and restore AdventureWorks2019.bak
#   3. Enable CDC on the database and selected tables
#
# Usage:
#   docker exec -it sqlserver bash /scripts/setup-adventureworks.sh
# =============================================================================

# Exit immediately if any command fails
set -e

# -- Configuration variables --
SA_PASSWORD="YourStrong!Passw0rd"         # SA password (must match docker-compose.yml)
SQLCMD="/opt/mssql-tools18/bin/sqlcmd"    # Path to sqlcmd in SQL Server 2019 container
BACKUP_DIR="/var/opt/mssql/backup"        # Directory to store the .bak file
BACKUP_URL="https://github.com/Microsoft/sql-server-samples/releases/download/adventureworks/AdventureWorks2019.bak"
BACKUP_FILE="${BACKUP_DIR}/AdventureWorks2019.bak"  # Full path to the backup file

# =============================================================================
# STEP 1: Wait for SQL Server to be ready
# SQL Server can take 15-30 seconds to initialize on first run.
# We poll every 5 seconds until we can successfully connect.
# =============================================================================
echo "============================================"
echo "STEP 1: Waiting for SQL Server to be ready..."
echo "============================================"

# Loop up to 30 times (150 seconds max wait)
for i in $(seq 1 30); do
    # Try to run a simple query (-C trusts self-signed cert, -b returns error on failure)
    if ${SQLCMD} -S localhost -U sa -P "${SA_PASSWORD}" -Q "SELECT 1" -b -C > /dev/null 2>&1; then
        echo "SQL Server is ready!"
        break
    fi
    echo "  Attempt ${i}/30: SQL Server not ready yet, waiting 5s..."
    sleep 5
done

# Verify connection works (will exit with error if not)
${SQLCMD} -S localhost -U sa -P "${SA_PASSWORD}" -Q "SELECT @@VERSION" -C -W

# =============================================================================
# STEP 2: Download and restore AdventureWorks2019
# The .bak file is downloaded from Microsoft's GitHub releases.
# We restore it to the default SQL Server data directory on Linux.
# =============================================================================
echo ""
echo "============================================"
echo "STEP 2: Downloading AdventureWorks2019..."
echo "============================================"

# Create backup directory if it doesn't exist
mkdir -p ${BACKUP_DIR}

# Download the backup file (skip if already downloaded)
if [ -f "${BACKUP_FILE}" ]; then
    echo "  Backup file already exists, skipping download."
else
    echo "  Downloading from ${BACKUP_URL}..."
    echo "  This is ~50MB and may take a minute..."
    # wget is available in the SQL Server container (curl is not)
    # --no-check-certificate: skip SSL verification (safe for GitHub releases)
    # -O specifies the output file path
    wget --no-check-certificate -O "${BACKUP_FILE}" "${BACKUP_URL}"
    echo "  Download complete!"
fi

echo ""
echo "============================================"
echo "STEP 2b: Restoring AdventureWorks2019..."
echo "============================================"

# Restore the database from the .bak file
# MOVE clauses are required on Linux because the backup was created on Windows
# and the file paths in the .bak don't exist on Linux.
# We move the data and log files to /var/opt/mssql/data/ (standard Linux path).
${SQLCMD} -S localhost -U sa -P "${SA_PASSWORD}" -C -Q "
-- Use master database context for restore operations
USE [master];

-- Restore the AdventureWorks2019 database from the backup file
-- MOVE: redirect Windows file paths to Linux paths
-- REPLACE: overwrite if the database already exists
-- STATS=10: show progress every 10%
RESTORE DATABASE [AdventureWorks2019]
FROM DISK = '${BACKUP_FILE}'
WITH
    MOVE 'AdventureWorks2019'     TO '/var/opt/mssql/data/AdventureWorks2019.mdf',
    MOVE 'AdventureWorks2019_log' TO '/var/opt/mssql/data/AdventureWorks2019_log.ldf',
    REPLACE,
    STATS = 10;
"

echo "  Restore complete!"

# Verify the database exists and is online
echo ""
echo "  Verifying database is online..."
${SQLCMD} -S localhost -U sa -P "${SA_PASSWORD}" -C -Q "
-- List all databases to confirm AdventureWorks2019 is present
SELECT name, state_desc FROM sys.databases WHERE name = 'AdventureWorks2019';
"

# =============================================================================
# STEP 3: Enable CDC on the database and tables
# Run the enable-cdc.sql script which:
#   - Enables CDC at the database level
#   - Enables CDC on 5 key tables (Person, Customer, SalesOrderHeader,
#     SalesOrderDetail, Product)
#   - Verifies the CDC setup
# =============================================================================
echo ""
echo "============================================"
echo "STEP 3: Enabling CDC..."
echo "============================================"

# Run the SQL script that enables CDC
# -i flag reads SQL commands from a file
${SQLCMD} -S localhost -U sa -P "${SA_PASSWORD}" -C -i /scripts/enable-cdc.sql

echo ""
echo "============================================"
echo "SETUP COMPLETE!"
echo "============================================"
echo ""
echo "AdventureWorks2019 is restored and CDC is enabled on:"
echo "  - Person.Person"
echo "  - Sales.Customer"
echo "  - Sales.SalesOrderHeader"
echo "  - Sales.SalesOrderDetail"
echo "  - Production.Product"
echo ""
echo "You can now deploy the Debezium connector."
