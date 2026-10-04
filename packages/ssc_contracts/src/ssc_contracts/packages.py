"""The platform package list: the system packages a build may install (SSC-093).

There are no Dockerfiles, so a native library, a font or a PDF tool an app needs comes from this
list, which the platform owns. Adding to it is a platform change reviewed by us, never a
per-customer setting. ``PYTHON_NEEDS`` and ``NODE_NEEDS`` say which app dependencies need which
Debian packages; the build installs the listed ones through Railpack, and an app whose
dependency needs one that is not listed, or whose ``railpack.json`` asks for one, is refused
with ``ADD_APPROVED_PACKAGE`` before any builder runs. Dependency names are lower case, Python
ones normalised as PEP 503 does.

Railpack, pinned and reviewed with the build tools, adds a few packages of its own for well-known
dependencies (``ffmpeg`` for pydub, ``libpq5`` for psycopg2, ``poppler-utils`` for pdf2image);
those it adds for Python are on the list too, so the list and what an app gets agree.
"""

from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Final

APPROVED_PACKAGES: Final = frozenset(
    {
        "default-libmysqlclient-dev",
        "default-mysql-client",
        "ffmpeg",
        "fonts-dejavu-core",
        "fonts-liberation",
        "libcairo2",
        "libcairo2-dev",
        "libgif-dev",
        "libharfbuzz-subset0",
        "libjpeg-dev",
        "libmagic1",
        "libpango-1.0-0",
        "libpango1.0-dev",
        "libpangoft2-1.0-0",
        "libpq-dev",
        "libpq5",
        "librsvg2-dev",
        "pkg-config",
        "poppler-utils",
    }
)
PYTHON_NEEDS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "pycairo": ("libcairo2-dev", "pkg-config"),
        "weasyprint": (
            "libpango-1.0-0",
            "libpangoft2-1.0-0",
            "libharfbuzz-subset0",
            "fonts-dejavu-core",
        ),
        "pdf2image": ("poppler-utils",),
        "python-magic": ("libmagic1",),
        "psycopg2": ("libpq-dev",),
        "mysqlclient": ("default-libmysqlclient-dev", "pkg-config"),
        "pytesseract": ("tesseract-ocr",),
        "pydub": ("ffmpeg",),
        "moviepy": ("ffmpeg",),
        "pyodbc": ("unixodbc-dev",),
        "python-ldap": ("libldap2-dev", "libsasl2-dev"),
        "xmlsec": ("libxmlsec1-dev", "pkg-config"),
        "pdfkit": ("wkhtmltopdf",),
    }
)
NODE_NEEDS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "canvas": (
            "libcairo2-dev",
            "libpango1.0-dev",
            "libjpeg-dev",
            "libgif-dev",
            "librsvg2-dev",
            "pkg-config",
        ),
        "pdf-poppler": ("poppler-utils",),
        "node-poppler": ("poppler-utils",),
        "node-tesseract-ocr": ("tesseract-ocr",),
        "fluent-ffmpeg": ("ffmpeg",),
        "odbc": ("unixodbc-dev",),
    }
)
HOW_TO_ASK: Final = (
    "Ask SSC support to add the package to the platform package list, naming the package and "
    "the dependency that needs it. Until it is listed, use a dependency that needs no system "
    "package (a pure-Python or pure-JavaScript one, or a wheel or prebuilt binary that carries "
    "its own library)."
)


def needed(python: Iterable[str], node: Iterable[str]) -> dict[str, tuple[str, ...]]:
    """Each dependency that needs system packages, with the packages it needs."""
    found = {d: PYTHON_NEEDS[d] for d in python if d in PYTHON_NEEDS}
    found.update({d: NODE_NEEDS[d] for d in node if d in NODE_NEEDS})
    return dict(sorted(found.items()))
