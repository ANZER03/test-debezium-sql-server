FROM confluentinc/cp-kafka-connect:7.7.7 AS confluent-source

# ─────────────────────────────────────────────────────────────────────────────
# Final image: Debezium Connect 2.3 + Confluent Avro Converter
# Strategy: copy the complete kafka-serde-tools directory from the official
# Confluent Kafka Connect image — this includes ALL transitive dependencies
# (Guava, Avro, Jackson, etc.) that the AvroConverter requires at runtime.
# ─────────────────────────────────────────────────────────────────────────────
FROM debezium/connect:2.3

# Copy the full Confluent serde-tools plugin directory into Kafka Connect's
# plugin path. The Debezium image auto-discovers converters in subdirectories
# of /kafka/connect/. Using the complete directory avoids missing-JAR errors.
COPY --from=confluent-source /usr/share/java/kafka-serde-tools /kafka/connect/confluent-avro
