"""Scenarios of the auth service.

One scenario, one public method, one transaction boundary. A scenario writes
the state change together with its events and raises ``DomainError``; it knows
nothing about HTTP.
"""
