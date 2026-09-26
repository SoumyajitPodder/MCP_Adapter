"""Local-development secret manager: reads ``ADAPTER_SECRET_<NAME>`` environment variables.

Production binds the organization's secret manager instead (pending, brief §15).
"""

import os
import re

from pydantic import SecretStr

from adapter_verify.access.ports import SecretUnavailableError

PREFIX = "ADAPTER_SECRET_"


def env_var_for(name: str) -> str:
    return PREFIX + re.sub(r"[^A-Za-z0-9]", "_", name).upper()


class EnvSecretManager:
    async def get(self, name: str) -> SecretStr:
        value = os.environ.get(env_var_for(name))
        if not value:
            raise SecretUnavailableError
        return SecretStr(value)
