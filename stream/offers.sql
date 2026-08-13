-- Phase 2: unify eight carrier feeds into one canonical `offers` stream.
--
-- Structure, and why:
--   * 8 CREATE STREAM declarations, one per raw topic. These only describe what
--     is already on the topic; they transform nothing.
--   * 1 CREATE STREAM for the `offers` target.
--   * 8 INSERT INTO statements - EXACTLY ONE TRANSFORMATION PER CARRIER, as the
--     project requires. Nothing is chained: each raw topic reaches `offers` in
--     a single hop. Chaining would multiply internal topics and state stores,
--     which a laptop-sized Docker stack cannot afford.
--
-- Every statement performs the same four jobs, differently each time because
-- every carrier publishes differently:
--   1. place codes   - city names and terminal names become LOS/ABV/ONI/PHC/LHR
--   2. timezone      - a departure becomes local time WITH an explicit offset
--   3. currency      - everything becomes NGN (kobo/100, GBP x rate)
--   4. identity      - offer_id is composed so repeated observations of one
--                      departure join together downstream
--
-- {{GBP_NGN_RATE}} is substituted at deploy time from FX_FALLBACK_GBP_NGN.
-- ksqlDB cannot call an FX API mid-stream, so the rate is baked in and the
-- statement is redeployed when it moves. See docs/decisions.md D-008.

SET 'auto.offset.reset' = 'earliest';

-- =========================================================== raw sources ===

CREATE STREAM IF NOT EXISTS raw_gigm (
    operator VARCHAR, from_city VARCHAR, to_city VARCHAR,
    dep_date VARCHAR, dep_time VARCHAR, fare VARCHAR, seats VARCHAR,
    travel_hours DOUBLE, booking_link VARCHAR, scraped_at VARCHAR
) WITH (KAFKA_TOPIC='raw.gigm', VALUE_FORMAT='JSON');

CREATE STREAM IF NOT EXISTS raw_abctransport (
    company VARCHAR, origin_terminal VARCHAR, destination_terminal VARCHAR,
    departs VARCHAR, amount DOUBLE, seats_available INT,
    journey_minutes INT, url VARCHAR, ts VARCHAR
) WITH (KAFKA_TOPIC='raw.abctransport', VALUE_FORMAT='JSON');

CREATE STREAM IF NOT EXISTS raw_crosscountry (
    carrier_code VARCHAR, start VARCHAR, `end` VARCHAR,
    epoch_departure BIGINT, price_kobo BIGINT, seats INT,
    duration_secs INT, deeplink VARCHAR, captured_at_epoch BIGINT
) WITH (KAFKA_TOPIC='raw.crosscountry', VALUE_FORMAT='JSON');

CREATE STREAM IF NOT EXISTS raw_chisco (
    line VARCHAR, board VARCHAR, alight VARCHAR, `when` VARCHAR,
    cost VARCHAR, open_seats INT, `hours` DOUBLE, link VARCHAR, pulled VARCHAR
) WITH (KAFKA_TOPIC='raw.chisco', VALUE_FORMAT='JSON');

CREATE STREAM IF NOT EXISTS raw_airpeace (
    airline VARCHAR, flight_number VARCHAR, dep_airport VARCHAR,
    arr_airport VARCHAR, std VARCHAR, cabin VARCHAR,
    total_fare STRUCT<amount DOUBLE, currency VARCHAR>,
    seats_remaining INT, duration_minutes INT, deep_link VARCHAR, fetched VARCHAR
) WITH (KAFKA_TOPIC='raw.airpeace', VALUE_FORMAT='JSON');

CREATE STREAM IF NOT EXISTS raw_ibomair (
    carrier VARCHAR, `from` VARCHAR, `to` VARCHAR, departure_utc VARCHAR,
    fare_ngn DOUBLE, seats INT, block_minutes INT,
    booking_url VARCHAR, as_of VARCHAR
) WITH (KAFKA_TOPIC='raw.ibomair', VALUE_FORMAT='JSON');

