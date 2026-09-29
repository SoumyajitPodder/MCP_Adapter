"""Reviewed YAML config files → strict models.

Parsed with ``safe_load`` and re-validated as JSON, so strict models accept enum strings while
YAML-only types (dates, sets, tags) are rejected. Errors name locations, never values.
"""

import json

import yaml
from pydantic import BaseModel, ValidationError


class ConfigFileError(Exception):
    """A configuration file is unreadable or invalid."""


def parse_yaml_model[M: BaseModel](model: type[M], text: str) -> M:
    try:
        data = yaml.safe_load(text)
        return model.model_validate_json(json.dumps(data))
    except yaml.YAMLError:
        msg = "not valid YAML"
        raise ConfigFileError(msg) from None
    except TypeError:
        msg = "contains a value that is not plain JSON (e.g. an unquoted date)"
        raise ConfigFileError(msg) from None
    except ValidationError as exc:
        where = ", ".join(
            ".".join(map(str, e["loc"])) or "<root>" for e in exc.errors(include_input=False)
        )
        msg = f"invalid fields: {where}"
        raise ConfigFileError(msg) from None
