
import re


def mask_pii(text: str) -> str:
    """Маскирует email, номера телефонов и банковских карт."""

    if not text:
        return text

    # 1. Банковские карты: 16 цифр с пробелами, дефисами или без них
    card_pattern = r"(?<!\d)(?:\d[ -]?){15}\d(?!\d)"
    text = re.sub(
        card_pattern,
        "****-****-****-****",
        text,
    )

    # 2. Email
    email_pattern = r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"
    text = re.sub(
        email_pattern,
        "[email]",
        text,
    )

    # 3. Российские номера телефонов: +7 или 8 и 10 цифр
    phone_pattern = r"(?<!\d)(?:\+7|8)[\s()\-]*(?:\d[\s()\-]*){10}(?!\d)"

    def replace_phone(match):
        digits = re.sub(r"\D", "", match.group())
        return f"+7 (***) ***-**-{digits[-2:]}"

    text = re.sub(
        phone_pattern,
        replace_phone,
        text,
    )

    return text