CREATE STREAM IF NOT EXISTS raw_arik (
    op_carrier VARCHAR, board_point VARCHAR, off_point VARCHAR,
    flight_date VARCHAR, dep_minutes_after_midnight INT,
    fare_amount DOUBLE, fare_currency VARCHAR, avail INT,
    elapsed_minutes INT, url VARCHAR, extracted_at VARCHAR
) WITH (KAFKA_TOPIC='raw.arik', VALUE_FORMAT='JSON');

CREATE STREAM IF NOT EXISTS raw_britishairways (
    `marketingCarrier` VARCHAR, origin VARCHAR, destination VARCHAR,
    `departureDateTime` VARCHAR, `cabinClass` VARCHAR, `totalPrice` DOUBLE,
    `currencyCode` VARCHAR, `availableSeats` INT,
    `journeyDurationMinutes` INT, `bookingUrl` VARCHAR, `timestamp` VARCHAR
) WITH (KAFKA_TOPIC='raw.britishairways', VALUE_FORMAT='JSON');

-- ====================================================== canonical target ===

-- offer_id is a VALUE column, not the Kafka message key: the canonical
-- contract defines it as a field in the message body.
--
-- VALUE_FORMAT is AVRO, not JSON. Kafka Connect's sinks need to know what a
-- record contains - the JDBC sink to map fields onto columns, the Redis sink
-- to locate a key. Schemaless JSON gives them neither, and both silently
-- wrote nothing. Avro registers the structure once with the Schema Registry
-- and every consumer gets it for free. See docs/decisions.md D-009.
CREATE STREAM IF NOT EXISTS offers (
    `offer_id` VARCHAR,
    `carrier` VARCHAR, `mode` VARCHAR, `origin` VARCHAR, `destination` VARCHAR,
    `depart_time` VARCHAR, `duration_min` INT, `price_ngn` DOUBLE,
    `seats_left` INT, `booking_url` VARCHAR, `fetched_at` VARCHAR
) WITH (KAFKA_TOPIC='offers', VALUE_FORMAT='AVRO', PARTITIONS=3, REPLICAS=1);

-- ============================================ one transformation per carrier

-- GIGM: DD-MM-YYYY date + separate HH:MM, "NGN 42,500", "12 left", city names.
INSERT INTO offers
SELECT
    CONCAT('gigm-road-',
           CASE WHEN from_city='Lagos' THEN 'LOS' WHEN from_city='Abuja' THEN 'ABV' WHEN from_city='Onitsha' THEN 'ONI' ELSE 'PHC' END,
           '-',
           CASE WHEN to_city='Lagos' THEN 'LOS' WHEN to_city='Abuja' THEN 'ABV' WHEN to_city='Onitsha' THEN 'ONI' ELSE 'PHC' END,
           '-',
           TIMESTAMPTOSTRING(
               STRINGTOTIMESTAMP(CONCAT(dep_date, ' ', dep_time),
                                 'dd-MM-yyyy HH:mm', 'Africa/Lagos'),
               'yyyyMMdd''T''HHmm''Z''', 'UTC')) AS `offer_id`,
    'GIGM' AS `carrier`,
    'road' AS `mode`,
    CASE WHEN from_city='Lagos' THEN 'LOS' WHEN from_city='Abuja' THEN 'ABV' WHEN from_city='Onitsha' THEN 'ONI' ELSE 'PHC' END AS `origin`,
    CASE WHEN to_city='Lagos' THEN 'LOS' WHEN to_city='Abuja' THEN 'ABV' WHEN to_city='Onitsha' THEN 'ONI' ELSE 'PHC' END AS `destination`,
    TIMESTAMPTOSTRING(
        STRINGTOTIMESTAMP(CONCAT(dep_date, ' ', dep_time),
                          'dd-MM-yyyy HH:mm', 'Africa/Lagos'),
        'yyyy-MM-dd''T''HH:mm:ssXXX', 'Africa/Lagos') AS `depart_time`,
    CAST(travel_hours * 60 AS INT) AS `duration_min`,
    CAST(REGEXP_REPLACE(fare, '[^0-9.]', '') AS DOUBLE) AS `price_ngn`,
    CAST(REGEXP_REPLACE(seats, '[^0-9]', '') AS INT) AS `seats_left`,
    booking_link AS `booking_url`,
    scraped_at AS `fetched_at`
