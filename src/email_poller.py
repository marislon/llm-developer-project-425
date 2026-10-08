import os
import imaplib
import smtplib
import json
import urllib.request
import urllib.error
import time
import uuid
import ydb
from datetime import datetime, timezone

from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr

IMAP_HOST = os.environ.get("IMAP_HOST", "imap.yandex.ru")
IMAP_USER = os.environ.get("IMAP_USER")

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.yandex.ru")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER")

HELPDESK_MAILBOX = os.environ.get("HELPDESK_MAILBOX")

IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")

YC_FOLDER_ID = os.environ.get("YC_FOLDER_ID")
AGENT_ID = os.environ.get("AGENT_ID")
MCP_GATEWAY_ID = os.environ.get("MCP_GATEWAY_ID")
YDB_ENDPOINT = os.environ.get("YDB_ENDPOINT")
YDB_DATABASE = os.environ.get("YDB_DATABASE")

driver = ydb.Driver(
    endpoint=YDB_ENDPOINT,
    database=YDB_DATABASE,
    credentials=ydb.iam.MetadataUrlCredentials(),
)

driver.wait(timeout=10)
pool = ydb.SessionPool(driver)


def save_message(
        user_id,
        role,
        text,
        ticket_id=None,
        model=None,
        tokens_in=0,
        tokens_out=0,
        latency_ms=0,
):
    message_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    query = """
                DECLARE $id AS Utf8;
                DECLARE $user_id AS Utf8;
                DECLARE $ticket_id AS Utf8?;
                DECLARE $role AS Utf8;
                DECLARE $text AS Utf8;
                DECLARE $model AS Utf8?;
                DECLARE $tokens_in AS Uint64;
                DECLARE $tokens_out AS Uint64;
                DECLARE $latency_ms AS Uint32;
                DECLARE $created_at AS Timestamp;
            
                UPSERT INTO messages (
                    id, user_id, ticket_id, role, text,
                    model, tokens_in, tokens_out, latency_ms, created_at
                )
                VALUES (
                    $id, $user_id, $ticket_id, $role, $text,
                    $model, $tokens_in, $tokens_out, $latency_ms, $created_at
                );
                """

    def execute(session):
        prepared = session.prepare(query)
        session.transaction().execute(
            prepared,
            {
                "$id": message_id,
                "$user_id": user_id,
                "$ticket_id": ticket_id,
                "$role": role,
                "$text": text,
                "$model": model,
                "$tokens_in": tokens_in,
                "$tokens_out": tokens_out,
                "$latency_ms": latency_ms,
                "$created_at": now,
            },
            commit_tx=True,
        )

    pool.retry_operation_sync(execute)

def get_message_history(user_id):
    query = """
    DECLARE $user_id AS Utf8;

    SELECT role, text
    FROM messages
    WHERE user_id = $user_id
    ORDER BY created_at DESC
    LIMIT 10;
    """

    def execute(session):
        prepared = session.prepare(query)
        return session.transaction().execute(
            prepared,
            {"$user_id": user_id},
            commit_tx=True,
        )

    result_sets = pool.retry_operation_sync(execute)

    history = []
    for row in reversed(result_sets[0].rows):
        history.append({
            "role": row.role,
            "content": row.text,
        })

    return history

def get_email_text(msg):
    body = msg.get_body(preferencelist=("plain",))

    if body is None:
        return ""

    return body.get_content().strip()


def get_iam_token():
    url = (
        "http://169.254.169.254/"
        "computeMetadata/v1/instance/service-accounts/default/token"
    )

    request = urllib.request.Request(
        url,
        headers={"Metadata-Flavor": "Google"},
    )

    with urllib.request.urlopen(request, timeout=5) as response:
        data = json.loads(response.read().decode("utf-8"))

    return data["access_token"]


