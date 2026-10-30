# SPDX-License-Identifier: Apache-2.0
"""Accounts and access (ADR-0050): users, sessions, roles, API keys, OIDC and audit events.

:mod:`roles` and :mod:`tokens` are pure and need nothing installed. :mod:`passwords` needs
``argon2-cffi``, :mod:`oidc` needs PyJWT, and :mod:`store` and :mod:`service` need the database
(the ``server`` extra). The shared-token mode of ``opseval serve`` (ADR-0031) imports none of
them.
"""
