"""Request and response bodies of the auth API.

Separate from both the ORM models and the domain: returning a model would tie
the contract to the schema of the database and make every migration a breaking
change of the API (docs/CODING_STANDARDS.md section 5).
"""
