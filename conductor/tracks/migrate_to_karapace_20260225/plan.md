# Implementation Plan - Migrate to Karapace and implement Avro serialization

## Phase 1: Infrastructure Migration
- [ ] **Task: Replace Confluent Schema Registry with Karapace**
    - [ ] Update `docker-compose.yml` to use `aivenoy/karapace` image.
    - [ ] Configure environment variables for Karapace (port 8081).
    - [ ] Update Kafka UI configuration to point to Karapace.
- [ ] **Task: Verify Karapace Health**
    - [ ] Start the containers and verify Karapace is reachable at `http://localhost:8081`.
- [ ] **Task: Conductor - User Manual Verification 'Infrastructure Migration' (Protocol in workflow.md)**

## Phase 2: Connector Configuration
- [ ] **Task: Update Connect Image with Avro JARs**
    - [ ] Verify if the custom `connect/Dockerfile` already has the necessary Avro JARs.
    - [ ] Rebuild the Connect image if necessary.
- [ ] **Task: Reconfigure Debezium Connector for Avro**
    - [ ] Update `connect/connector-config.json` to use `io.confluent.connect.avro.AvroConverter`.
    - [ ] Point the converter to the Karapace registry URL.
- [ ] **Task: Deploy and Verify Connector**
    - [ ] Deploy the updated connector.
    - [ ] Verify that schemas are successfully registered in Karapace.
- [ ] **Task: Conductor - User Manual Verification 'Connector Configuration' (Protocol in workflow.md)**

## Phase 3: Python Client Updates
- [ ] **Task: Update Python Consumer for Avro**
    - [ ] Install `confluent-kafka[avro]` and `fastavro`.
    - [ ] Implement `AvroDeserializer` in `consumer/consumer.py`.
    - [ ] Update consumer logic to handle Avro-decoded payloads.
- [ ] **Task: Verify End-to-End Flow**
    - [ ] Run the producer to generate changes in SQL Server.
    - [ ] Run the updated consumer and verify it correctly processes Avro messages.
- [ ] **Task: Conductor - User Manual Verification 'Python Client Updates' (Protocol in workflow.md)**

## Phase 4: Final Validation
- [ ] **Task: Performance and Integrity Check**
    - [ ] Verify message sizes in Kafka UI (should be smaller than JSON).
    - [ ] Ensure all 5 CDC tables are correctly streaming Avro events.
- [ ] **Task: Conductor - User Manual Verification 'Final Validation' (Protocol in workflow.md)**
