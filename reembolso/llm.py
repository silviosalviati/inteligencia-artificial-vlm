"""Cliente do modelo de visão local (llama.cpp server, API compatível com OpenAI)."""
from __future__ import annotations

import base64
import io
import json
import os

import requests
from PIL import Image

LLM_URL = os.environ.get("REEMBOLSO_LLM_URL", "http://127.0.0.1:8080/v1/chat/completions")
TIMEOUT = int(os.environ.get("REEMBOLSO_LLM_TIMEOUT", "900"))
LADO_MAX = int(os.environ.get("REEMBOLSO_IMG_LADO", "1024"))

_num = {"type": ["number", "null"]}
_str = {"type": ["string", "null"]}

SCHEMA = {
    "type": "object",
    "properties": {
        "tipo_documento": {
            "type": "string",
            "enum": ["nfce", "nfe_danfe", "cupom_sat", "nfse", "recibo_simples",
                     "comprovante_cartao", "fatura", "bilhete_transporte", "outro"],
        },
        "emitente": {
            "type": "object",
            "properties": {"razao_social": _str, "nome_fantasia": _str, "cnpj": _str,
                           "cpf": _str, "endereco": _str, "cidade": _str, "uf": _str},
            "required": ["razao_social", "cnpj", "cpf", "cidade", "uf"],
        },
        "destinatario": {
            "type": "object",
            "properties": {"nome": _str, "cpf_cnpj": _str},
            "required": ["nome", "cpf_cnpj"],
        },
        "numero_documento": _str,
        "serie": _str,
        "data_emissao": {**_str, "description": "DD/MM/AAAA"},
        "hora_emissao": _str,
        "chave_acesso": {**_str, "description": "44 dígitos, sem espaços"},
        "itens": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"descricao": {"type": "string"}, "quantidade": _num,
                               "valor_unitario": _num, "valor_total": _num},
                "required": ["descricao", "valor_total"],
            },
        },
        "subtotal": _num,
        "desconto": _num,
        "acrescimo": _num,
        "total": _num,
        "forma_pagamento": _str,
        "legibilidade": {"type": "string", "enum": ["boa", "media", "ruim"]},
        "sinais_suspeitos": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"descricao": {"type": "string"},
                               "gravidade": {"type": "string", "enum": ["baixa", "media", "alta"]}},
                "required": ["descricao", "gravidade"],
            },
        },
        "observacoes": _str,
    },
    "required": ["tipo_documento", "emitente", "destinatario", "data_emissao", "chave_acesso",
                 "itens", "total", "legibilidade", "sinais_suspeitos"],
}

PROMPT = """Você é um auditor de reembolsos corporativos no Brasil. Analise o documento da imagem
(nota fiscal, NFC-e, cupom SAT, NFS-e, recibo, comprovante de cartão etc.).

TAREFA 1 — EXTRAÇÃO: transcreva os dados EXATAMENTE como aparecem.
- Valores numéricos em reais como número (ex.: 1234.56). Datas em DD/MM/AAAA.
- CNPJ/CPF/chave de acesso como aparecem impressos (pode manter pontuação).
- Se um campo não existir ou estiver ilegível, use null. NUNCA invente ou complete dados.

TAREFA 2 — AUDITORIA VISUAL: liste em "sinais_suspeitos" apenas indícios CONCRETOS que você vê, como:
- fontes, tamanhos ou alinhamentos diferentes dentro do mesmo campo (ex.: valor ou data);
- números com aparência colada, borrões ou fundo diferente ao redor de um campo;
- total que não corresponde à soma dos itens; datas ou números rasurados;
- itens incompatíveis com o tipo de estabelecimento;
- recibo genérico (modelo de papelaria) sem CNPJ/CPF, sem assinatura ou com dados vagos;
- aparência de documento gerado digitalmente/por IA (texto sem sentido, logotipos deformados).
Se não houver indícios, retorne lista vazia. Não especule sem evidência visual.
{extra}
Responda somente com o JSON no esquema pedido."""


def _b64(img: Image.Image) -> str:
    img = img.copy()
    img.thumbnail((LADO_MAX, LADO_MAX))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92)
    return base64.b64encode(buf.getvalue()).decode()


def disponivel() -> bool:
    try:
        base = LLM_URL.split("/v1/")[0]
        return requests.get(f"{base}/health", timeout=3).status_code == 200
    except requests.RequestException:
        return False


def analisar_imagem(paginas: list[Image.Image], texto_pdf: str = "") -> dict:
    extra = ""
    if texto_pdf.strip():
        extra = ("\nO PDF também possui camada de texto (abaixo). Use-a para conferir a leitura, "
                 "mas a imagem é a referência:\n---\n" + texto_pdf[:4000] + "\n---\n")
    conteudo = [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_b64(p)}"}}
                for p in paginas]
    conteudo.append({"type": "text", "text": PROMPT.format(extra=extra)})

    # Streaming: em CPU a leitura da imagem leva minutos; o timeout vale por
    # intervalo sem dados, não para a análise inteira.
    resp = requests.post(
        LLM_URL,
        json={
            "messages": [{"role": "user", "content": conteudo}],
            "temperature": 0,
            "max_tokens": 2048,
            "stream": True,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "reembolso", "schema": SCHEMA}},
        },
        stream=True,
        timeout=(10, TIMEOUT),
    )
    resp.raise_for_status()
    partes, tempos = [], {}
    for linha in resp.iter_lines(decode_unicode=True):
        if not linha or not linha.startswith("data: ") or linha == "data: [DONE]":
            continue
        ev = json.loads(linha[6:])
        for ch in ev.get("choices") or []:
            partes.append((ch.get("delta") or {}).get("content") or "")
        tempos = ev.get("timings") or tempos
    texto = "".join(partes)
    inicio, fim = texto.find("{"), texto.rfind("}")
    dados = json.loads(texto[inicio:fim + 1])
    if tempos:
        dados["_desempenho"] = {
            "leitura_s": round(tempos.get("prompt_ms", 0) / 1000, 1),
            "tokens_entrada": tempos.get("prompt_n"),
            "geracao_s": round(tempos.get("predicted_ms", 0) / 1000, 1),
            "tokens_saida": tempos.get("predicted_n"),
            "tokens_por_s": round(tempos.get("predicted_per_second", 0), 2),
        }
    return dados
