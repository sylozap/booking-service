"""Rules of the catalog service.

Deliberately thin. The catalog is a reference book, and
docs/CODING_STANDARDS.md section 5 records the exception: there is no domain
entity here, no repository returning one, and the only real rule of the service
-- what a master finally charges for a service -- lives in :mod:`pricing` as a
value object over plain numbers.

What is left are the two modules every service has: the identifiers, so that
four kinds of UUID cannot be passed in each other's place, and the failures the
service can report.
"""
