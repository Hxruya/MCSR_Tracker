-- mcsr_app -> The web app and importer: read and write rows, run the stored routines (refresh_player_stats,
--             pct, win_pct) and make session TEMPORARY tables (seed analysis). No CREATE/ALTER/DROP, no
--             TRIGGER, no GRANT.
-- mcsr_admin -> Owns the schema: everything on the two databases (init_db, restore_data, the tests' setup).

CREATE USER IF NOT EXISTS 'mcsr_app'@'localhost' IDENTIFIED BY 'CHANGE_ME_APP';
CREATE USER IF NOT EXISTS 'mcsr_admin'@'localhost' IDENTIFIED BY 'CHANGE_ME_ADMIN';

GRANT SELECT, INSERT, UPDATE, DELETE, EXECUTE, CREATE TEMPORARY TABLES ON `mcsr`.* TO 'mcsr_app'@'localhost';
GRANT SELECT, INSERT, UPDATE, DELETE, EXECUTE, CREATE TEMPORARY TABLES ON `mcsr_test`.* TO 'mcsr_app'@'localhost';

GRANT ALL PRIVILEGES ON `mcsr`.* TO 'mcsr_admin'@'localhost';
GRANT ALL PRIVILEGES ON `mcsr_test`.* TO 'mcsr_admin'@'localhost';