"""Consultas online OPCIONAIS (dados públicos de empresas). Desligáveis na interface.

Envia apenas o CNPJ do estabelecimento emissor à BrasilAPI; nenhum dado do colaborador sai da máquina.
"""
from __future__ import annotations

import requests

from .validators import limpa


def consulta_cnpj(cnpj: str) -> dict:
    """Retorna {"status": "ok"|"nao_encontrado"|"erro", ...dados}."""
    c = limpa(cnpj)
    try:
        r = requests.get(f"https://brasilapi.com.br/api/cnpj/v1/{c}", timeout=15)
    except requests.RequestException as e:
        return {"status": "erro", "erro": str(e)}
    if r.status_code == 404:
        return {"status": "nao_encontrado"}
    if r.status_code != 200:
        return {"status": "erro", "erro": f"HTTP {r.status_code}"}
    try:
        d = r.json()
    except ValueError:
        return {"status": "erro", "erro": "resposta inválida da BrasilAPI"}
    return {
        "status": "ok",
        "razao_social": d.get("razao_social"),
        "nome_fantasia": d.get("nome_fantasia"),
        "situacao": d.get("descricao_situacao_cadastral"),
        "data_inicio_atividade": d.get("data_inicio_atividade"),
        "cnae": d.get("cnae_fiscal_descricao"),
        "municipio": d.get("municipio"),
        "uf": d.get("uf"),
    }
