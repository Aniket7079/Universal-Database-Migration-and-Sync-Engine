-- Target schema for the demo migration (PostgreSQL).
-- The migration engine COPIES data; it does not create tables, so the target
-- schema must exist first. Note the BOOLEAN columns: this is where MySQL's
-- TINYINT(1) values (1/0) get converted to true/false.

CREATE TABLE IF NOT EXISTS members (
    id         INTEGER PRIMARY KEY,
    username   VARCHAR(100) NOT NULL,
    email      VARCHAR(200) NOT NULL,
    phone      VARCHAR(20),
    is_active  BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS products (
    id          INTEGER PRIMARY KEY,
    name        VARCHAR(200) NOT NULL,
    price       NUMERIC(10,2),
    description TEXT,
    in_stock    BOOLEAN DEFAULT TRUE,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
