# Snapshot Mode Configuration Guide

This document explains different snapshot strategies and when to use each.

---

## Strategy 1: Schema-Only (No Initial Snapshot)

**Use Case:** Only capture changes from NOW forward, ignore all historical data.

### Configuration Changes

Change line 42 in `connect/connector-config.json`:

```json
{
    "_comment_snapshot": "Schema-only mode - NO initial snapshot, only capture changes from deployment forward",
    "snapshot.mode": "schema_only"
}
```

### Deployment

```bash
# Stop and delete existing connector
./connect/deploy-connector.sh delete

# Redeploy with new config
./connect/deploy-connector.sh deploy

# Connector will start streaming immediately (no snapshot phase)
```

### Behavior

```
Timeline:
├─ Deploy Time: Capture table schemas, record LSN position
├─ Start streaming: Immediately capture INSERT/UPDATE/DELETE
└─ Historical data: NEVER captured (not in Kafka)

Example:
- Database has 10,000 existing orders
- You deploy connector at 2:00 PM
- Result: 10,000 orders NEVER appear in Kafka
- Only orders created/updated after 2:00 PM are captured
```

### When to Use

✅ Event logging systems (audit trail starting "now")  
✅ Real-time dashboards (historical data elsewhere)  
✅ Development/testing (don't want to wait for snapshot)  

❌ Data replication (need full copy of table)  
❌ Analytics (need historical data for trends)  

---

## Strategy 2: Filtered Initial Snapshot (Recommended for "Last Year" Requirement)

**Use Case:** Snapshot only specific data (e.g., last year's records), then stream all changes.

### Configuration Changes

Add to `connect/connector-config.json` after line 42:

```json
{
    "_comment_snapshot": "Initial snapshot mode with per-table filtering",
    "snapshot.mode": "initial",

    "_comment_snapshot_select": "Override SELECT statements for specific tables to filter snapshot data",
    "snapshot.select.statement.overrides": "Production.Product,Sales.SalesOrderHeader,Sales.SalesOrderDetail",

    "_comment_product_filter": "Only snapshot products sold in the last year",
    "snapshot.select.statement.overrides.Production.Product": "SELECT * FROM Production.Product WHERE SellStartDate >= DATEADD(year, -1, GETDATE())",

    "_comment_order_header_filter": "Only snapshot orders from the last year",
    "snapshot.select.statement.overrides.Sales.SalesOrderHeader": "SELECT * FROM Sales.SalesOrderHeader WHERE OrderDate >= DATEADD(year, -1, GETDATE())",

    "_comment_order_detail_filter": "Only snapshot order details for last year's orders (via JOIN)",
    "snapshot.select.statement.overrides.Sales.SalesOrderDetail": "SELECT sod.* FROM Sales.SalesOrderDetail sod INNER JOIN Sales.SalesOrderHeader soh ON sod.SalesOrderID = soh.SalesOrderID WHERE soh.OrderDate >= DATEADD(year, -1, GETDATE())"
}
```

### Important Notes

**Person.Person and Sales.Customer Not Filtered:**
- These tables don't have date columns, so they're fully snapshotted
- If you want to filter them, you need a JOIN to a table with dates:

```json
{
    "snapshot.select.statement.overrides.Sales.Customer": "SELECT DISTINCT c.* FROM Sales.Customer c INNER JOIN Sales.SalesOrderHeader soh ON c.CustomerID = soh.CustomerID WHERE soh.OrderDate >= DATEADD(year, -1, GETDATE())"
}
```

### Deployment

```bash
# Stop and delete existing connector
./connect/deploy-connector.sh delete

# Delete existing topics (they contain full snapshot data)
docker exec kafka kafka-topics --bootstrap-server kafka:29092 --delete --topic aw.AdventureWorks2019.Production.Product
docker exec kafka kafka-topics --bootstrap-server kafka:29092 --delete --topic aw.AdventureWorks2019.Sales.SalesOrderHeader
docker exec kafka kafka-topics --bootstrap-server kafka:29092 --delete --topic aw.AdventureWorks2019.Sales.SalesOrderDetail

# Redeploy with filtered config
./connect/deploy-connector.sh deploy

# Watch snapshot progress
docker logs kafka-connect -f | grep "Exported"
```

### Behavior

```
Timeline:
├─ Deploy Time: Run filtered SELECT for each table
│  ├─ Production.Product: 50 rows (only last year)
│  ├─ Sales.SalesOrderHeader: 5,000 rows (only last year)
│  └─ Sales.SalesOrderDetail: 15,000 rows (only last year)
│
├─ After Snapshot: Switch to streaming mode
│  └─ Capture ALL changes (even to records older than 1 year)
│
└─ Future: If someone updates a 2-year-old order
   └─ Change IS captured (streaming captures everything)
   └─ But "before" state might not be in Kafka (wasn't snapshotted)
```

### Pros and Cons

✅ **Pros:**
- Smaller snapshot (faster, less Kafka storage)
- Still get relevant historical data
- All future changes captured (even to old records)
- Clean, simple configuration

❌ **Cons:**
- Need to write custom SQL for each table
- WHERE clauses must be maintained
- If old record changes, "before" state unavailable (wasn't snapshotted)
- Must delete old topics before redeploying

---

## Strategy 3: Schema-Only + Filtered Incremental Snapshot (Most Flexible)

**Use Case:** Start streaming immediately, backfill last year's data on-demand.

### Configuration Changes

Change line 42 in `connect/connector-config.json`:

```json
{
    "_comment_snapshot": "Schema-only mode - NO initial snapshot, use incremental snapshot for backfill",
    "snapshot.mode": "schema_only",

    "_comment_signal_channels": "Enable signal channels for incremental snapshot",
    "signal.enabled.channels": "source,kafka",
    "signal.data.collection": "AdventureWorks2019.dbo.debezium_signal",
    "signal.kafka.topic": "aw-signal",
    "signal.kafka.bootstrap.servers": "kafka:29092",
    "signal.kafka.groupId": "debezium-signal-consumer",
    "incremental.snapshot.chunk.size": "1024"
}
```

### Deployment

```bash
# Stop and delete existing connector
./connect/deploy-connector.sh delete

# Redeploy with schema-only config
./connect/deploy-connector.sh deploy

# Connector starts streaming immediately (no snapshot phase)

# Wait a few seconds for streaming to start
sleep 10

# NOW trigger filtered incremental snapshots
./connect/send-signal.sh snapshot Production.Product "SellStartDate >= DATEADD(year, -1, GETDATE())"

./connect/send-signal.sh snapshot Sales.SalesOrderHeader "OrderDate >= DATEADD(year, -1, GETDATE())"

./connect/send-signal.sh snapshot Sales.SalesOrderDetail "SalesOrderID IN (SELECT SalesOrderID FROM Sales.SalesOrderHeader WHERE OrderDate >= DATEADD(year, -1, GETDATE()))"
```

### Behavior

```
Timeline:
T=0s:   Deploy connector (schema-only)
T=5s:   Connector is STREAMING (capturing live changes)
T=60s:  Send incremental snapshot signal
T=65s:  Backfill starts (WHILE streaming continues)
T=180s: Backfill completes

Kafka Topic Contents:
├─ Live changes from T=5s onwards (op:"c", op:"u", op:"d")
├─ Backfilled data from signal (op:"r")
└─ Events are INTERLEAVED (not in chronological order)
```

### Event Ordering Example

```json
// T=10s: Live INSERT
{"op":"c", "after":{"OrderID":12345, "OrderDate":"2026-02-23"}}

// T=70s: Backfill READ (2025 order)
{"op":"r", "after":{"OrderID":11111, "OrderDate":"2025-05-10"}}

// T=75s: Backfill READ (2025 order)
{"op":"r", "after":{"OrderID":11112, "OrderDate":"2025-06-15"}}

// T=80s: Live UPDATE (2026 order)
{"op":"u", "before":{...}, "after":{"OrderID":12345, "OrderDate":"2026-02-23"}}
```

**Important:** Consumer must handle out-of-order events!

### Pros and Cons

✅ **Pros:**
- Zero downtime (connector starts immediately)
- Maximum flexibility (backfill different ranges later)
- Can test streaming before backfilling
- Can run multiple incremental snapshots with different filters

❌ **Cons:**
- Most complex to orchestrate
- Events arrive out-of-order (backfill mixed with live)
- Need to track which snapshots completed
- Consumer must be order-agnostic

---

## Comparison Table

| Aspect | Strategy 1: Schema-Only | Strategy 2: Filtered Initial | Strategy 3: Schema + Incremental |
|--------|------------------------|----------------------------|----------------------------------|
| **Startup Time** | 5 seconds | 30-60 seconds (filtered snapshot) | 5 seconds (snapshot runs later) |
| **Historical Data** | None | Last year only | Last year (backfilled on-demand) |
| **Kafka Storage** | Minimal (only new changes) | Medium (filtered data) | Medium (filtered data) |
| **Configuration** | Simplest (change 1 line) | Medium (custom SQL per table) | Complex (signals + timing) |
| **Event Ordering** | Chronological | Chronological | Mixed (out-of-order) |
| **Use Case** | Forward-only logging | Typical CDC with filter | Advanced/flexible pipelines |

---

## Recommendation for Your Requirement

**"I want just the records of the last year"**

👉 **Use Strategy 2: Filtered Initial Snapshot**

### Why?

1. **Clean separation:** Snapshot phase → filtered data, Streaming phase → all changes
2. **Chronological order:** Events arrive in order (snapshot first, then streaming)
3. **Simple consumer logic:** No need to handle out-of-order events
4. **One-time setup:** Configure once, works forever

### Implementation Steps

1. **Backup current connector config:**
```bash
cp connect/connector-config.json connect/connector-config.json.backup
```

2. **Add filtered snapshot configuration** (see Strategy 2 above)

3. **Delete existing connector and topics:**
```bash
./connect/deploy-connector.sh delete
docker exec kafka kafka-topics --bootstrap-server kafka:29092 --delete --topic aw.AdventureWorks2019.Production.Product
docker exec kafka kafka-topics --bootstrap-server kafka:29092 --delete --topic aw.AdventureWorks2019.Sales.SalesOrderHeader
docker exec kafka kafka-topics --bootstrap-server kafka:29092 --delete --topic aw.AdventureWorks2019.Sales.SalesOrderDetail
```

4. **Redeploy:**
```bash
./connect/deploy-connector.sh deploy
```

5. **Verify filtered snapshot:**
```bash
# Check logs for snapshot progress
docker logs kafka-connect -f | grep "Exported"

# Expected: Lower row counts than full snapshot
# Example: "Exported 5,000 records" instead of "Exported 31,465 records"
```

6. **Test streaming works:**
```bash
# Insert a new order (should appear in Kafka)
docker exec sqlserver /opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -P 'YourStrong!Passw0rd' -Q "
USE AdventureWorks2019;
INSERT INTO Sales.SalesOrderHeader (...) VALUES (...);
" -C -b

# Consume from topic to see the INSERT
docker exec kafka kafka-console-consumer \
    --bootstrap-server kafka:29092 \
    --topic aw.AdventureWorks2019.Sales.SalesOrderHeader \
    --max-messages 1
```

---

## Common Pitfalls

### Pitfall 1: Forgetting to Delete Old Topics

**Problem:**
```bash
# Redeploy connector with filtered snapshot
./connect/deploy-connector.sh delete
./connect/deploy-connector.sh deploy

# Topics still contain OLD full snapshot data!
```

**Solution:**
Always delete topics before redeploying with different snapshot config:
```bash
docker exec kafka kafka-topics --bootstrap-server kafka:29092 --list | grep "^aw\." | xargs -I {} docker exec kafka kafka-topics --bootstrap-server kafka:29092 --delete --topic {}
```

### Pitfall 2: Filtering Tables Without Date Columns

**Problem:**
```json
{
    "snapshot.select.statement.overrides.Person.Person": "SELECT * FROM Person.Person WHERE ??? >= DATEADD(year, -1, GETDATE())"
}
```

Person.Person doesn't have a date column!

**Solution:**
Join to a table that does:
```json
{
    "snapshot.select.statement.overrides.Person.Person": "SELECT DISTINCT p.* FROM Person.Person p INNER JOIN Sales.SalesOrderHeader soh ON p.BusinessEntityID = soh.SalesPersonID WHERE soh.OrderDate >= DATEADD(year, -1, GETDATE())"
}
```

### Pitfall 3: SQL Syntax Errors

**Problem:**
```json
{
    "snapshot.select.statement.overrides.Sales.SalesOrderHeader": "SELECT * FROM Sales.SalesOrderHeader WHERE OrderDate >= '2025-01-01'"
}
```

Hardcoded dates get stale!

**Solution:**
Use SQL Server date functions:
```sql
-- Last 365 days
WHERE OrderDate >= DATEADD(day, -365, GETDATE())

-- Last calendar year
WHERE YEAR(OrderDate) = YEAR(GETDATE()) - 1

-- Since specific date (dynamic)
WHERE OrderDate >= DATEADD(year, -1, GETDATE())
```

---

## Testing Your Configuration

### Test 1: Verify Filtered Snapshot Size

```bash
# Count messages in topic (after snapshot completes)
docker exec kafka kafka-run-class kafka.tools.GetOffsetShell \
    --broker-list kafka:29092 \
    --topic aw.AdventureWorks2019.Sales.SalesOrderHeader | \
    awk -F':' '{sum += $3} END {print "Total messages:", sum}'

# Compare to SQL Server filtered count
docker exec sqlserver /opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -P 'YourStrong!Passw0rd' -Q "
USE AdventureWorks2019;
SELECT COUNT(*) AS FilteredCount FROM Sales.SalesOrderHeader WHERE OrderDate >= DATEADD(year, -1, GETDATE());
" -C -b

# Numbers should match!
```

### Test 2: Verify Streaming Captures All Changes

```bash
# Insert an OLD order (2 years ago)
docker exec sqlserver /opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -P 'YourStrong!Passw0rd' -Q "
USE AdventureWorks2019;
-- Insert with old date
INSERT INTO Sales.SalesOrderHeader (OrderDate, ...) VALUES ('2024-01-01', ...);
" -C -b

# Check if it appears in Kafka (it should!)
docker exec kafka kafka-console-consumer \
    --bootstrap-server kafka:29092 \
    --topic aw.AdventureWorks2019.Sales.SalesOrderHeader \
    --from-beginning | grep "2024-01-01"

# Expected: You see the INSERT event (op:"c") even though date is > 1 year ago
```

This proves streaming captures ALL changes regardless of filter!

---

## Summary

**Your Question:** "What if I don't want a full snapshot, just last year's records?"

**Answer:** Use **Strategy 2: Filtered Initial Snapshot**

**Configuration:**
```json
{
    "snapshot.mode": "initial",
    "snapshot.select.statement.overrides": "Sales.SalesOrderHeader,...",
    "snapshot.select.statement.overrides.Sales.SalesOrderHeader": "SELECT * FROM Sales.SalesOrderHeader WHERE OrderDate >= DATEADD(year, -1, GETDATE())"
}
```

**Result:**
- Initial snapshot: Only last year's data
- Streaming: ALL changes (even to older records)
- Clean, chronological event ordering
- Simple consumer logic

**Alternative:** If you truly want ZERO historical data, use `"snapshot.mode": "schema_only"`.
