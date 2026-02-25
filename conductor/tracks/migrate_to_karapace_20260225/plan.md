# Implementation Plan - Migrate to Karapace and implement Avro serialization

## Phase 1: Infrastructure Migration
- [x] **Task: Replace Confluent Schema Registry with Karapace**
    - [x] Update `docker-compose.yml` to use `ghcr.io/aiven-open/karapace:latest` image.
    - [x] Configure environment variables for Karapace (port 8081). Fixed: `KARAPACE_HOST`/`KARAPACE_PORT` (were incorrectly set as `KARAPACE_REGISTRY_HOST`/`KARAPACE_REGISTRY_PORT`). Fixed: command changed from `/venv/bin/karapace` (broken) to `python3 -m karapace`.
    - [x] Update Kafka UI configuration to point to Karapace (`KAFKA_CLUSTERS_0_SCHEMAREGISTRY: http://schema-registry:8081`).
- [x] **Task: Verify Karapace Health**
    - [x] Start the containers and verify Karapace is reachable at `http://localhost:8081`. Confirmed: `GET /` returns `{}`, `GET /subjects` returns `[]` (empty on fresh start).
- [ ] **Task: Conductor - User Manual Verification 'Infrastructure Migration' (Protocol in workflow.md)**

## Phase 2: Connector Configuration
- [x] **Task: Update Connect Image with Avro JARs**
    - [x] Verified: `connect/Dockerfile` copies `kafka-serde-tools` from `confluentinc/cp-kafka-connect:7.7.7` — all Avro JARs present.
    - [ ] Rebuild the Connect image if necessary (required after any Dockerfile changes).
- [x] **Task: Reconfigure Debezium Connector for Avro**
    - [x] `connect/connector-config.json` already uses `io.confluent.connect.avro.AvroConverter` for both key and value.
    - [x] Converter points to `http://schema-registry:8081` (Karapace).
- [x] **Task: Deploy and Verify Connector**
    - [x] Deploy the updated connector via `./connect/deploy-connector.sh` — HTTP 201, state RUNNING, task RUNNING.
    - [x] Verify that schemas are successfully registered in Karapace — all 10 subjects present (key+value for all 5 tables).
- [ ] **Task: Conductor - User Manual Verification 'Connector Configuration' (Protocol in workflow.md)**

## Phase 3: Python Client Updates
- [x] **Task: Update Python Consumer for Avro**
    - [x] `consumer/requirements.txt` has `confluent-kafka[avro]>=2.3.0` (includes `fastavro`).
    - [x] `consumer/consumer.py` fully implements `AvroDeserializer` with `SchemaRegistryClient`.
    - [x] Consumer logic handles Avro-decoded payloads (dict), including bytes-encoded decimal fields.
- [x] **Task: Verify End-to-End Flow**
    - [x] Run the producer to generate changes in SQL Server (snapshot data used for verification).
    - [x] Run the updated consumer and verify it correctly processes Avro messages — 10 SNAPSHOT events from SalesOrderDetail decoded correctly with `Decimal` money fields.
- [ ] **Task: Conductor - User Manual Verification 'Python Client Updates' (Protocol in workflow.md)**

## Phase 4: Final Validation
- [x] **Task: Performance and Integrity Check**
    - [x] Verified message sizes in Kafka UI — Avro binary format confirmed (Decimal fields encoded as compact bytes, not JSON strings).
    - [x] All 5 CDC tables correctly streaming Avro events: Person.Person, Sales.Customer, Sales.SalesOrderHeader, Sales.SalesOrderDetail, Production.Product.
- [ ] **Task: Conductor - User Manual Verification 'Final Validation' (Protocol in workflow.md)**
