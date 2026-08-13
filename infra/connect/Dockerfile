# Kafka Connect with the two sink connectors this project needs.
#
# The base cp-kafka-connect image ships no connectors, so they are installed at
# build time rather than downloaded on every container start.
FROM confluentinc/cp-kafka-connect:7.6.1

# offers -> PostgreSQL (append-only price history, the ML training set)
RUN confluent-hub install --no-prompt confluentinc/kafka-connect-jdbc:10.7.6

# offers -> Redis (latest price per offer, what makes /search fast)
RUN confluent-hub install --no-prompt redis/redis-enterprise-kafka:6.7.4
