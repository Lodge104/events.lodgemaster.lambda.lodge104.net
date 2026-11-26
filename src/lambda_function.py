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
"""

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import boto3

import lodgemaster

EVENT_DATA_TABLE_NAME = os.environ["EVENT_DATA_TABLE_NAME"]
DETAIL_FETCH_CONCURRENCY = int(os.environ.get("DETAIL_FETCH_CONCURRENCY", "8"))

EVENTS_PATH = "/api/events/"
EVENT_DETAILS_PATH = "/api/events/{event_id}"

# Partition key of EVENT_DATA_TABLE_NAME.
EVENT_KEY = "id"

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_dynamodb = boto3.resource("dynamodb")


def _fetch_events(headers):
    events = lodgemaster.get_json(EVENTS_PATH, headers)
    if not isinstance(events, list):
        raise RuntimeError(f"LodgeMaster {EVENTS_PATH} returned an unexpected body")
    return events


def _fetch_event_details(event_id, headers):
    details = lodgemaster.get_json(EVENT_DETAILS_PATH.format(event_id=event_id), headers)
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
    headers = lodgemaster.open_api_session()

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
