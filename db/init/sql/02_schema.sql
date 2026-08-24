-- ---------------------------------------------------------------------------
-- Schemas and analytical tables.
--
-- Schema analytics : the only schema agent-generated SQL may read (control C3).
--                    Mirrors the Olist Brazilian E-commerce dataset faithfully,
--                    including its original column spellings, so real Kaggle CSVs
--                    load without transformation.
-- Schema agent     : the service's own state. Created here, populated in Step 3.
--                    analyst_ro is never granted anything on it.
--
-- Sensitive columns are listed and flagged rather than dropped, so that the column
-- policy itself is reviewable and testable (control C4). The customer_contact table
-- exists to give that policy a real surface: the synthetic generator populates it,
-- and the Kaggle path leaves it empty (Olist ships no direct identifiers).
-- ---------------------------------------------------------------------------

CREATE SCHEMA IF NOT EXISTS analytics;
CREATE SCHEMA IF NOT EXISTS agent;

SET search_path = analytics, public;

-- --- Dimensions ------------------------------------------------------------

CREATE TABLE analytics.customers (
    customer_id                TEXT PRIMARY KEY,
    customer_unique_id         TEXT NOT NULL,
    customer_zip_code_prefix   TEXT,
    customer_city              TEXT,
    customer_state             CHAR(2)
);
COMMENT ON TABLE  analytics.customers IS
    'One row per order-scoped customer key. customer_unique_id identifies the person across orders.';
COMMENT ON COLUMN analytics.customers.customer_unique_id IS
    'SENSITIVE: person-level identifier. Aggregates allowed, projection blocked.';

-- Direct identifiers, kept in their own table so the grant and the policy are both obvious.
CREATE TABLE analytics.customer_contact (
    customer_id      TEXT PRIMARY KEY REFERENCES analytics.customers(customer_id),
    full_name        TEXT,
    email            TEXT,
    phone            TEXT,
    street_address   TEXT
);
COMMENT ON TABLE analytics.customer_contact IS
    'SENSITIVE: direct identifiers. Every column is projection-blocked by column_policy.py; '
    'approved aggregates such as count(distinct email) are permitted. Empty when the real '
    'Olist CSVs are loaded, since that dataset ships no direct identifiers.';

CREATE TABLE analytics.sellers (
    seller_id                TEXT PRIMARY KEY,
    seller_zip_code_prefix   TEXT,
    seller_city              TEXT,
    seller_state             CHAR(2)
);

CREATE TABLE analytics.products (
    product_id                    TEXT PRIMARY KEY,
    product_category_name         TEXT,
    product_name_lenght           INTEGER,   -- original Olist spelling, kept deliberately
    product_description_lenght    INTEGER,   -- original Olist spelling, kept deliberately
    product_photos_qty            INTEGER,
    product_weight_g              INTEGER,
    product_length_cm             INTEGER,
    product_height_cm             INTEGER,
    product_width_cm              INTEGER
);

CREATE TABLE analytics.product_category_name_translation (
    product_category_name           TEXT PRIMARY KEY,
    product_category_name_english   TEXT NOT NULL
);

CREATE TABLE analytics.geolocation (
    geolocation_zip_code_prefix   TEXT NOT NULL,
    geolocation_lat               NUMERIC(10, 7),
    geolocation_lng               NUMERIC(10, 7),
    geolocation_city              TEXT,
    geolocation_state             CHAR(2)
);
COMMENT ON COLUMN analytics.geolocation.geolocation_lat IS
    'SENSITIVE: precise location. Projection blocked; aggregation to state or city allowed.';
COMMENT ON COLUMN analytics.geolocation.geolocation_lng IS
    'SENSITIVE: precise location. Projection blocked; aggregation to state or city allowed.';

-- --- Facts -----------------------------------------------------------------

CREATE TABLE analytics.orders (
    order_id                         TEXT PRIMARY KEY,
    customer_id                      TEXT NOT NULL REFERENCES analytics.customers(customer_id),
    order_status                     TEXT NOT NULL,
    order_purchase_timestamp         TIMESTAMP NOT NULL,
    order_approved_at                TIMESTAMP,
    order_delivered_carrier_date     TIMESTAMP,
    order_delivered_customer_date    TIMESTAMP,
    order_estimated_delivery_date    TIMESTAMP
);
COMMENT ON COLUMN analytics.orders.order_status IS
    'delivered | shipped | canceled | unavailable | invoiced | processing | created | approved';

