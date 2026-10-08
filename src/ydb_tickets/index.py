import json
import os
import uuid
from datetime import datetime, timezone

import ydb


YDB_ENDPOINT = os.environ.get("YDB_ENDPOINT")
YDB_DATABASE = os.environ.get("YDB_DATABASE")


driver = ydb.Driver(
    endpoint=YDB_ENDPOINT,
    database=YDB_DATABASE,
    credentials=ydb.iam.MetadataUrlCredentials(),
)

driver.wait(timeout=10)

pool = ydb.SessionPool(driver)

def create_ticket(user_id, category, text):
    ticket_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    query = """
    DECLARE $id AS Utf8;
    DECLARE $user_id AS Utf8;
    DECLARE $category AS Utf8;
    DECLARE $status AS Utf8;
    DECLARE $text AS Utf8;
    DECLARE $created_at AS Timestamp;
    DECLARE $updated_at AS Timestamp;

    UPSERT INTO tickets (
        id,
        user_id,
        category,
        status,
        text,
        created_at,
        updated_at
    )
    VALUES (
        $id,
        $user_id,
        $category,
        $status,
        $text,
        $created_at,
        $updated_at
    );
    """

    def execute(session):
        prepared = session.prepare(query)

        session.transaction().execute(
            prepared,
            {
                "$id": ticket_id,
                "$user_id": user_id,
                "$category": category,
                "$status": "open",
                "$text": text,
                "$created_at": now,
                "$updated_at": now,
            },
            commit_tx=True,
        )

    pool.retry_operation_sync(execute)

    return {
        "ticket_id": ticket_id,
        "created_at": now.isoformat(),
    }

def list_my_tickets(user_id):
    query = """
    DECLARE $user_id AS Utf8;

    SELECT
        id,
        status,
        category,
        text,
        created_at
    FROM tickets
    WHERE user_id = $user_id
    ORDER BY created_at DESC;
    """

    def execute(session):
        prepared = session.prepare(query)

        return session.transaction().execute(
            prepared,
            {
                "$user_id": user_id,
            },
            commit_tx=True,
        )

    result_sets = pool.retry_operation_sync(execute)

    tickets = []

    for row in result_sets[0].rows:
        tickets.append(
            {
                "id": row.id,
                "status": row.status,
                "category": row.category,
                "text": row.text,
                "created_at": str(row.created_at),
            }
        )

    return tickets

def handle(event, context):
    try:
        if isinstance(event, str):
            event = json.loads(event)

        if "body" in event:
            body = event["body"]

            if isinstance(body, str):
                event = json.loads(body)
            elif isinstance(body, dict):
                event = body

        action = event.get("action")

        if action == "create-ticket":
            return create_ticket(
                event["user_id"],
                event["category"],
                event["text"],
            )

        if action == "list-my-tickets":
            return list_my_tickets(event["user_id"])

        # MCP Hub передаёт аргументы непосредственно в event.
        if "category" in event and "text" in event and "user_id" in event:
            return create_ticket(
                event["user_id"],
                event["category"],
                event["text"],
            )

        if "user_id" in event:
            return list_my_tickets(event["user_id"])

        return {
            "error": "Unknown action",
        }

    except Exception as error:
        print(f"ydb-tickets error: {error}")

        return {
            "error": str(error),
        }
