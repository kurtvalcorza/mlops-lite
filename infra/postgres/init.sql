-- POSTGRES_DB creates the `mlflow` database. Create the gateway DB alongside it.
--
-- 023 US4 (T513, FR-297/302): this file owns DATABASE creation only. The application schema —
-- previously mirrored here AND in platformlib/store.py:DDL, two copies that could drift — lives
-- solely in the ordered migration files under platformlib/migrations/, applied by the gateway at
-- startup (and inspectable via scripts/migrate_db.py). A fresh volume therefore boots with an
-- EMPTY gateway database; the first gateway start materializes the schema through the same
-- runner an upgrade uses.
CREATE DATABASE gateway;
