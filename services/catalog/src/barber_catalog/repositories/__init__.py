"""Access to the data of the catalog.

All SQL of the service lives here. The catalog is the one place in the platform
where a repository hands back an ORM model rather than a domain entity: it is a
reference book without domain entities to hand back, and the exception is
recorded in docs/CODING_STANDARDS.md section 5. Mapping a row into a response
schema is the job of the scenario that asked for it.
"""
