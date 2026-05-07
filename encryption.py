"""encryption.py — Utilidades de cifrado, validacion Luhn, deteccion de tarjetas."""
import re


def validate_card_luhn(number: str) -> bool:
    """Valida numero de tarjeta con algoritmo de Luhn."""
    n = re.sub(r"\D", "", number or "")
    if not n or len(n) < 12 or len(n) > 19:
        return False
    total = 0
    reverse = n[::-1]
    for i, ch in enumerate(reverse):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def detect_card_type(number: str) -> str:
    """Detecta el tipo de tarjeta: visa, mastercard, amex, discover."""
    n = re.sub(r"\D", "", number or "")
    if re.match(r"^4", n):
        return "visa"
    if re.match(r"^(5[1-5]|2[2-7])", n):
        return "mastercard"
    if re.match(r"^3[47]", n):
        return "amex"
    if re.match(r"^6(?:011|5)", n):
        return "discover"
    return "unknown"


def validate_email(email: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email or ""))


def mask_card_number(number: str) -> str:
    n = re.sub(r"\D", "", number or "")
    if len(n) < 4:
        return "****"
    return "**** **** **** " + n[-4:]
