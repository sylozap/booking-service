"""Implementations of the ports the domain declares.

Everything with a dependency the domain is not allowed to have lives here: the
argon2 hasher behind :class:`~barber_auth.domain.passwords.PasswordHasher`, and
the development mailer standing in for ``notification`` until T5.4.
"""
