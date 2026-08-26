"""AWS request signing, checked against the reference implementation.

PitWatch signs its own AWS requests rather than depending on boto3, which is
about a hundred megabytes installed for the one call this makes. That is only a
good trade if the signing is correct, and a wrong signature fails as an opaque
403 with no clue in it.

So the expected values below were not written by hand and are not this code's
own output. They were produced by **botocore's** SigV4Auth, the reference
implementation, for exactly this request, and frozen here. Regenerate them the
same way if the signing ever needs to change:

    pip install botocore     # never a dependency of PitWatch, dev only
    python - <<'EOF'
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials
    request = AWSRequest(method="POST", url="https://sns.us-east-1.amazonaws.com/",
                         data=BODY, headers={"Content-Type": CONTENT_TYPE})
    SigV4Auth(Credentials(ACCESS_KEY, SECRET_KEY), "sns", "us-east-1").add_auth(request)
    print(request.context["timestamp"], request.headers["Authorization"])
    EOF

Note that botocore overwrites any timestamp you set with the current time, so
read the one it actually used back out of the request context rather than
assuming it honored yours.

The credentials are AWS's own documentation examples and are not real.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pitwatch.notify.sigv4 import authorization_header, canonical_request, signing_key

ACCESS_KEY = "AKIDEXAMPLE"
SECRET_KEY = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
REGION = "us-east-1"
SERVICE = "sns"
HOST = "sns.us-east-1.amazonaws.com"
CONTENT_TYPE = "application/x-www-form-urlencoded; charset=utf-8"
BODY = b"Action=Publish&Message=PitWatch+test&PhoneNumber=%2B12125550142&Version=2010-03-31"

# Frozen from botocore. See the module docstring.
SIGNED_AT = "20260826T011353Z"
EXPECTED = (
    "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20260826/us-east-1/sns/aws4_request, "
    "SignedHeaders=content-type;host;x-amz-date, "
    "Signature=a3c60439d2617a88bb8228a3e774fe1e1aa0a74b33307b78b31596ebd4abf6c1"
)


def sign_the_reference_request() -> str:
    return authorization_header(
        access_key=ACCESS_KEY,
        secret_key=SECRET_KEY,
        region=REGION,
        service=SERVICE,
        method="POST",
        path="/",
        query="",
        headers={"host": HOST, "x-amz-date": SIGNED_AT, "content-type": CONTENT_TYPE},
        payload=BODY,
        now=datetime.strptime(SIGNED_AT, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC),
    )


def test_the_signature_matches_botocore():
    assert sign_the_reference_request() == EXPECTED


def test_signing_is_deterministic():
    assert sign_the_reference_request() == sign_the_reference_request()


def test_a_different_secret_gives_a_different_signature():
    """Obvious, and the thing that silently would not happen if the key
    derivation dropped a step and signed with a constant."""
    other = authorization_header(
        access_key=ACCESS_KEY,
        secret_key="a-completely-different-secret-key-value",
        region=REGION,
        service=SERVICE,
        method="POST",
        path="/",
        query="",
        headers={"host": HOST, "x-amz-date": SIGNED_AT, "content-type": CONTENT_TYPE},
        payload=BODY,
        now=datetime.strptime(SIGNED_AT, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC),
    )

    assert other != EXPECTED


def test_the_body_is_covered_by_the_signature():
    """If the payload hash were left out, the message could be rewritten in
    transit and the signature would still verify."""
    tampered = authorization_header(
        access_key=ACCESS_KEY,
        secret_key=SECRET_KEY,
        region=REGION,
        service=SERVICE,
        method="POST",
        path="/",
        query="",
        headers={"host": HOST, "x-amz-date": SIGNED_AT, "content-type": CONTENT_TYPE},
        payload=BODY + b"&Extra=1",
        now=datetime.strptime(SIGNED_AT, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC),
    )

    assert tampered != EXPECTED


def test_headers_are_lowercased_sorted_and_listed():
    request, signed = canonical_request(
        "POST",
        "/",
        "",
        {"Host": HOST, "X-Amz-Date": SIGNED_AT, "Content-Type": CONTENT_TYPE},
        BODY,
    )

    assert signed == "content-type;host;x-amz-date"
    assert "\nhost:sns.us-east-1.amazonaws.com\n" in request


def test_the_signing_key_depends_on_every_scope_element():
    base = signing_key(SECRET_KEY, "20260826", REGION, SERVICE)

    assert base != signing_key(SECRET_KEY, "20260827", REGION, SERVICE)
    assert base != signing_key(SECRET_KEY, "20260826", "us-west-2", SERVICE)
    assert base != signing_key(SECRET_KEY, "20260826", REGION, "ses")
