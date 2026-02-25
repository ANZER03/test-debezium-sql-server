# Runbook — Migration Operations

## Pre-migration checklist

### SQL Server
- [ ] SQL Server Agent is running (`MSSQL_AGENT_ENABLED=true` in docker-compose)
- [ ] CDC enabled on database: `EXEC sys.sp_cdc_enable_db`
- [ ] Snapshot isolation enabled:
  ```sql
  ALTER DATABASE <db> SET READ_COMMITTED_SNAPSHOT ON;
  ALTER DATABASE <db> SET ALLOW_SNAPSHOT_ISOLATION ON;
  ```
- [ ] CDC enabled on each source table: `EXEC sys.sp_cdc_enable_table ...`
- [ ] `MSSQL_SA_PASSWORD` in `.env` matches the running SQL Server container

### PostgreSQL
- [ ] `wal_level=logical` confirmed: `SHOW wal_level;` → must return `logical`
- [ ] Publication exists: `SELECT * FROM pg_publication;`
- [ ] Replication slot does NOT already exist with the same name (would conflict):
  `SELECT slot_name FROM pg_replication_slots;`
- [ ] `max_replication_slots` >= number of planned connectors: `SHOW max_replication_slots;`

### Kafka / Connect
- [ ] All containers healthy: `./scripts/health-check.sh`
- [ ] Topics pre-created: `./topics/create-topics.sh both`
- [ ] Connect REST API reachable: `curl http://localhost:8083/connectors`

---

## Deploy migration

```bash
cd Production/

# 1. Configure environment
cp infra/.env.example infra/.env
# Edit infra/.env with real values

# 2. Full deploy (infra + topics + connectors)
./scripts/deploy.sh both          # both SQL Server and PostgreSQL
./scripts/deploy.sh sqlserver     # SQL Server only
./scripts/deploy.sh postgres      # PostgreSQL only
```

---

## Monitor progress

```bash
# Live dashboard (refreshes every 5s)
./scripts/monitor.sh

# Connector status only
curl -s http://localhost:8083/connectors/prod-sqlserver-migration/status | python3 -m json.tool
curl -s http://localhost:8083/connectors/prod-postgres-migration/status | python3 -m json.tool

# Consumer lag (requires a consumer group to be active)
docker exec kafka kafka-consumer-groups \
    --bootstrap-server kafka:29092 \
    --group <your-consumer-group> \
    --describe

# Topic log-end-offset (total messages produced by Debezium)
docker exec kafka kafka-run-class kafka.tools.GetOffsetShell \
    --bootstrap-server kafka:29092 \
    --topic prod_mssql.AdventureWorks2019.Sales.SalesOrderDetailBig \
    --time -1
```

---

## Handle a FAILED connector task

A task enters FAILED state when an unrecoverable error occurs (network timeout,
SQL Server restart, schema change, etc.). The connector state shows an error trace.

```bash
# 1. Check what went wrong
curl -s http://localhost:8083/connectors/prod-sqlserver-migration/status | python3 -m json.tool

# 2a. Restart the task (safe — resumes from last committed offset, no re-snapshot)
curl -X POST http://localhost:8083/connectors/prod-sqlserver-migration/tasks/0/restart

# 2b. If task restart fails, restart the whole connector
#     WARNING: for MSSQL this may trigger a re-snapshot. For PG it resumes from slot LSN.
curl -X POST http://localhost:8083/connectors/prod-sqlserver-migration/restart

# 2c. Last resort: delete and redeploy
#     This WILL trigger a full re-snapshot from the beginning.
curl -X DELETE http://localhost:8083/connectors/prod-sqlserver-migration
sleep 5
curl -X POST -H "Content-Type: application/json" \
    --data @connectors/sqlserver/connector.json \
    http://localhost:8083/connectors
```

---

## Verify migration completeness

After the snapshot phase completes (consumer lag = 0), verify the row count:

```bash
# Messages in Kafka topic (should equal row count in source table)
docker exec kafka kafka-run-class kafka.tools.GetOffsetShell \
    --bootstrap-server kafka:29092 \
    --topic prod_mssql.AdventureWorks2019.Sales.SalesOrderDetailBig \
    --time -1 | awk -F: '{sum += $3} END {print "Total messages:", sum}'

# Row count in SQL Server
docker exec sqlserver /opt/mssql-tools18/bin/sqlcmd \
    -S localhost -U sa -P "$MSSQL_SA_PASSWORD" -C \
    -Q "SELECT COUNT(*) FROM AdventureWorks2019.Sales.SalesOrderDetailBig"

# Row count in PostgreSQL
docker exec postgres psql -U postgres -d benchdb \
    -c "SELECT COUNT(*) FROM public.sales_order_detail_big;"
```

The three numbers should match.

---

## Switch connector to streaming CDC (post-migration)

After the snapshot is complete and verified, there are two options:

### Option A: Keep the connector running (continuous CDC)
The connector automatically transitions from snapshot to streaming mode.
No action needed — it is already streaming changes.

### Option B: Stop snapshot-mode connector, deploy streaming-only connector
```bash
# Delete the migration connector
curl -X DELETE http://localhost:8083/connectors/prod-sqlserver-migration

# Redeploy with snapshot.mode=never (uses existing Kafka offsets, no re-snapshot)
# Edit connectors/sqlserver/connector.json: change snapshot.mode to "never"
# Then redeploy:
curl -X POST -H "Content-Type: application/json" \
    --data @connectors/sqlserver/connector.json \
    http://localhost:8083/connectors
```

---

## Teardown

```bash
# Stop connectors + containers (keep data volumes)
./scripts/teardown.sh

# Stop everything and delete all data (use only to reset from scratch)
./scripts/teardown.sh --volumes

# Stop connectors only (leave infrastructure running)
./scripts/teardown.sh --connectors-only
```

---

## PostgreSQL replication slot management

**Critical:** always drop replication slots when done. An inactive slot prevents
PostgreSQL from recycling WAL segments, causing disk exhaustion.

```bash
# List active slots
docker exec postgres psql -U postgres -d benchdb \
    -c "SELECT slot_name, active, pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn)) AS lag FROM pg_replication_slots;"

# Drop a specific slot
docker exec postgres psql -U postgres -d benchdb \
    -c "SELECT pg_drop_replication_slot('debezium_prod');"
```

The `teardown.sh` script drops all slots automatically.

---

## Scaling to larger tables

For tables with more than 50M rows, use snapshot partitioning:

```json
{
  "snapshot.select.statement.overrides": "public.big_table",
  "snapshot.select.statement.overrides.public.big_table":
    "SELECT * FROM public.big_table WHERE id BETWEEN 1 AND 25000000"
}
```

Deploy one connector per key range with different `topic.prefix` and `slot.name`
values. They run in parallel and each handles its own partition of the data.

---

## Common errors

| Error | Cause | Fix |
|-------|-------|-----|
| `max.queue.size must be greater than max.batch.size` | queue <= batch | Ensure queue > batch in connector config |
| `invalid schema namespace` | topic.prefix contains hyphens | Use underscores only in topic.prefix |
| `replication slot already exists` | Previous connector was not cleaned up | `SELECT pg_drop_replication_slot('slot_name');` |
| `Avro: Unknown magic byte` | Consumer not using Avro deserializer | Configure consumer with `io.confluent.kafka.serializers.KafkaAvroDeserializer` |
| Connector stuck in `UNASSIGNED` | Connect worker not ready | Wait 60s after Connect startup; check `docker logs kafka-connect` |
| Task immediately FAILED with LSN error | PG slot corrupted | Delete connector, drop slot, redeploy |
