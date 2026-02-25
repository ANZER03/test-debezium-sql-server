# Product Guidelines

## Development Philosophy
- **Modular Design:** Keep components (database, connector, registry, consumers) decoupled and independently scalable.
- **Fail-Fast:** Implement rigorous health checks and monitoring to detect and alert on pipeline failures immediately.
- **Data as Code:** Treat schemas as first-class citizens; all changes must be versioned and validated against the registry.

## Technical Standards
- **Serialization:** Prefer Avro for all production topics to ensure data consistency and performance.
- **Idempotency:** Consumers must be designed to handle duplicate messages to ensure "at-least-once" processing safety.
- **Monitoring:** All components must expose metrics for throughput, latency, and error rates.

## Communication Style
- **Documentation:** Maintain clear, concise `README` files for each component.
- **Logging:** Use structured logging (JSON preferred) to facilitate centralized log analysis.
- **Versioning:** Use Semantic Versioning (SemVer) for all internal libraries and schema updates.

## Performance & Scalability
- **Batching:** Optimize Kafka producer and consumer batch sizes to balance throughput and latency.
- **Resource Management:** Ensure Docker containers are configured with appropriate CPU and memory limits.
- **Parallelism:** Leverage Kafka partitions to scale consumer processing horizontally.
