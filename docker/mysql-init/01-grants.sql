-- Runs once, when the MySQL container creates its data volume (docker-compose.yml).
-- MYSQL_USER already owns `emergencysos`; the tests and the load test use a separate
-- `emergencysos_test` database, so give the app user that one too.
CREATE DATABASE IF NOT EXISTS emergencysos_test CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
GRANT ALL PRIVILEGES ON `emergencysos_test`.* TO 'sos_app'@'%';
FLUSH PRIVILEGES;
