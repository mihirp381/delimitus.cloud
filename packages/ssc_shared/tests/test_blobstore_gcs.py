"""``v4_url`` signs what Google's own client library signs, offline: the same service account
key, the same instant, the same headers and query, the same URL (SSC-046)."""

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.auth import crypt
from google.cloud.storage import _signing  # pyright: ignore[reportPrivateUsage]
from google.oauth2 import service_account

from ssc_shared.blobstore_gcs import KeySigner, v4_url

EMAIL = "ssc-data@ssc-c-bcdfghjklmnp.iam.gserviceaccount.com"
BUCKET = "ssc-c-bcdfghjklmnp-cell"
NOW = datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC)
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()
CASES = {
    "an upload with a type and a length range": (
        "PUT",
        "files/env_pppppppppppppppppppp/photos/cat.png",
        {"content-type": "image/png", "x-goog-content-length-range": "0,26214400"},
        {},
    ),
    "a download as an attachment": (
        "GET",
        "files/env_pppppppppppppppppppp/page.html",
        {},
        {"response-content-disposition": 'attachment; filename="page.html"'},
    ),
    "a plain download": ("GET", "snapshots/org_x/1.json", {}, {}),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_v4_url_matches_the_client_library(case: str) -> None:
    method, key, headers, query = CASES[case]
    signer = crypt.RSASigner.from_string(PEM)
    credentials = service_account.Credentials(signer, EMAIL, "https://oauth2.googleapis.com/token")
    theirs = _signing.generate_signed_url_v4(  # pyright: ignore[reportUnknownMemberType]
        credentials,
        resource=f"/{BUCKET}/{key}",
        expiration=timedelta(seconds=600),
        method=method,
        headers=dict(headers),
        query_parameters=dict(query),
        _request_timestamp=NOW.strftime("%Y%m%dT%H%M%SZ"),
    )
    ours = v4_url(
        signer=KeySigner(EMAIL, signer),
        bucket=BUCKET,
        key=key,
        method=method,
        headers=headers,
        now=NOW,
        expires_in=timedelta(seconds=600),
        query=query,
    )
    assert ours == theirs
