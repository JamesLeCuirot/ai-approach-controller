import importlib

import pytest


@pytest.mark.parametrize(
    "module",
    [
        "approach",
        "approach.sim",
        "approach.airports",
        "approach.airports.egkk",
        "approach.controllers",
        "approach.ai",
        "approach.data",
        "approach.radar",
    ],
)
def test_package_imports(module):
    assert importlib.import_module(module).__doc__
