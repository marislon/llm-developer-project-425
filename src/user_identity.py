import hashlib
import hmac
import os


def make_user_id(email: str) -> str:
    secret = os.environ["USER_ID_SECRET"]

    normalized_email = email.strip().lower()

    digest = hmac.new(
        secret.encode("utf-8"),
        normalized_email.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return "usr_" + digest
