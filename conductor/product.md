# Product Guide - SQL Server CDC Pipeline (Karapace Edition)

## Initial Concept
A robust, high-performance CDC pipeline streaming SQL Server changes to Kafka, utilizing Karapace for open-source schema management and Avro for efficient serialization.

## Vision
To provide a scalable and maintainable data streaming architecture that leverages open-source components (Debezium, Karapace, Kafka) to deliver real-time database events to downstream consumers with minimal latency and high reliability.

## Target Audience
- **Data Engineers:** Focused on building high-throughput, low-latency data pipelines.
- **App Developers:** Focused on consuming structured, schema-validated events for downstream services.
- **DevOps/SRE:** Focused on system stability, observability, and cost-effective open-source licensing.

## Strategic Goals
1. **Performance Tuning:** Optimize the end-to-end pipeline (SQL Server -> Debezium -> Kafka -> Karapace -> Consumer) for maximum throughput and reliability.
2. **Open-Source Compliance:** Migrate from proprietary components (Confluent Schema Registry) to fully open-source alternatives like Karapace.
3. **Data Integrity:** Transition to Avro serialization to ensure strict schema enforcement and reduce message payload size.

## Key Features
- **Karapace Integration:** Replace Confluent Schema Registry with Karapace for Apache 2.0 licensed schema management.
- **Avro Transition:** Implement Avro serialization across the pipeline (Debezium and Python clients) to improve performance and schema governance.
- **Enhanced Monitoring:** Utilize Kafka UI and Karapace APIs to monitor schema evolution and connector health.

## Constraints & Principles
- **Zero Downtime Source:** Ensure operations on SQL Server remain unaffected by the CDC process.
- **No Data Loss:** Guarantee at-least-once delivery of all database change events.
- **Open-Source First:** Prioritize Apache-licensed tools and libraries to avoid vendor lock-in and licensing costs.
