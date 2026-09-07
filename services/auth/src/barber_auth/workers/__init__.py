"""Background work of the auth service.

A worker is restartable by construction: interrupting it at any moment leaves
the database in a state the next pass can continue from. Selecting the
candidates, doing the work and recording it happen in one transaction, and the
task is started from the lifespan of the service rather than with a bare
``create_task`` (docs/CODING_STANDARDS.md section 13).
"""
