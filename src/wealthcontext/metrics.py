"""Public, versioned metric definitions; authoritative arithmetic lives in calculation modules."""

from importlib.resources import files

import yaml


def catalogue() -> list[dict]:
    directory = files("wealthcontext").joinpath("resources/metrics")
    definitions = [yaml.safe_load(path.read_text(encoding="utf-8"))
                   for path in directory.iterdir() if path.name.endswith(".yaml")]
    return sorted(definitions, key=lambda definition: definition["id"])
