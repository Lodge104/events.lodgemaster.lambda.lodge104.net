"""lodge104-eventattendees Lambda handler.

For every upcoming or in-progress event in the lodge104-event_data table
(EVENT_DATA_TABLE_NAME, populated by lodge104-eventdata), fetches the
event's registrations from the LodgeMaster EventAttendeeGrid OData feed and
mirrors them into the lodge104-event_attendees table
(EVENT_ATTENDEES_TABLE_NAME).

Each attendee item is keyed by (eventId, EventMemberId): a member who
registers for several events gets one item per event. The BsaPersonId-index
GSI finds every registration for one member.

Only items that actually changed are written, and registrations that have
disappeared from LodgeMaster (cancelled/removed) are deleted. Attendees of
events that have ended are left in place as a historical snapshot.
"""

import logging
import os
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key

import lodgemaster

EVENT_DATA_TABLE_NAME = os.environ["EVENT_DATA_TABLE_NAME"]
EVENT_ATTENDEES_TABLE_NAME = os.environ["EVENT_ATTENDEES_TABLE_NAME"]

ATTENDEE_GRID_PATH = "/odata/EventAttendeeGrid"
PAGE_SIZE = 100

EVENT_KEY = "eventId"
ATTENDEE_KEY = "EventMemberId"
# Key attribute of the BsaPersonId-index GSI. DynamoDB rejects a NULL value
# for an index key, so it is dropped from items where LodgeMaster has none.
MEMBER_INDEX_KEY = "BsaPersonId"

# Same column list as the LodgeMaster portal's attendee grid.
ATTENDEE_FIELDS = ",".join([
    "EventMemberAddons", "EventAmountDue", "EventDuesAnnualPass", "EventAttendeeType",
    "EmailPrimaryBounced", "AddressPrimaryBounced", "PhonePrimaryBounced", "EventBalance",
    "EventBaseFeeAmount", "EventBaseFeeIsEarly", "EventBaseFeeIsLate", "BrotherhoodCouncil",
    "BrotherhoodDate", "EventEligibleBrotherhood", "BrotherhoodEligibleDate",
    "BrotherhoodEvent", "BrotherhoodEventLocation", "CalloutCouncil", "CalloutDate",
    "EventEligibleCallout", "CalloutEvent", "CalloutEventLocation", "EventEligibleOrdeal",
    "EventClanCandidate", "Chapter", "EventCheckinTime", "EventCheckoutTime",
    "AddressPrimaryCity", "AddressPrimaryCountry", "Level", "DateOfBirth", "DateOfDeath",
    "EventDietaryNeeds", "District", "DuesAnnualPass", "DuesCardPrinted", "DuesInputDate",
    "EventDuesPaid", "DuesAmountPaid", "DuesPaidDate", "EventClanElangomat",
    "ElectionDate", "EmailPrimaryAddress", "EmailPrimaryType",
    "EventEmergencyContactOneEmailAddress", "EventEmergencyContactOneFirstName",
    "EventEmergencyContactOneLastName", "EventEmergencyContactOnePhoneNumber",
    "EventEmergencyContactOneRelationship", "EventEmergencyContactTwoEmailAddress",
    "EventEmergencyContactTwoFirstName", "EventEmergencyContactTwoLastName",
    "EventEmergencyContactTwoPhoneNumber", "EventEmergencyContactTwoRelationship",
    "EventAge", "EventLevel", "EventNotes", "FullName", "Gender", "EventHasHealthInfo",
    "EventHealthFormExpDate", "LastName", "LastUpdatedDate", "DuesYear", "BsaPersonId",
    "EventIsPaidInFull", "EventRecordType", "EventRegisteredDate", "EventMemberType",
    "EventSpecialNeeds", "YouthAdult", "EventMemberId", "OalmId", "ReadOnly",
    "RecordType",
])

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_dynamodb = boto3.resource("dynamodb")


def _parse_utc(value):
    # LodgeMaster uses 7 fractional digits ("...00.0000000Z"); Python's
    # fromisoformat only accepts up to 6, so drop the fraction entirely.
    return datetime.fromisoformat(value.split(".", 1)[0].rstrip("Z")).replace(
        tzinfo=timezone.utc
    )


