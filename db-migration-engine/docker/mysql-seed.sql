-- Demo source data for testing migrations
USE source_db;

CREATE TABLE IF NOT EXISTS users (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    username   VARCHAR(100) NOT NULL,
    email      VARCHAR(200) NOT NULL,
    phone      VARCHAR(20),
    is_active  TINYINT(1) DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS products (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    name        VARCHAR(200) NOT NULL,
    price       DECIMAL(10,2),
    description LONGTEXT,
    in_stock    TINYINT(1) DEFAULT 1,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Insert sample data
INSERT INTO users (username, email, phone, is_active) VALUES
('alice_smith',   'alice@example.com',   '9876543210', 1),
('bob_jones',     'bob@example.com',     '8765432109', 1),
('charlie_brown', 'charlie@example.com', '7654321098', 0),
('diana_prince',  'diana@example.com',   '6543210987', 1),
('eve_wilson',    'eve@example.com',     '5432109876', 1);

INSERT INTO products (name, price, description, in_stock) VALUES
('Laptop Pro',     75000.00, 'High performance laptop',    1),
('Wireless Mouse', 1500.00,  'Ergonomic wireless mouse',   1),
('USB-C Hub',      2500.00,  '7-in-1 USB-C Hub',           0),
('Mechanical KB',  4500.00,  'Mechanical keyboard RGB',     1),
('Monitor 27"',    25000.00, '4K IPS 27 inch monitor',     1);
