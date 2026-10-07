import os
import imaplib
import smtplib
import json
import urllib.request
import urllib.error

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


def call_agent(user_text):
    iam_token = get_iam_token()

    url = "https://ai.api.cloud.yandex.net/v1/responses"

    payload = {
        "model": f"gpt://{YC_FOLDER_ID}/yandexgpt/latest",
        "input": user_text,
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

        return data["output"][0]["content"][0]["text"]

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

            agent_reply = call_agent(user_text)

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
