# Specification - Migrate to Karapace and implement Avro serialization

## Overview
This track involves replacing the existing Confluent Schema Registry with Karapace (an open-source alternative) and transitioning the entire CDC pipeline (Debezium and Python clients) from JSON serialization to Avro serialization.

## Objectives
- Replace `cp-schema-registry` with `karapace` in `docker-compose.yml`.
- Reconfigure Debezium SQL Server connector to use `AvroConverter`.
- Update Python Producer to use Avro serialization for inserting data into SQL Server (if applicable, or simply ensure it doesn't break).
- Update Python Consumer to use `AvroDeserializer` for reading CDC events.
- Verify end-to-end data flow with Avro schemas in Karapace.

## Technical Details
- **Registry:** `aivenoy/karapace:latest`
- **Converter:** `io.confluent.connect.avro.AvroConverter` (requires ensuring JARs are present in the Connect image).
- **Python Libraries:** `confluent-kafka[avro]`, `fastavro`.

## Success Criteria
- Karapace is running and healthy.
- Debezium successfully registers schemas in Karapace.
- Python consumer can decode Avro messages using schemas from Karapace.
- No data loss during the transition.
- Kafka UI correctly displays Avro messages and Karapace schemas.
