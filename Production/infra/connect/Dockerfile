# =============================================================================
# Dockerfile — Debezium Connect + Confluent Avro Converter
# =============================================================================
#
# Strategy: multi-stage build
#   Stage 1 (confluent-source): pull the official Confluent Kafka Connect image
#   solely to extract the Confluent Avro converter JARs (kafka-serde-tools).
#
#   Stage 2 (final): use the Debezium Connect 2.3 image as the base. Debezium
#   ships with all CDC connector plugins (SQL Server, PostgreSQL, MySQL, etc.)
#   pre-installed. We COPY the Avro JARs from stage 1 into the plugin path.
#
# Why this approach instead of downloading JARs at runtime:
#   - No network dependency at container start — image is self-contained
#   - Fully reproducible: both source image tags are pinned
#   - Avoids version drift between the Confluent Avro converter and the
#     Kafka client version inside the Debezium image
#   - Audit-friendly: the exact JARs baked into the image are inspectable
#     with `docker inspect` and `docker history`
#
# Pinned versions:
#   confluentinc/cp-kafka-connect:7.7.7 — matches the Kafka broker version
#   debezium/connect:2.3               — latest stable Debezium release
#     (Debezium 2.3 supports SQL Server 2017+, PostgreSQL 10+)
#
# Plugin discovery:
#   Debezium's connect image sets KAFKA_PLUGIN_PATH=/kafka/connect.
#   Any subdirectory placed there is auto-discovered as a plugin.
#   We copy the full kafka-serde-tools directory as a single plugin bundle
#   to ensure all transitive dependencies (Guava, Jackson, Avro core, etc.)
#   are present. Missing a transitive dependency causes ClassNotFound at
#   connector startup — copying the full directory avoids this entirely.
# =============================================================================

FROM confluentinc/cp-kafka-connect:7.7.7 AS confluent-source

FROM debezium/connect:2.3

# Copy the complete Confluent Avro serde-tools plugin directory.
# This includes: avro-*.jar, kafka-avro-serializer-*.jar,
# kafka-schema-registry-client-*.jar, and all transitive dependencies.
COPY --from=confluent-source /usr/share/java/kafka-serde-tools \
                             /kafka/connect/confluent-avro