FROM raw_gigm
WHERE fare IS NOT NULL AND dep_date IS NOT NULL;

-- ABC Transport: named terminals, single local datetime, numeric price.
INSERT INTO offers
SELECT
    CONCAT('abctransport-road-',
           CASE WHEN origin_terminal='LAGOS-JIBOWU' THEN 'LOS' WHEN origin_terminal='ABUJA-UTAKO' THEN 'ABV' WHEN origin_terminal='ONITSHA-UPPER-IWEKA' THEN 'ONI' ELSE 'PHC' END,
           '-',
           CASE WHEN destination_terminal='LAGOS-JIBOWU' THEN 'LOS' WHEN destination_terminal='ABUJA-UTAKO' THEN 'ABV' WHEN destination_terminal='ONITSHA-UPPER-IWEKA' THEN 'ONI' ELSE 'PHC' END,
           '-',
           TIMESTAMPTOSTRING(
               STRINGTOTIMESTAMP(departs, 'yyyy-MM-dd HH:mm:ss', 'Africa/Lagos'),
               'yyyyMMdd''T''HHmm''Z''', 'UTC')) AS `offer_id`,
    'ABC Transport' AS `carrier`,
    'road' AS `mode`,
    CASE WHEN origin_terminal='LAGOS-JIBOWU' THEN 'LOS' WHEN origin_terminal='ABUJA-UTAKO' THEN 'ABV' WHEN origin_terminal='ONITSHA-UPPER-IWEKA' THEN 'ONI' ELSE 'PHC' END AS `origin`,
    CASE WHEN destination_terminal='LAGOS-JIBOWU' THEN 'LOS' WHEN destination_terminal='ABUJA-UTAKO' THEN 'ABV' WHEN destination_terminal='ONITSHA-UPPER-IWEKA' THEN 'ONI' ELSE 'PHC' END AS `destination`,
    TIMESTAMPTOSTRING(
        STRINGTOTIMESTAMP(departs, 'yyyy-MM-dd HH:mm:ss', 'Africa/Lagos'),
        'yyyy-MM-dd''T''HH:mm:ssXXX', 'Africa/Lagos') AS `depart_time`,
    journey_minutes AS `duration_min`,
    amount AS `price_ngn`,
    seats_available AS `seats_left`,
    url AS `booking_url`,
    ts AS `fetched_at`
FROM raw_abctransport
WHERE amount IS NOT NULL AND departs IS NOT NULL;

-- Cross Country: epoch seconds, and price in KOBO - divide by 100 or every
-- fare is a hundred times too large.
INSERT INTO offers
SELECT
    CONCAT('crosscountry-road-',
           CASE WHEN start='Lagos' THEN 'LOS' WHEN start='Abuja' THEN 'ABV' WHEN start='Onitsha' THEN 'ONI' ELSE 'PHC' END,
           '-',
           CASE WHEN `end`='Lagos' THEN 'LOS' WHEN `end`='Abuja' THEN 'ABV' WHEN `end`='Onitsha' THEN 'ONI' ELSE 'PHC' END,
           '-',
           TIMESTAMPTOSTRING(epoch_departure * 1000,
                             'yyyyMMdd''T''HHmm''Z''', 'UTC')) AS `offer_id`,
    'Cross Country' AS `carrier`,
    'road' AS `mode`,
    CASE WHEN start='Lagos' THEN 'LOS' WHEN start='Abuja' THEN 'ABV' WHEN start='Onitsha' THEN 'ONI' ELSE 'PHC' END AS `origin`,
    CASE WHEN `end`='Lagos' THEN 'LOS' WHEN `end`='Abuja' THEN 'ABV' WHEN `end`='Onitsha' THEN 'ONI' ELSE 'PHC' END AS `destination`,
    TIMESTAMPTOSTRING(epoch_departure * 1000,
                      'yyyy-MM-dd''T''HH:mm:ssXXX', 'Africa/Lagos') AS `depart_time`,
    CAST(duration_secs / 60 AS INT) AS `duration_min`,
    CAST(price_kobo AS DOUBLE) / 100.0 AS `price_ngn`,
    seats AS `seats_left`,
    deeplink AS `booking_url`,
    TIMESTAMPTOSTRING(captured_at_epoch * 1000,
                      'yyyy-MM-dd''T''HH:mm:ss''Z''', 'UTC') AS `fetched_at`
