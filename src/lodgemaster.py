"""Shared LodgeMaster API client used by every handler in this package.

Authenticates with the portal session cookies that the separately-owned
lodge104-lodgemaster-scrapper function keeps in KEYS_TABLE_NAME, then
exposes a small JSON GET helper.
"""

import json
import os
from decimal import Decimal

import boto3
import urllib3

KEYS_TABLE_NAME = os.environ["KEYS_TABLE_NAME"]
LODGEMASTER_BASE_URL = os.environ["LODGEMASTER_BASE_URL"].rstrip("/")
HTTP_POOL_SIZE = int(os.environ.get("HTTP_POOL_SIZE", "8"))

# Name of the one item in KEYS_TABLE_NAME whose value also needs to be sent
# as the X-Xsrf-Token header (in addition to the Cookie header below).
XSRF_TOKEN_ITEM_NAME = "XSRF-TOKEN"

# Sent on every LodgeMaster request. Without it the portal answers an
# unauthenticated/unrecognised request with a 302 to its login page (which
# bounces through id.oa-scouting.org until urllib3 gives up with "too many
# redirects"); with it, the API returns a plain 401-style JSON body instead.
XHR_HEADERS = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}

# Reused across warm invocations of the same execution environment. See
# https://docs.aws.amazon.com/lambda/latest/dg/best-practices.html.
# PoolManager is thread-safe.
_http = urllib3.PoolManager(
    maxsize=HTTP_POOL_SIZE,
    timeout=urllib3.Timeout(connect=10, read=60),
    retries=urllib3.Retry(total=3, backoff_factor=1, status_forcelist=(502, 503, 504)),
)
_dynamodb_client = boto3.client("dynamodb")


def _load_session_cookies():
    """Returns {name: value} for every cookie lodge104-lodgemaster-scrapper
    has stored in KEYS_TABLE_NAME.

    Forwards every item rather than a fixed allow-list: the portal's OpenID
    Connect login flow issues several cookies that must all be present
    together (OA.Portal.Auth plus its OA.Portal.AuthC1/C2 chunks, an
    .AspNetCore.Antiforgery.* cookie with a per-deployment suffix, etc.).

    Read fresh on every invocation (never cached across warm starts): the
    scrapper rewrites these regularly, and a stale copy is just a dead session.
    """
    cookies = {}
    paginator = _dynamodb_client.get_paginator("scan")
    for page in paginator.paginate(TableName=KEYS_TABLE_NAME):
        for item in page.get("Items", []):
            cookies[item["name"]["S"]] = item["value"]["S"]

    if not cookies:
        raise RuntimeError(
            f"{KEYS_TABLE_NAME} is empty; has lodge104-lodgemaster-scrapper run yet?"
        )
    if XSRF_TOKEN_ITEM_NAME not in cookies:
        raise RuntimeError(
            f"'{XSRF_TOKEN_ITEM_NAME}' is missing from {KEYS_TABLE_NAME}; has "
            "lodge104-lodgemaster-scrapper run recently?"
        )
    return cookies


def _request_headers(cookies):
    return {
        **XHR_HEADERS,
        "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
        "X-Xsrf-Token": cookies[XSRF_TOKEN_ITEM_NAME],
    }


def _merge_set_cookies(cookies, response):
    for header in response.headers.getlist("Set-Cookie"):
        pair = header.split(";", 1)[0]
        name, _, value = pair.partition("=")
        if name and value:
            cookies[name.strip()] = value.strip()


def open_api_session():
    """Exchanges the scrapper's browser-login cookies for an authorised API
    session and returns the headers to use for subsequent API calls.

    The cookies in KEYS_TABLE_NAME alone are NOT enough: every /api and
    /odata call returns "Authorization has been denied for this request"
    until the portal SPA's startup call, POST /api/app/login, has been made.
    That call validates the cookie jar and responds with refreshed
    XSRF-TOKEN / OA.LM.Lodge.Auth cookies, which must replace the stored
    ones for later requests.
    """
    cookies = _load_session_cookies()
    response = _http.request(
        "POST",
        f"{LODGEMASTER_BASE_URL}/api/app/login",
        headers={**_request_headers(cookies), "Content-Type": "application/json"},
        body=b"",
        redirect=False,
    )
    if response.status != 200:
        raise RuntimeError(
            f"LodgeMaster /api/app/login failed with status {response.status}; "
            "the stored session cookies are probably expired"
        )
    _merge_set_cookies(cookies, response)
    return _request_headers(cookies)


def get_json(path, headers, fields=None):
    """GETs a LodgeMaster path (optionally with query fields) and returns the
    parsed JSON body.

    Floats are parsed as Decimal because the DynamoDB resource API rejects
    Python floats (e.g. paidAmount).
    """
    response = _http.request(
        "GET",
        f"{LODGEMASTER_BASE_URL}{path}",
        fields=fields,
        headers=headers,
        redirect=False,
    )
    if response.status != 200:
        raise RuntimeError(f"LodgeMaster GET {path} failed with status {response.status}")
    return json.loads(response.data, parse_float=Decimal)
