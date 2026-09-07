"""Data access of the auth service.

A repository never commits: the user row, the role, the confirmation token and
the outbox records of one registration have to land in one transaction, and a
repository that committed on its own would make that impossible
(docs/CODING_STANDARDS.md section 7).
"""
