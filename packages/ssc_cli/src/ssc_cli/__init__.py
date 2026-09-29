"""The ssc command line tool."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ssc-cli")
except PackageNotFoundError:  # a source tree without installed metadata
    __version__ = "0.0.0"
