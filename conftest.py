"""Fixtures available to every test of the repository.

The containers, the database isolation and the waiting helpers live in
``barber_common.testing``: five services need the same ones, and a copy per
service drifts apart within a month. Registering the module as a plugin here
makes ``postgres_dsn``, ``kafka_bootstrap`` and ``redis_dsn`` available in any
test without an import.
"""

pytest_plugins = ["barber_common.testing.fixtures"]
