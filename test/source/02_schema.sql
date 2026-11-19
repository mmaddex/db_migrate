-- Application schema, created by the app role.
SET ROLE app;

CREATE TABLE public.customers (
  id           bigserial PRIMARY KEY,
  email        citext NOT NULL,
  external_ref text NOT NULL,
  name         text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);
-- Expression unique index (deferred to build_indexes.sh).
CREATE UNIQUE INDEX customers_email_idx ON public.customers (lower(email::text));
-- Plain unique index that a foreign key references: must stay in the schema.
CREATE UNIQUE INDEX customers_external_ref_idx ON public.customers (external_ref);
-- Indexed text columns (built under Render's collation).
CREATE INDEX customers_name_idx ON public.customers (name);
CREATE INDEX customers_name_trgm_idx ON public.customers USING gin (name gin_trgm_ops);

CREATE TABLE public.products (
  sku           text PRIMARY KEY,
  title         text NOT NULL,
  attributes    jsonb NOT NULL DEFAULT '{}',
  price_cents   integer NOT NULL,
  price_dollars numeric GENERATED ALWAYS AS (price_cents / 100.0) STORED
);
CREATE INDEX products_attributes_idx ON public.products USING gin (attributes);
CREATE INDEX products_title_idx ON public.products (title text_pattern_ops);

CREATE TABLE public.orders (
  id           serial PRIMARY KEY,
  customer_id  bigint NOT NULL REFERENCES public.customers (id),
  customer_ref text NOT NULL REFERENCES public.customers (external_ref),
  status       text NOT NULL DEFAULT 'new',
  note         text,
  token        uuid NOT NULL DEFAULT heroku_ext.gen_random_uuid(),
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX orders_customer_id_idx ON public.orders (customer_id);
CREATE INDEX orders_status_created_idx ON public.orders (status, created_at DESC);
CREATE UNIQUE INDEX orders_token_idx ON public.orders (token);

CREATE TABLE public.order_items (
  order_id integer NOT NULL REFERENCES public.orders (id) ON DELETE CASCADE,
  line_no  integer NOT NULL,
  sku      text NOT NULL REFERENCES public.products (sku),
  qty      integer NOT NULL CHECK (qty > 0),
  PRIMARY KEY (order_id, line_no)
);
CREATE INDEX order_items_sku_idx ON public.order_items (sku);

CREATE TABLE public.events (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind       text NOT NULL,
  payload    jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX events_kind_idx ON public.events (kind) WHERE kind <> 'noise';

-- No primary key and no unique index: preflight must block this table, and
-- keys.sh adds a replication key to it.
CREATE TABLE public.audit_log (
  at      timestamptz NOT NULL DEFAULT now(),
  message text NOT NULL
);

-- No primary key, but a unique index on NOT NULL columns: Bucardo uses it as
-- the key, so preflight lets it through, and schema.sh keeps the index.
CREATE TABLE public.customer_tags (
  customer_id bigint NOT NULL REFERENCES public.customers (id),
  tag         text NOT NULL
);
CREATE UNIQUE INDEX customer_tags_uq ON public.customer_tags (customer_id, tag);

-- No primary key, and the unique index Bucardo would use has a NULL in it:
-- preflight must block this table too.
CREATE TABLE public.settings (
  name  text,
  value text NOT NULL
);
CREATE UNIQUE INDEX settings_name_idx ON public.settings (name);

-- App trigger. On the target, Bucardo writes in replica mode, so this must
-- NOT fire there (otherwise audit_log row counts would diverge).
CREATE FUNCTION public.log_status_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.status IS DISTINCT FROM OLD.status THEN
    INSERT INTO public.audit_log (message)
    VALUES (format('order %s: %s -> %s', NEW.id, OLD.status, NEW.status));
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER orders_status_audit AFTER UPDATE ON public.orders
  FOR EACH ROW EXECUTE FUNCTION public.log_status_change();

-- Second schema with a standalone sequence and a unique constraint.
CREATE SEQUENCE billing.invoice_number_seq START 1000;
CREATE TABLE billing.invoices (
  id           bigserial PRIMARY KEY,
  number       bigint NOT NULL DEFAULT nextval('billing.invoice_number_seq') UNIQUE,
  order_id     integer NOT NULL REFERENCES public.orders (id) ON DELETE CASCADE,
  amount_cents bigint NOT NULL,
  issued_on    date NOT NULL DEFAULT current_date
);
CREATE INDEX invoices_order_id_idx ON billing.invoices (order_id);
COMMENT ON INDEX billing.invoices_order_id_idx IS 'lookup by order';

-- Materialized view (not replicated; refreshed at cutover) with a unique index.
CREATE MATERIALIZED VIEW public.customer_order_totals AS
  SELECT c.id AS customer_id, count(o.id) AS orders, max(o.created_at) AS last_order_at
  FROM public.customers c LEFT JOIN public.orders o ON o.customer_id = c.id
  GROUP BY c.id;
CREATE UNIQUE INDEX customer_order_totals_pk ON public.customer_order_totals (customer_id);

-- View that references Heroku's extension schema.
CREATE VIEW public.order_tokens AS
  SELECT id, heroku_ext.digest(token::text, 'sha256') AS token_hash FROM public.orders;