FROM raw_crosscountry
WHERE price_kobo IS NOT NULL AND epoch_departure IS NOT NULL;

-- CHISCO: DD/MM/YYYY HH:MM and "30,000.00".
INSERT INTO offers
SELECT
    CONCAT('chisco-road-',
           CASE WHEN board='Lagos' THEN 'LOS' WHEN board='Abuja' THEN 'ABV' WHEN board='Onitsha' THEN 'ONI' ELSE 'PHC' END,
           '-',
           CASE WHEN alight='Lagos' THEN 'LOS' WHEN alight='Abuja' THEN 'ABV' WHEN alight='Onitsha' THEN 'ONI' ELSE 'PHC' END,
           '-',
           TIMESTAMPTOSTRING(
               STRINGTOTIMESTAMP(`when`, 'dd/MM/yyyy HH:mm', 'Africa/Lagos'),
               'yyyyMMdd''T''HHmm''Z''', 'UTC')) AS `offer_id`,
    'CHISCO' AS `carrier`,
    'road' AS `mode`,
    CASE WHEN board='Lagos' THEN 'LOS' WHEN board='Abuja' THEN 'ABV' WHEN board='Onitsha' THEN 'ONI' ELSE 'PHC' END AS `origin`,
    CASE WHEN alight='Lagos' THEN 'LOS' WHEN alight='Abuja' THEN 'ABV' WHEN alight='Onitsha' THEN 'ONI' ELSE 'PHC' END AS `destination`,
    TIMESTAMPTOSTRING(
        STRINGTOTIMESTAMP(`when`, 'dd/MM/yyyy HH:mm', 'Africa/Lagos'),
        'yyyy-MM-dd''T''HH:mm:ssXXX', 'Africa/Lagos') AS `depart_time`,
    CAST(`hours` * 60 AS INT) AS `duration_min`,
    CAST(REGEXP_REPLACE(cost, '[^0-9.]', '') AS DOUBLE) AS `price_ngn`,
    open_seats AS `seats_left`,
    link AS `booking_url`,
    pulled AS `fetched_at`
FROM raw_chisco
WHERE cost IS NOT NULL AND `when` IS NOT NULL;

-- Air Peace: ISO-8601 with offset, fare nested inside an object.
INSERT INTO offers
SELECT
    CONCAT('airpeace-air-', dep_airport, '-', arr_airport, '-',
           TIMESTAMPTOSTRING(
               STRINGTOTIMESTAMP(std, 'yyyy-MM-dd''T''HH:mm:ssXXX'),
               'yyyyMMdd''T''HHmm''Z''', 'UTC')) AS `offer_id`,
    'Air Peace' AS `carrier`,
    'air' AS `mode`,
    dep_airport AS `origin`,
    arr_airport AS `destination`,
    TIMESTAMPTOSTRING(
        STRINGTOTIMESTAMP(std, 'yyyy-MM-dd''T''HH:mm:ssXXX'),
        'yyyy-MM-dd''T''HH:mm:ssXXX', 'Africa/Lagos') AS `depart_time`,
    duration_minutes AS `duration_min`,
    total_fare->amount AS `price_ngn`,
    seats_remaining AS `seats_left`,
    deep_link AS `booking_url`,
    fetched AS `fetched_at`
FROM raw_airpeace
WHERE total_fare IS NOT NULL AND std IS NOT NULL;