CREATE TABLE analytics.order_items (
    order_id              TEXT NOT NULL REFERENCES analytics.orders(order_id),
    order_item_id         INTEGER NOT NULL,
    product_id            TEXT NOT NULL REFERENCES analytics.products(product_id),
    seller_id             TEXT NOT NULL REFERENCES analytics.sellers(seller_id),
    shipping_limit_date   TIMESTAMP,
    price                 NUMERIC(12, 2) NOT NULL,
    freight_value         NUMERIC(12, 2) NOT NULL,
    PRIMARY KEY (order_id, order_item_id)
);
COMMENT ON TABLE analytics.order_items IS
    'One row per item line. Order revenue is sum(price); freight is tracked separately, which is '
    'why the approved revenue metric excludes it and the freight ratio metric exists.';

CREATE TABLE analytics.payments (
    order_id               TEXT NOT NULL REFERENCES analytics.orders(order_id),
    payment_sequential     INTEGER NOT NULL,
    payment_type           TEXT NOT NULL,
    payment_installments   INTEGER,
    payment_value          NUMERIC(12, 2) NOT NULL,
    PRIMARY KEY (order_id, payment_sequential)
);
COMMENT ON TABLE analytics.payments IS
    'An order may have several payment rows. Summing payment_value across an order and then '
    'across orders double counts nothing, but joining payments to order_items without '
    'aggregating first does — a common source of wrong-but-plausible SQL.';

CREATE TABLE analytics.reviews (
    review_id                 TEXT NOT NULL,
    order_id                  TEXT NOT NULL REFERENCES analytics.orders(order_id),
    review_score              SMALLINT NOT NULL CHECK (review_score BETWEEN 1 AND 5),
    review_comment_title      TEXT,
    review_comment_message    TEXT,
    review_creation_date      TIMESTAMP,
    review_answer_timestamp   TIMESTAMP,
    PRIMARY KEY (review_id, order_id)
);
COMMENT ON COLUMN analytics.reviews.review_comment_message IS
    'Free text written by customers. Treated strictly as data, never as instructions '
    '(control C6, prompt-injection containment).';

-- --- Helper dimension ------------------------------------------------------

CREATE TABLE analytics.dim_date (
    date_key      DATE PRIMARY KEY,
    year          SMALLINT NOT NULL,
    quarter       SMALLINT NOT NULL,
    month         SMALLINT NOT NULL,
    month_name    TEXT NOT NULL,
    year_month    TEXT NOT NULL,
    day           SMALLINT NOT NULL,
    day_of_week   SMALLINT NOT NULL,
    iso_week      SMALLINT NOT NULL,
    is_weekend    BOOLEAN NOT NULL
);
COMMENT ON TABLE analytics.dim_date IS
    'Continuous calendar, so period-over-period comparisons do not silently skip empty periods.';

-- --- Indexes ---------------------------------------------------------------

CREATE INDEX idx_orders_purchase_ts    ON analytics.orders (order_purchase_timestamp);
CREATE INDEX idx_orders_status         ON analytics.orders (order_status);
CREATE INDEX idx_orders_customer       ON analytics.orders (customer_id);
CREATE INDEX idx_order_items_product   ON analytics.order_items (product_id);
CREATE INDEX idx_order_items_seller    ON analytics.order_items (seller_id);
CREATE INDEX idx_payments_type         ON analytics.payments (payment_type);
CREATE INDEX idx_reviews_order         ON analytics.reviews (order_id);
CREATE INDEX idx_reviews_score         ON analytics.reviews (review_score);
CREATE INDEX idx_customers_unique      ON analytics.customers (customer_unique_id);
CREATE INDEX idx_customers_state       ON analytics.customers (customer_state);
CREATE INDEX idx_products_category     ON analytics.products (product_category_name);
CREATE INDEX idx_geolocation_zip       ON analytics.geolocation (geolocation_zip_code_prefix);
CREATE INDEX idx_dim_date_year_month   ON analytics.dim_date (year_month);

-- --- Convenience view used by several approved metrics ---------------------

CREATE VIEW analytics.v_order_revenue AS
SELECT
    o.order_id,
    o.customer_id,
    o.order_status,
    o.order_purchase_timestamp,
    o.order_purchase_timestamp::date                                   AS purchase_date,
    to_char(o.order_purchase_timestamp, 'YYYY-MM')                     AS year_month,
    o.order_delivered_customer_date,
    o.order_estimated_delivery_date,
    COUNT(oi.order_item_id)                                            AS item_count,
    COALESCE(SUM(oi.price), 0)                                         AS item_revenue,
    COALESCE(SUM(oi.freight_value), 0)                                 AS freight_revenue,
    COALESCE(SUM(oi.price), 0) + COALESCE(SUM(oi.freight_value), 0)    AS gross_revenue
FROM analytics.orders o
LEFT JOIN analytics.order_items oi ON oi.order_id = o.order_id
GROUP BY o.order_id;

COMMENT ON VIEW analytics.v_order_revenue IS
    'Order grain with items pre-aggregated. Exists so metric templates do not have to repeat the '
    'aggregate-before-join step that is easy to get wrong.';
