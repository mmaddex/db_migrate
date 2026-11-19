-- Seed data, as the app role.
SET ROLE app;

INSERT INTO public.customers (email, external_ref, name, created_at)
SELECT format('User%s@Example.com', g), format('cus_%s', g),
       (ARRAY['Zoë', 'zack', 'Émile', 'adam', 'Ångström', 'bob', 'Ölaf', 'carol'])[1 + g % 8] || ' ' || g,
       now() - (g || ' minutes')::interval
FROM generate_series(1, 2000) g;

INSERT INTO public.products (sku, title, attributes, price_cents)
SELECT format('SKU-%s', g), format('Product %s', g),
       jsonb_build_object('color', (ARRAY['red','green','blue'])[1 + g % 3], 'size', g % 10),
       100 + g * 7
FROM generate_series(1, 300) g;

INSERT INTO public.orders (customer_id, customer_ref, status, note, created_at)
SELECT c, format('cus_%s', c), (ARRAY['new','paid','shipped','cancelled'])[1 + g % 4],
       CASE WHEN g % 5 = 0 THEN NULL ELSE repeat('note ', 1 + g % 7) END,
       now() - (g || ' seconds')::interval
FROM generate_series(1, 20000) g, LATERAL (SELECT 1 + (g * 7919) % 2000 AS c) x;

INSERT INTO public.order_items (order_id, line_no, sku, qty)
SELECT o, l, format('SKU-%s', 1 + (o * l) % 300), 1 + (o + l) % 5
FROM generate_series(1, 20000) o, generate_series(1, 1 + o % 4) l;

INSERT INTO public.events (kind, payload)
SELECT (ARRAY['signup','login','noise','purchase'])[1 + g % 4], jsonb_build_object('n', g)
FROM generate_series(1, 30000) g;

INSERT INTO public.audit_log (message)
SELECT format('seed %s', g) FROM generate_series(1, 500) g;

INSERT INTO public.customer_tags (customer_id, tag)
SELECT 1 + g % 2000, (ARRAY['vip','beta','churned'])[1 + g % 3] FROM generate_series(1, 3000) g;

INSERT INTO public.settings (name, value)
SELECT 'setting_' || g, 'v' || g FROM generate_series(1, 50) g;
INSERT INTO public.settings (name, value) VALUES (NULL, 'unnamed');

INSERT INTO billing.invoices (order_id, amount_cents)
SELECT g * 2, 1000 + g FROM generate_series(1, 10000) g;

REFRESH MATERIALIZED VIEW public.customer_order_totals;
ANALYZE;