-- Ibom Air: publishes departure in UTC, so it must be put BACK into local
-- time or every Ibom Air departure displays an hour early.
INSERT INTO offers
SELECT
    CONCAT('ibomair-air-', `from`, '-', `to`, '-',
           TIMESTAMPTOSTRING(
               STRINGTOTIMESTAMP(departure_utc, 'yyyy-MM-dd''T''HH:mm:ss''Z''', 'UTC'),
               'yyyyMMdd''T''HHmm''Z''', 'UTC')) AS `offer_id`,
    'Ibom Air' AS `carrier`,
    'air' AS `mode`,
    `from` AS `origin`,
    `to` AS `destination`,
    TIMESTAMPTOSTRING(
        STRINGTOTIMESTAMP(departure_utc, 'yyyy-MM-dd''T''HH:mm:ss''Z''', 'UTC'),
        'yyyy-MM-dd''T''HH:mm:ssXXX', 'Africa/Lagos') AS `depart_time`,
    block_minutes AS `duration_min`,
    fare_ngn AS `price_ngn`,
    seats AS `seats_left`,
    booking_url AS `booking_url`,
    as_of AS `fetched_at`
FROM raw_ibomair
WHERE fare_ngn IS NOT NULL AND departure_utc IS NOT NULL;

-- Arik Air: date and time-of-day arrive separately, the latter as minutes
-- past midnight, so the departure has to be reassembled arithmetically.
INSERT INTO offers
SELECT
    CONCAT('arik-air-', board_point, '-', off_point, '-',
           TIMESTAMPTOSTRING(
               STRINGTOTIMESTAMP(flight_date, 'yyyy-MM-dd', 'Africa/Lagos')
                   + dep_minutes_after_midnight * 60000,
               'yyyyMMdd''T''HHmm''Z''', 'UTC')) AS `offer_id`,
    'Arik Air' AS `carrier`,
    'air' AS `mode`,
    board_point AS `origin`,
    off_point AS `destination`,
    TIMESTAMPTOSTRING(
        STRINGTOTIMESTAMP(flight_date, 'yyyy-MM-dd', 'Africa/Lagos')
            + dep_minutes_after_midnight * 60000,
        'yyyy-MM-dd''T''HH:mm:ssXXX', 'Africa/Lagos') AS `depart_time`,
    elapsed_minutes AS `duration_min`,
    fare_amount AS `price_ngn`,
    avail AS `seats_left`,
    url AS `booking_url`,
    extracted_at AS `fetched_at`
FROM raw_arik
WHERE fare_amount IS NOT NULL AND flight_date IS NOT NULL;

-- British Airways: the only carrier quoting a foreign currency, and the only
-- one whose origin may sit in a different timezone (LHR). Both are handled
-- here rather than downstream, so `offers` is uniformly NGN and local-time.
INSERT INTO offers
SELECT
    CONCAT('britishairways-air-', origin, '-', destination, '-',
           TIMESTAMPTOSTRING(
               STRINGTOTIMESTAMP(`departureDateTime`, 'yyyy-MM-dd''T''HH:mm:ssXXX'),
               'yyyyMMdd''T''HHmm''Z''', 'UTC')) AS `offer_id`,
    'British Airways' AS `carrier`,
    'air' AS `mode`,
    origin AS `origin`,
    destination AS `destination`,
    TIMESTAMPTOSTRING(
        STRINGTOTIMESTAMP(`departureDateTime`, 'yyyy-MM-dd''T''HH:mm:ssXXX'),
        'yyyy-MM-dd''T''HH:mm:ssXXX',
        CASE WHEN origin='LHR' THEN 'Europe/London' ELSE 'Africa/Lagos' END
    ) AS `depart_time`,
    `journeyDurationMinutes` AS `duration_min`,
    `totalPrice` * {{GBP_NGN_RATE}} AS `price_ngn`,
    `availableSeats` AS `seats_left`,
    `bookingUrl` AS `booking_url`,
    `timestamp` AS `fetched_at`
FROM raw_britishairways
WHERE `totalPrice` IS NOT NULL AND `departureDateTime` IS NOT NULL;