def _active_event_ids(events_table):
    """Ids of events that have not ended yet (upcoming or in progress)."""
    now = datetime.now(timezone.utc)
    ids = []
    scan_kwargs = {
        "ProjectionExpression": "#id, endDateTime",
        "ExpressionAttributeNames": {"#id": "id"},
    }
    while True:
        page = events_table.scan(**scan_kwargs)
        for event in page.get("Items", []):
            end = event.get("endDateTime")
            if end and _parse_utc(end) >= now:
                ids.append(int(event["id"]))
        if "LastEvaluatedKey" not in page:
            return sorted(ids)
        scan_kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _fetch_attendees(event_id, headers):
    """Pages through EventAttendeeGrid for one event using $skip/$top."""
    attendees = []
    while True:
        body = lodgemaster.get_json(
            ATTENDEE_GRID_PATH,
            headers,
            fields={
                "$orderby": "EventMemberId",
                "$top": str(PAGE_SIZE),
                "$skip": str(len(attendees)),
                "$select": ATTENDEE_FIELDS,
                "$count": "true",
                "eventId": str(event_id),
            },
        )
        if "value" not in body:
            raise RuntimeError(f"EventAttendeeGrid for event {event_id} returned an error")
        page = body["value"]
        attendees.extend(page)
        total = body.get("@odata.count", len(attendees))
        if not page or len(attendees) >= total:
            return attendees


def _to_item(event_id, attendee):
    item = {**attendee, EVENT_KEY: event_id}
    if item.get(MEMBER_INDEX_KEY) is None:
        item.pop(MEMBER_INDEX_KEY, None)
    return item


def _existing_attendees(attendees_table, event_id):
    items = {}
    query_kwargs = {"KeyConditionExpression": Key(EVENT_KEY).eq(event_id)}
    while True:
        page = attendees_table.query(**query_kwargs)
        for item in page.get("Items", []):
            items[int(item[ATTENDEE_KEY])] = item
        if "LastEvaluatedKey" not in page:
            return items
        query_kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _sync_event(attendees_table, event_id, headers):
    """Mirrors one event's attendees. Returns (written, deleted, total)."""
    current = {
        int(a[ATTENDEE_KEY]): _to_item(event_id, a)
        for a in _fetch_attendees(event_id, headers)
    }
    existing = _existing_attendees(attendees_table, event_id)

    # int and Decimal compare equal, so items read back from DynamoDB match
    # freshly parsed API records when nothing changed.
    changed = [item for key, item in current.items() if existing.get(key) != item]
    removed = existing.keys() - current.keys()

    with attendees_table.batch_writer(overwrite_by_pkeys=[EVENT_KEY, ATTENDEE_KEY]) as batch:
        for item in changed:
            batch.put_item(Item=item)
        for key in removed:
            batch.delete_item(Key={EVENT_KEY: event_id, ATTENDEE_KEY: key})

    return len(changed), len(removed), len(current)


def lambda_handler(_event, _context):
    """Synchronize attendees of every upcoming/in-progress event."""
    events_table = _dynamodb.Table(EVENT_DATA_TABLE_NAME)
    attendees_table = _dynamodb.Table(EVENT_ATTENDEES_TABLE_NAME)

    event_ids = _active_event_ids(events_table)
    if not event_ids:
        logger.info("No upcoming events; nothing to sync")
        return {"events": 0, "attendees": 0, "written": 0, "removed": 0}

    headers = lodgemaster.open_api_session()

    totals = {"events": len(event_ids), "attendees": 0, "written": 0, "removed": 0}
    for event_id in event_ids:
        written, removed, count = _sync_event(attendees_table, event_id, headers)
        logger.info(
            "Event %d: %d attendees, %d written, %d removed", event_id, count, written, removed
        )
        totals["attendees"] += count
        totals["written"] += written
        totals["removed"] += removed

    logger.info("Synced %s", totals)
    return totals
