# Tech Stack

## Data Source
- **SQL Server 2019:** Primary relational database with CDC enabled for transaction capture.

## Streaming & Integration
- **Apache Kafka (Confluent cp-kafka 7.7.7):** Message broker running in KRaft mode with a 2-node cluster.
- **Debezium (Kafka Connect 2.3):** Distributed CDC platform used to capture changes from SQL Server.
- **Karapace (Aiven):** Open-source, Apache 2.0 licensed schema registry compatible with the Confluent Schema Registry API.

## Languages & Libraries
- **Python 3.8+:** Primary language for producers and consumers.
- **confluent-kafka-python:** Kafka client with Avro support.
- **pymssql:** SQL Server client for the producer.

## Serialization
- **Apache Avro:** Binary serialization format for efficient and schema-safe message payloads.

## Infrastructure & Monitoring
- **Docker Compose:** Container orchestration for local development and testing.
- **Kafka UI (Provectus):** Web-based monitoring for topics, schemas, and connector status.
