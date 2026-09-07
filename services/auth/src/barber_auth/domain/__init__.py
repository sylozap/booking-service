"""Rules of the auth service: no database, no HTTP, no clock of its own.

Everything here is a pure function or an immutable value. The only import from
outside the standard library is ``barber_common.errors.DomainError`` in
:mod:`barber_auth.domain.errors`, so that a rule can name the failure it
detects instead of raising a ``ValueError`` somebody has to translate three
layers away.
"""
