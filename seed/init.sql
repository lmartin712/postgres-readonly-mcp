-- Fictional demo data only. Every name, company and number below is invented.

CREATE TABLE customers (
    id          serial PRIMARY KEY,
    name        text NOT NULL,
    city        text NOT NULL,
    created_at  date NOT NULL
);

CREATE TABLE invoices (
    id            serial PRIMARY KEY,
    customer_id   integer NOT NULL REFERENCES customers(id),
    issued_on     date NOT NULL,
    status        text NOT NULL CHECK (status IN ('open', 'paid', 'overdue'))
);

CREATE TABLE invoice_lines (
    id           serial PRIMARY KEY,
    invoice_id   integer NOT NULL REFERENCES invoices(id),
    description  text NOT NULL,
    quantity     integer NOT NULL,
    unit_price   numeric(12, 2) NOT NULL
);

-- Deliberately NOT allow-listed: used to demo rejection.
CREATE TABLE staff_salaries (
    id          serial PRIMARY KEY,
    employee    text NOT NULL,
    monthly_pay numeric(12, 2) NOT NULL
);

INSERT INTO customers (name, city, created_at) VALUES
    ('Acme Kft.',           'Exampleville', '2024-01-15'),
    ('Example Ltd.',        'Sampletown',   '2024-03-02'),
    ('Foo Bar Bt.',         'Exampleville', '2024-06-20'),
    ('Placeholder Zrt.',    'Testburg',     '2025-02-11');

INSERT INTO invoices (customer_id, issued_on, status) VALUES
    (1, '2025-01-10', 'paid'),
    (1, '2025-02-10', 'paid'),
    (1, '2025-03-10', 'open'),
    (2, '2025-02-01', 'overdue'),
    (2, '2025-03-01', 'open'),
    (3, '2025-03-15', 'paid'),
    (4, '2025-04-01', 'open');

INSERT INTO invoice_lines (invoice_id, description, quantity, unit_price) VALUES
    (1, 'Widget, standard',   10,  120.00),
    (1, 'Delivery',            1,   45.00),
    (2, 'Widget, standard',   20,  115.00),
    (3, 'Gadget, large',       2,  980.00),
    (4, 'Consulting hour',     8,  160.00),
    (5, 'Consulting hour',    12,  160.00),
    (5, 'Travel',              1,   80.00),
    (6, 'Gadget, small',       5,  210.00),
    (7, 'Widget, standard',   40,  110.00);

INSERT INTO staff_salaries (employee, monthly_pay) VALUES
    ('Employee A', 1000.00),
    ('Employee B', 1200.00);

-- Outermost safety layer: a role that can only SELECT the allow-listed tables.
CREATE ROLE mcp_reader LOGIN PASSWORD 'demo_reader_pw';
GRANT CONNECT ON DATABASE invoicing_demo TO mcp_reader;
GRANT USAGE ON SCHEMA public TO mcp_reader;
GRANT SELECT ON customers, invoices, invoice_lines TO mcp_reader;
-- staff_salaries is intentionally not granted.
ALTER ROLE mcp_reader SET default_transaction_read_only = on;
