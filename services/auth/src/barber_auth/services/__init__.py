"""Scenarios of the auth service.

One scenario, one public method, one transaction boundary. The state change and
the events it causes are written together, and nothing here knows that HTTP
exists: a scenario raises a ``DomainError`` and the ``api`` layer turns it into
``problem+json`` (docs/CODING_STANDARDS.md section 7).
"""
