"""One TLS rule for every connector (SSC-051, GA-5): verified always, two ways."""

import ssl


def tls_context(ca: str | None) -> ssl.SSLContext:
    """``verify-ca`` against a pasted CA, else ``verify-full`` against the system store. With a
    pasted CA the chain must lead to it and the name is not checked (a Cloud SQL certificate
    names its instance, not its address); without one the system trust store and the host name
    decide. There is no plaintext and no unverified mode. Python 3.13 and later refuse a CA
    without an Authority Key Identifier under ``VERIFY_X509_STRICT``, which Cloud SQL's
    per-instance CA lacks, so that one flag is cleared for a pasted CA."""
    if ca is None:
        return ssl.create_default_context()
    try:
        context = ssl.create_default_context(cadata=ca)
    except (ssl.SSLError, ValueError) as exc:
        raise ValueError("the CA certificate is not PEM") from exc
    context.check_hostname = False
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return context
