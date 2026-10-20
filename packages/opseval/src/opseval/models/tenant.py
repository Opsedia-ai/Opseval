# SPDX-License-Identifier: Apache-2.0
"""Test-tenant pool declarations (FR-RUN-08, FR-SAF-03, ADR-0041).

A tenant is an isolated account of the agent under test. The pool is declared in a file
so it can be committed and reviewed, and **credentials are never in it**: each tenant
names the environment variable that holds its token. A pool file in version control that
carried secrets would be the obvious way to leak them.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

TENANT_SCHEMA = "opseval/v1"


class Tenant(BaseModel):
    """One isolated test account."""

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")]
    token_env: str | None = Field(default=None, min_length=1)
    """Name of the environment variable holding this tenant's token. Not the token."""
    target: str | None = None
    """Per-tenant agent URL, when each tenant has its own endpoint."""
    note: str | None = Field(default=None, max_length=500)


class TenantPool(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: str = Field(default=TENANT_SCHEMA, alias="schema")
    tenants: list[Tenant] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> TenantPool:
        seen: set[str] = set()
        for tenant in self.tenants:
            if tenant.id in seen:
                raise ValueError(f"duplicate tenant id {tenant.id!r}")
            seen.add(tenant.id)
        return self

    @property
    def ids(self) -> list[str]:
        return [t.id for t in self.tenants]


def lane_count(accounts: int, requested: int | None, cap: int) -> int:
    """``min(accounts, requested, cap)``, as FR-RUN-08 specifies, and never below 1.

    A lane without a tenant to hold would defeat the isolation the pool exists for, so
    the number of accounts is a hard ceiling however many lanes were asked for.
    """
    wanted = accounts if requested is None else requested
    return max(1, min(accounts, wanted, cap))


__all__ = ["TENANT_SCHEMA", "Tenant", "TenantPool", "lane_count"]
