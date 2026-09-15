-- Four databases and four roles, created once when the volume is empty.
--
-- Every service gets a role that can connect to its own database and to no
-- other.

CREATE ROLE auth WITH LOGIN PASSWORD 'auth';
CREATE ROLE catalog WITH LOGIN PASSWORD 'catalog';
CREATE ROLE booking WITH LOGIN PASSWORD 'booking';
CREATE ROLE notification WITH LOGIN PASSWORD 'notification';

CREATE DATABASE auth OWNER auth;
CREATE DATABASE catalog OWNER catalog;
CREATE DATABASE booking OWNER booking;
CREATE DATABASE notification OWNER notification;

-- PUBLIC may connect to any database by default, which would make the roles
-- above decorative.
REVOKE ALL ON DATABASE auth FROM PUBLIC;
REVOKE ALL ON DATABASE catalog FROM PUBLIC;
REVOKE ALL ON DATABASE booking FROM PUBLIC;
REVOKE ALL ON DATABASE notification FROM PUBLIC;

GRANT CONNECT, TEMPORARY ON DATABASE auth TO auth;
GRANT CONNECT, TEMPORARY ON DATABASE catalog TO catalog;
GRANT CONNECT, TEMPORARY ON DATABASE booking TO booking;
GRANT CONNECT, TEMPORARY ON DATABASE notification TO notification;
