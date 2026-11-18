-- Heroku-like source: the app role owns the database and its tables but is
-- not a superuser. Extensions are installed by the platform (superuser),
-- some into Heroku's heroku_ext schema.
CREATE ROLE app LOGIN PASSWORD 'app:pass';   -- ':' on purpose: preflight warns
ALTER DATABASE app OWNER TO app;
ALTER SCHEMA public OWNER TO app;

CREATE SCHEMA heroku_ext;
GRANT USAGE ON SCHEMA heroku_ext TO app;
CREATE EXTENSION pgcrypto WITH SCHEMA heroku_ext;
CREATE EXTENSION citext WITH SCHEMA public;
CREATE EXTENSION pg_trgm WITH SCHEMA public;

CREATE SCHEMA billing AUTHORIZATION app;
