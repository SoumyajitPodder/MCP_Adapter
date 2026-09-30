"""Local-development secret manager: reads ``ADAPTER_SECRET_<NAME>`` environment variables.

Production binds the organization's secret manager instead (pending, brief §15).
"""

import os
import re
from collections.abc import Iterable

from pydantic import SecretStr

from adapter_verify.access.ports import SecretUnavailableError

PREFIX = "ADAPTER_SECRET_"


def env_var_for(name: str) -> str:
    return PREFIX + re.sub(r"[^A-Za-z0-9]", "_", name).upper()


class EnvSecretManager:
    """``names`` are the secret names it will serve (the credential map's). Two names that map
    to one variable, e.g. ``a-b/read`` and ``a_b.read``, would share a value across tools and
    defeat per-tool scoping (§9.7), so they fail at construction."""

    def __init__(self, names: Iterable[str] = ()) -> None:
        by_var: dict[str, str] = {}
        for name in names:
            other = by_var.setdefault(env_var_for(name), name)
            if other != name:
                msg = f"secret names {other!r} and {name!r} both map to {env_var_for(name)}"
                raise ValueError(msg)

    async def get(self, name: str) -> SecretStr:
        value = os.environ.get(env_var_for(name))
        if not value:
            raise SecretUnavailableError
        return SecretStr(value)
