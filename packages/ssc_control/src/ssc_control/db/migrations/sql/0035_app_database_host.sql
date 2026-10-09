-- GA-4.5 · An app database host may end in one dot.

-- Commit d89ea6b made the cell agent answer the instance's private DNS name as Cloud SQL lists
-- it, trailing dot kept, because the server certificate names it that way and libpq's
-- verify-full compares names exactly (`<id>.<id>.<region>.sql-psa.goog.`). The host CHECK of
-- 0022 refused that name, so the first Postgres deploy on cell 2 failed on 2026-10-09 with
-- app_database_host_check and ended DEPLOYMENT_STALLED. The pattern is the same with one
-- optional trailing dot; a leading dot, a second dot, an upper case letter and an empty host
-- stay refused. Rows from before still match, and the stored host is never rewritten: the
-- pinned URLs hold it.
ALTER TABLE ssc.app_database
  DROP CONSTRAINT app_database_host_check,
  ADD CONSTRAINT app_database_host_check
    CHECK (host ~ '^[a-z0-9]([a-z0-9.:-]{0,251}[a-z0-9])?\.?$');
