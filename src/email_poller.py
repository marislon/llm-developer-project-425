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
from pii_masking import mask_pii
from user_identity import make_user_id
from llm_input_guard import classify_with_llm, is_message_allowed

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
    text = mask_pii(text)
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
    body = msg.get_body(preferencelist=("plain", "html"))

    if body is None:
        return ""

    content = body.get_content()

    if body.get_content_type() == "text/html":
        from html import unescape
        from html.parser import HTMLParser

        class TextExtractor(HTMLParser):
            def __init__(self):
                super().__init__()
                self.parts = []

            def handle_data(self, data):
                self.parts.append(data)

        parser = TextExtractor()
        parser.feed(content)
        content = unescape(" ".join(parser.parts))

    return content.strip()


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

    payload = {
        "model": f"gpt://{YC_FOLDER_ID}/yandexgpt/latest",
        "input": [
                     {
                         "role": "system",
                         "content": (
                             "Ты — AI Help Desk, помощник службы поддержки образовательной платформы. "
                             "Отвечай пользователю на русском языке. "
                             "Учитывай историю предыдущей переписки. "

                             "Если пользователь задаёт вопрос о правилах, услугах, подписках, "
                             "оплате или технических инструкциях платформы, "
                             "ВСЕГДА сначала используй file_search для поиска в базе знаний. "
                             "Формируй ответ только на основе найденной информации. "
                             "Отвечай кратко, понятно и указывай название документа-источника. "
                             "Если доступна ссылка на источник, включай её в ответ. "
                             "Не цитируй больше трёх предложений из документа. "

                             "Если в базе знаний нет ответа, честно сообщи об этом "
                             "и предложи пользователю зарегистрировать обращение. "
                             "Не придумывай правила, цены и технические инструкции. "

                             "Создавай новое обращение через create-ticket только тогда, "
                             "когда пользователь явно просит зарегистрировать новое обращение. "
                             "Если пользователь просит вспомнить переписку, узнать статус "
                             "или получить информацию о существующем обращении, "
                             "не создавай новый тикет. "
                             "Если пользователь прямо говорит, что новое обращение создавать "
                             "не нужно, не вызывай create-ticket. "
                             "Не создавай повторное обращение по уже зарегистрированной "
                             "проблеме без явного запроса пользователя."
                         ),
                     }
                 ] + (history or []) + [
                     {"role": "user", "content": user_text}
                 ],
        "tools": [
            {
                "type": "file_search",
                "vector_store_ids": [
                    "fvtdsn8q2t9elir34u5h"
                ],
            },
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


import re


def remove_quoted_reply(text):
    """Отделяет новое сообщение от цитируемой переписки."""

    # Типичный разделитель цитаты Gmail
    text = re.split(
        r"(?im)^\s*On .+ wrote:\s*$",
        text,
        maxsplit=1,
    )[0]

    # Разделитель в русскоязычных письмах
    text = re.split(
        r"(?im)^\s*В .+ писал[аи]?:\s*$",
        text,
        maxsplit=1,
    )[0]

    # Цитируемые строки
    lines = []
    for line in text.splitlines():
        if line.lstrip().startswith(">"):
            break
        lines.append(line)

    return "\n".join(lines).strip()


def process_emails():
    processed = 0

    with imaplib.IMAP4_SSL(IMAP_HOST, 993) as imap:
        imap.login(IMAP_USER, IMAP_PASSWORD)
        imap.select("INBOX")

        status, message_numbers = imap.search(None, "UNSEEN")

        if status != "OK":
            return processed

        for num in message_numbers[0].split():
            status, data = imap.fetch(num, "(BODY.PEEK[])")

            if status != "OK":
                continue

            raw_email = data[0][1]
            msg = BytesParser(policy=policy.default).parsebytes(raw_email)

            sender_name, sender_address = parseaddr(msg["From"])
            subject = msg["Subject"] or "Без темы"
            user_text = remove_quoted_reply(get_email_text(msg))
            user_text = mask_pii(user_text)

            if not sender_address or not user_text:
                continue
            try:
                category = classify_with_llm(
                    message=user_text,
                    iam_token=get_iam_token(),
                    folder_id=YC_FOLDER_ID,
                )
            except Exception:
                category = "error"

            if not is_message_allowed(category):
                # При техническом сбое оставляем письмо непрочитанным
                # для повторной проверки при следующем запуске.
                if category == "error":
                    imap.store(num, "-FLAGS", "\\Seen")
                else:
                    # Подозрительные и посторонние сообщения не передаём агенту.
                    imap.store(num, "+FLAGS", "\\Seen")
                continue

            user_id = make_user_id(sender_address)
            history = get_message_history(user_id)

            agent_result = call_agent(
                f"Идентификатор пользователя (user_id): {user_id}\n"
                f"Сообщение пользователя: {user_text}",
                history=history,
            )
            agent_reply = agent_result["text"]

            save_message(
                user_id=user_id,
                role="user",
                text=user_text,
                ticket_id=agent_result["ticket_id"],
            )

            save_message(
                user_id=user_id,
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
        print("EMAIL_POLLER_ERROR_TYPE:", type(error).__name__)

        return {
            "processed": 0,
            "status": "error",
            "error": str(error),
        }
