"""Single source of truth for the package version.

Read at build time by hatchling (see [tool.hatch.version] in pyproject.toml)
and importable at runtime (`from _version import __version__`). Bump this one
line to change the version everywhere.
"""

__version__ = "0.3.2"
