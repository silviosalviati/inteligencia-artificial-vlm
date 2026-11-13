"""Histórico local (SQLite) para detectar reembolsos duplicados."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from .forensics import distancia_phash

DB = Path(__file__).resolve().parent.parent / "dados" / "historico.sqlite"
LIMIAR_PHASH = 18  # bits diferentes (hash de 256 bits); abaixo disso = mesma imagem


def _con() -> sqlite3.Connection:
    DB.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS reembolsos (
        id INTEGER PRIMARY KEY, criado_em TEXT, colaborador TEXT, arquivo TEXT,
        sha256 TEXT, phash TEXT, chave TEXT, cnpj TEXT, numero TEXT,
        data_emissao TEXT, total REAL, score INTEGER)""")
    return con


def buscar_duplicados(sha256: str, phash: str, chave: str | None, cnpj: str | None,
                      numero: str | None, data_emissao: str | None, total: float | None) -> list[dict]:
    achados = []
    with _con() as con:
        for row in con.execute("SELECT id, criado_em, colaborador, arquivo, sha256, phash, chave, "
                               "cnpj, numero, data_emissao, total FROM reembolsos"):
            (rid, criado, colab, arq, r_sha, r_ph, r_ch, r_cnpj, r_num, r_data, r_total) = row
            motivo = None
            if r_sha == sha256:
                motivo = "arquivo idêntico"
            elif chave and r_ch == chave:
                motivo = "mesma chave de acesso"
            elif r_ph and distancia_phash(r_ph, phash) <= LIMIAR_PHASH:
                motivo = "imagem praticamente igual"
            elif (cnpj and numero and r_cnpj == cnpj and r_num == numero):
                motivo = "mesmo CNPJ e número de documento"
            elif (cnpj and data_emissao and total is not None and r_cnpj == cnpj
                  and r_data == data_emissao and r_total is not None and abs(r_total - total) < 0.01):
                motivo = "mesmo CNPJ, data e valor"
            if motivo:
                achados.append({"id": rid, "em": criado, "colaborador": colab,
                                "arquivo": arq, "motivo": motivo})
    return achados


def registrar(colaborador: str, arquivo: str, sha256: str, phash: str, chave: str | None,
              cnpj: str | None, numero: str | None, data_emissao: str | None,
              total: float | None, score: int) -> int:
    with _con() as con:
        cur = con.execute(
            "INSERT INTO reembolsos (criado_em, colaborador, arquivo, sha256, phash, chave, cnpj, "
            "numero, data_emissao, total, score) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), colaborador, arquivo, sha256, phash,
             chave, cnpj, numero, data_emissao, total, score))
        return cur.lastrowid
