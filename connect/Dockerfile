FROM debezium/connect:2.3

# Confluent platform version — must be compatible with cp-schema-registry:7.7.7
ENV CONFLUENT_VERSION=7.7.7

# Download Confluent Avro converter JARs into a Kafka Connect plugin directory
# The Debezium image auto-discovers plugins in /kafka/connect/ subdirectories
RUN mkdir -p /kafka/connect/confluent-avro && \
    cd /kafka/connect/confluent-avro && \
    curl -fSL https://packages.confluent.io/maven/io/confluent/kafka-connect-avro-converter/${CONFLUENT_VERSION}/kafka-connect-avro-converter-${CONFLUENT_VERSION}.jar -O && \
    curl -fSL https://packages.confluent.io/maven/io/confluent/kafka-connect-avro-data/${CONFLUENT_VERSION}/kafka-connect-avro-data-${CONFLUENT_VERSION}.jar -O && \
    curl -fSL https://packages.confluent.io/maven/io/confluent/kafka-avro-serializer/${CONFLUENT_VERSION}/kafka-avro-serializer-${CONFLUENT_VERSION}.jar -O && \
    curl -fSL https://packages.confluent.io/maven/io/confluent/kafka-schema-serializer/${CONFLUENT_VERSION}/kafka-schema-serializer-${CONFLUENT_VERSION}.jar -O && \
    curl -fSL https://packages.confluent.io/maven/io/confluent/kafka-schema-registry-client/${CONFLUENT_VERSION}/kafka-schema-registry-client-${CONFLUENT_VERSION}.jar -O && \
    curl -fSL https://packages.confluent.io/maven/io/confluent/common-config/${CONFLUENT_VERSION}/common-config-${CONFLUENT_VERSION}.jar -O && \
    curl -fSL https://packages.confluent.io/maven/io/confluent/common-utils/${CONFLUENT_VERSION}/common-utils-${CONFLUENT_VERSION}.jar -O
