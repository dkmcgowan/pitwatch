"""Signing an AWS request, without pulling in boto3.

boto3 and botocore are about a hundred megabytes installed, most of it JSON
service definitions for services this application will never call. PitWatch
makes exactly one AWS request, `SNS Publish`, with a static access key, so the
whole of that is here in sixty lines instead.

That trade is only worth taking if the signing is actually right, and a wrong
signature fails as an opaque 403. So the test for this checks it against a
signature produced by botocore itself, generated once from the reference
implementation and frozen as a fixture. See tests/test_sigv4.py.

The algorithm is Signature Version 4, documented at
https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv-create-signed-request.html
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime
from urllib.parse import quote

ALGORITHM = "AWS4-HMAC-SHA256"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret_key: str, date_stamp: str, region: str, service: str) -> bytes:
    """The four step key derivation. Each step signs the next scope element."""
    key = f"AWS4{secret_key}".encode()
    key = _sign(key, date_stamp)
    key = _sign(key, region)
    key = _sign(key, service)
    return _sign(key, "aws4_request")


def canonical_request(
    method: str,
    path: str,
    query: str,
    headers: dict[str, str],
    payload: bytes,
) -> tuple[str, str]:
    """Return the canonical request and the list of signed header names.

    Header names are lowercased and sorted, values are stripped. Anything not
    listed here is not signed and can be changed in transit without breaking the
    signature, which is why host, date and content type are all included.
    """
    lowered = {name.lower(): value.strip() for name, value in headers.items()}
    signed_headers = ";".join(sorted(lowered))
    canonical_headers = "".join(f"{name}:{lowered[name]}\n" for name in sorted(lowered))

    request = "\n".join(
        [
            method.upper(),
            quote(path or "/", safe="/~"),
            query,
            canonical_headers,
            signed_headers,
            _sha256(payload),
        ]
    )
    return request, signed_headers


def authorization_header(
    *,
    access_key: str,
    secret_key: str,
    region: str,
    service: str,
    method: str,
    path: str,
    query: str,
    headers: dict[str, str],
    payload: bytes,
    now: datetime,
) -> str:
    """Build the Authorization header value for one request.

    ``now`` must be UTC and must match the x-amz-date header in ``headers``, or
    AWS rejects the request. It is passed in rather than read here so the tests
    can pin it.
    """
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    scope = f"{date_stamp}/{region}/{service}/aws4_request"

    request, signed_headers = canonical_request(method, path, query, headers, payload)
    string_to_sign = "\n".join([ALGORITHM, amz_date, scope, _sha256(request.encode("utf-8"))])

    key = signing_key(secret_key, date_stamp, region, service)
    signature = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    return (
        f"{ALGORITHM} Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
