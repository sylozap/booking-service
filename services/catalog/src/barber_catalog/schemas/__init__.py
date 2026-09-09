"""Request and response bodies of the catalog API.

A schema is not a model and not an entity (docs/CODING_STANDARDS.md section 5).
Returning an ORM object from a router would tie the published contract to the
shape of the database, and every migration would become a breaking change to
the API.
"""
