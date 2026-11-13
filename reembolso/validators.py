"""Validações determinísticas de documentos fiscais brasileiros."""
from __future__ import annotations

import re
from datetime import date, datetime

UF_CODIGOS = {
    "11": "RO", "12": "AC", "13": "AM", "14": "RR", "15": "PA", "16": "AP", "17": "TO",
    "21": "MA", "22": "PI", "23": "CE", "24": "RN", "25": "PB", "26": "PE", "27": "AL",
    "28": "SE", "29": "BA", "31": "MG", "32": "ES", "33": "RJ", "35": "SP", "41": "PR",
    "42": "SC", "43": "RS", "50": "MS", "51": "MT", "52": "GO", "53": "DF",
}
MODELOS = {"55": "NF-e", "65": "NFC-e", "59": "CF-e SAT", "57": "CT-e", "67": "CT-e OS"}


def limpa(valor: str | None) -> str:
    """Mantém só letras e dígitos, em maiúsculas (CNPJ alfanumérico)."""
    return re.sub(r"[^0-9A-Za-z]", "", valor or "").upper()


def _val(c: str) -> int:
    # Regra do CNPJ alfanumérico (IN RFB 2.229/2024): valor = código ASCII - 48.
    return ord(c) - 48


def _dv_mod11(base: str, pesos: list[int]) -> int:
    resto = sum(_val(c) * p for c, p in zip(base, pesos)) % 11
    return 0 if resto < 2 else 11 - resto


def valida_cnpj(cnpj: str | None) -> bool:
    c = limpa(cnpj)
    if not re.fullmatch(r"[0-9A-Z]{12}[0-9]{2}", c) or len(set(c)) == 1:
        return False
    p1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    d1 = _dv_mod11(c[:12], p1)
    d2 = _dv_mod11(c[:12] + str(d1), [6] + p1)
    return c[12:] == f"{d1}{d2}"


def valida_cpf(cpf: str | None) -> bool:
    c = limpa(cpf)
    if not re.fullmatch(r"\d{11}", c) or len(set(c)) == 1:
        return False
    for n in (9, 10):
        soma = sum(int(c[i]) * (n + 1 - i) for i in range(n))
        dv = (soma * 10) % 11 % 10
        if dv != int(c[n]):
            return False
    return True


def dv_chave(base43: str) -> int:
    pesos = [2, 3, 4, 5, 6, 7, 8, 9]
    soma = sum(_val(c) * pesos[i % 8] for i, c in enumerate(reversed(base43)))
    resto = soma % 11
    return 0 if resto < 2 else 11 - resto


def parse_chave(chave: str | None) -> dict | None:
    """Decompõe a chave de acesso de 44 posições (NF-e/NFC-e/CF-e/CT-e)."""
    c = limpa(chave)
    if len(c) != 44:
        return None
    return {
        "chave": c,
        "dv_ok": c[43].isdigit() and dv_chave(c[:43]) == int(c[43]),
        "uf": UF_CODIGOS.get(c[0:2]),
        "ano_mes": f"20{c[2:4]}-{c[4:6]}",
        "cnpj_emitente": c[6:20],
        "modelo": MODELOS.get(c[20:22], c[20:22]),
        "serie": c[22:25],
        "numero": c[25:34].lstrip("0") or "0",
    }


def acha_chave(texto: str | None) -> str | None:
    """Procura uma sequência de 44 dígitos (com ou sem espaços) num texto."""
    if not texto:
        return None
    juntos = re.sub(r"[\s.\-]", "", texto)
    m = re.search(r"(?<!\d)\d{44}(?!\d)", juntos)
    return m.group(0) if m else None


def para_float(v) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^\d,.\-]", "", str(v))
    if not s:
        return None
    if "," in s and "." in s:
        # o separador que aparece por último é o decimal: 1.234,56 (BR) ou 1,234.56 (US)
        mil, dec = (".", ",") if s.rfind(",") > s.rfind(".") else (",", ".")
        s = s.replace(mil, "").replace(dec, ".")
    elif "," in s:  # formato BR: 1234,56
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


_DATA_RE = re.compile(r"\d{4}-\d{2}-\d{2}|\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}")


def para_data(v: str | None) -> date | None:
    """Aceita DD/MM/AAAA, DD/MM/AA, AAAA-MM-DD etc., mesmo com hora junto ("05/11/26 14:30")."""
    if not v:
        return None
    m = _DATA_RE.search(str(v))
    if not m:
        return None
    s = m.group(0)
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d-%m-%y", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def brl(v: float | None) -> str:
    if v is None:
        return "-"
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