def call_agent(user_text, history=None):
    iam_token = get_iam_token()
    start_time = time.perf_counter()

    url = "https://ai.api.cloud.yandex.net/v1/responses"

    print(
        "REQUEST_HISTORY:",
        json.dumps(history or [], ensure_ascii=False)
    )

    payload = {
        "model": f"gpt://{YC_FOLDER_ID}/yandexgpt/latest",
        "input": (history or []) + [
            {"role": "user", "content": user_text}
        ],
        "tools": [
            {
                "type": "mcp",
                "server_label": "ydb-tickets-mcp",
                "require_approval": "never",
                "server_url": "https://db899ndddreca0g983fs.ibiatp37.mcpgw.serverless.yandexcloud.net/sse",
                "authorization": f"Bearer {iam_token}",
            }
        ],
    }

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {iam_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = json.loads(response.read().decode("utf-8"))
            print("RESPONSE_OUTPUT:", json.dumps(data.get("output", []), ensure_ascii=False))
            print("RESPONSE_STATUS:", data.get("status"), "ERROR:", data.get("error"), "INCOMPLETE:",
                  data.get("incomplete_details"))

        for item in data.get("output", []):
            if item.get("type") in ("mcp_call", "mcp_list_tools"):
                print("MCP_EVENT:", json.dumps(item, ensure_ascii=False))

        latency_ms = int((time.perf_counter() - start_time) * 1000)
        usage = data.get("usage") or {}
        tokens_in = usage.get("input_tokens", 0)
        tokens_out = usage.get("output_tokens", 0)

        ticket_id = None

        for item in data.get("output", []):
            if (
                    item.get("type") == "mcp_call"
                    and item.get("name") == "create-ticket"
                    and not item.get("error")
            ):
                output = item.get("output")

                if isinstance(output, str):
                    result = json.loads(output)
                    ticket_id = result.get("ticket_id")

        for item in data.get("output", []):
            if item.get("type") == "message" and item.get("role") == "assistant":
                for content in item.get("content", []):
                    if content.get("type") == "output_text":
                        return {
                            "text": content["text"],
                            "tokens_in": tokens_in,
                            "tokens_out": tokens_out,
                            "latency_ms": latency_ms,
                            "ticket_id": ticket_id,
                        }

        raise RuntimeError("В ответе модели не найден текст ассистента")

    except urllib.error.HTTPError as error:
        error_body = error.read().decode("utf-8")
        raise RuntimeError(
            f"AI API error {error.code}: {error_body}"
        ) from error


def send_email(to_address, subject, body):
    message = EmailMessage()
    message["From"] = HELPDESK_MAILBOX
    message["To"] = to_address
    message["Subject"] = subject
    message.set_content(body)

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.login(SMTP_USER, SMTP_PASSWORD)
        smtp.send_message(message)


def process_emails():
    processed = 0

    with imaplib.IMAP4_SSL(IMAP_HOST, 993) as imap:
        imap.login(IMAP_USER, IMAP_PASSWORD)
        imap.select("INBOX")

        status, message_numbers = imap.search(None, "UNSEEN")

        if status != "OK":
            return processed

        for num in message_numbers[0].split():
            status, data = imap.fetch(num, "(RFC822)")

            if status != "OK":
                continue

            raw_email = data[0][1]
            msg = BytesParser(policy=policy.default).parsebytes(raw_email)

            sender_name, sender_address = parseaddr(msg["From"])
            subject = msg["Subject"] or "Без темы"
            user_text = get_email_text(msg)

            if not sender_address or not user_text:
                continue
            history = get_message_history(sender_address)

            agent_result = call_agent(
                f"Идентификатор пользователя (user_id): {sender_address}\n"
                f"Сообщение пользователя: {user_text}",
                history=history,
            )
            agent_reply = agent_result["text"]

            save_message(
                user_id=sender_address,
                role="user",
                text=user_text,
                ticket_id=agent_result["ticket_id"],
            )

            save_message(
                user_id=sender_address,
                role="assistant",
                text=agent_reply,
                ticket_id=agent_result["ticket_id"],
                model="yandexgpt/latest",
                tokens_in=agent_result["tokens_in"],
                tokens_out=agent_result["tokens_out"],
                latency_ms=agent_result["latency_ms"],
            )

            send_email(
                sender_address,
                f"Re: {subject}",
                agent_reply,
            )

            imap.store(num, "+FLAGS", "\\Seen")
            processed += 1

    return processed


def handle(event, context):
    try:
        processed = process_emails()

        return {
            "processed": processed,
            "status": "ok",
        }

    except Exception as error:
        print(f"email-poller error: {error}")

        return {
            "processed": 0,
            "status": "error",
            "error": str(error),
        }
