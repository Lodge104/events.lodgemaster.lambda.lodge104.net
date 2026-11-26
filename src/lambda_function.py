"""lodge104-eventdata Lambda handler.

Pulls every Lodge 104 event from the LodgeMaster API and writes it into the
lodge104-event_data DynamoDB table (EVENT_DATA_TABLE_NAME).

For each event returned by the list endpoint (GET /api/events/), the
per-event details endpoint (GET /api/events/{id}) is also called and its
fields are merged into the record, so each item holds both the list's
computed/display fields (type, chapter, eventLocation, countRegistered,
paidAmount, ...) and the detail-only fields (eventTypeId, eventLocationId,
notes, online-registration settings, ...).

Events that no longer exist in LodgeMaster are removed from the table so it
mirrors the source.

Triggered on a schedule by an EventBridge rule (see terraform/). The
LodgeMaster portal session cookies this function depends on (in
KEYS_TABLE_NAME) are refreshed out-of-band by the separately-owned
lodge104-lodgemaster-scrapper function.
"""

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal

import boto3
import urllib3

EVENT_DATA_TABLE_NAME = os.environ["EVENT_DATA_TABLE_NAME"]
KEYS_TABLE_NAME = os.environ["KEYS_TABLE_NAME"]
LODGEMASTER_BASE_URL = os.environ["LODGEMASTER_BASE_URL"].rstrip("/")
DETAIL_FETCH_CONCURRENCY = int(os.environ.get("DETAIL_FETCH_CONCURRENCY", "8"))

# Name of the one item in KEYS_TABLE_NAME whose value also needs to be sent
# as the X-Xsrf-Token header (in addition to the Cookie header below).
XSRF_TOKEN_ITEM_NAME = "XSRF-TOKEN"

EVENTS_PATH = "/api/events/"
EVENT_DETAILS_PATH = "/api/events/{event_id}"

# Partition key of EVENT_DATA_TABLE_NAME.
EVENT_KEY = "id"

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Reused across warm invocations of the same execution environment. See
# https://docs.aws.amazon.com/lambda/latest/dg/best-practices.html.
# PoolManager is thread-safe; maxsize matches the detail-fetch concurrency.
_http = urllib3.PoolManager(
    maxsize=DETAIL_FETCH_CONCURRENCY,
    timeout=urllib3.Timeout(connect=10, read=60),
    retries=urllib3.Retry(total=3, backoff_factor=1, status_forcelist=(502, 503, 504)),
)
_dynamodb = boto3.resource("dynamodb")
_dynamodb_client = boto3.client("dynamodb")

# Sent on every LodgeMaster request. Without it the portal answers an
# unauthenticated/unrecognised request with a 302 to its login page (which
# bounces through id.oa-scouting.org until urllib3 gives up with "too many
# redirects"); with it, the API returns a plain 401-style JSON body instead.
XHR_HEADERS = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}


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


def _open_api_session():
    """Exchanges the scrapper's browser-login cookies for an authorised API
    session and returns the headers to use for subsequent API calls.

    The cookies in KEYS_TABLE_NAME alone are NOT enough: every /api call
    returns "Authorization has been denied for this request" until the
    portal SPA's startup call, POST /api/app/login, has been made. That call
    validates the cookie jar and responds with refreshed XSRF-TOKEN /
    OA.LM.Lodge.Auth cookies, which must replace the stored ones for later
    requests.
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


def _get_json(path, headers):
    """GETs a LodgeMaster API path and returns the parsed JSON body.

    Floats are parsed as Decimal because the DynamoDB resource API rejects
    Python floats (e.g. paidAmount).
    """
    response = _http.request(
        "GET", f"{LODGEMASTER_BASE_URL}{path}", headers=headers, redirect=False
    )
    if response.status != 200:
        raise RuntimeError(f"LodgeMaster GET {path} failed with status {response.status}")
    return json.loads(response.data, parse_float=Decimal)


def _fetch_events(headers):
    events = _get_json(EVENTS_PATH, headers)
    if not isinstance(events, list):
        raise RuntimeError(f"LodgeMaster {EVENTS_PATH} returned an unexpected body")
    return events


def _fetch_event_details(event_id, headers):
    details = _get_json(EVENT_DETAILS_PATH.format(event_id=event_id), headers)
    if not isinstance(details, dict) or details.get(EVENT_KEY) != event_id:
        raise RuntimeError(
            f"LodgeMaster details for event {event_id} returned an unexpected body"
        )
    return details


def _enrich_events(events, headers):
    """Returns each list record merged with its details record.

    The two payloads share most fields with identical values; where they
    overlap, the details endpoint wins since it is the authoritative,
    single-event view.
    """
    synced_at = datetime.now(timezone.utc).isoformat()

    def enrich(event):
        details = _fetch_event_details(event[EVENT_KEY], headers)
        return {**event, **details, "lastSyncedAt": synced_at}

    with ThreadPoolExecutor(max_workers=DETAIL_FETCH_CONCURRENCY) as pool:
        return list(pool.map(enrich, events))


def _existing_event_ids(table):
    ids = set()
    scan_kwargs = {
        "ProjectionExpression": "#k",
        "ExpressionAttributeNames": {"#k": EVENT_KEY},
    }
    while True:
        page = table.scan(**scan_kwargs)
        ids.update(item[EVENT_KEY] for item in page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return ids
        scan_kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def lambda_handler(_event, _context):
    """Synchronize LodgeMaster events (with details) into DynamoDB."""
    table = _dynamodb.Table(EVENT_DATA_TABLE_NAME)
    headers = _open_api_session()

    events = _fetch_events(headers)
    if not events:
        # An empty list almost certainly means an upstream problem, not that
        # every event was deleted; refuse to wipe the table.
        raise RuntimeError(f"LodgeMaster {EVENTS_PATH} returned no events")

    records = _enrich_events(events, headers)

    current_ids = {record[EVENT_KEY] for record in records}
    stale_ids = _existing_event_ids(table) - current_ids

    with table.batch_writer(overwrite_by_pkeys=[EVENT_KEY]) as batch:
        for record in records:
            batch.put_item(Item=record)
        for stale_id in stale_ids:
            batch.delete_item(Key={EVENT_KEY: stale_id})

    logger.info("Synced %d events, removed %d stale events", len(records), len(stale_ids))
    return {"eventsSynced": len(records), "eventsRemoved": len(stale_ids)}
