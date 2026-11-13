"""Cliente do modelo de visão local (llama.cpp server, API compatível com OpenAI)."""
from __future__ import annotations

import base64
import io
import json
import os

import requests
from PIL import Image

from .normas import CATEGORIAS

LLM_URL = os.environ.get("REEMBOLSO_LLM_URL", "http://127.0.0.1:8080/v1/chat/completions")
TIMEOUT = int(os.environ.get("REEMBOLSO_LLM_TIMEOUT", "900"))
MAX_TEXTO_PDF = int(os.environ.get("REEMBOLSO_MAX_TEXTO_PDF", "3000"))
LADO_MAX = int(os.environ.get("REEMBOLSO_IMG_LADO", "1024"))

_num = {"type": ["number", "null"]}


def _txt(n: int = 80, **extra) -> dict:
    # maxLength é imposto pela gramática do llama.cpp: se o modelo entrar em repetição
    # (ex.: endereço repetido até o limite de tokens), o campo é cortado e o JSON fecha.
    return {"type": ["string", "null"], "maxLength": n, **extra}


_str = _txt()

# Como o documento aparece na imagem → quantos graus girar (PIL: positivo = anti-horário) para endireitar.
ORIENTACOES = {"normal": 0, "de_cabeca_para_baixo": 180, "topo_a_direita": 90, "topo_a_esquerda": -90}

SCHEMA = {
    "type": "object",
    "properties": {
        # Primeiro campo de propósito: o modelo decide a orientação antes de transcrever.
        "orientacao": {"type": "string", "enum": list(ORIENTACOES)},
        "tipo_documento": {
            "type": "string",
            "enum": ["nfce", "nfe_danfe", "cupom_sat", "nfse", "recibo_simples",
                     "comprovante_cartao", "fatura", "bilhete_transporte", "outro"],
        },
        "emitente": {
            "type": "object",
            "properties": {"razao_social": _txt(120), "nome_fantasia": _txt(120), "cnpj": _str,
                           "cpf": _str, "endereco": _txt(160), "cidade": _str, "uf": _txt(2)},
            "required": ["razao_social", "cnpj", "cpf", "cidade", "uf"],
        },
        "destinatario": {
            "type": "object",
            "properties": {"nome": _txt(120), "cpf_cnpj": _str},
            "required": ["nome", "cpf_cnpj"],
        },
        "numero_documento": _str,
        "serie": _str,
        "data_emissao": _txt(10, description="DD/MM/AAAA"),
        "hora_emissao": _str,
        # Só dígitos e espaços (imposto pela gramática): a URL de consulta tem campo próprio, senão o
        # modelo escrevia a URL aqui e a chave era cortada pelo limite de tamanho.
        "chave_acesso": {"type": ["string", "null"], "pattern": "^[0-9 ]{0,60}$",
                         "description": "44 dígitos (11 grupos de 4)"},
        "url_consulta": _txt(120),
        "protocolo_autorizacao": _txt(30, description="protocolo de autorização da SEFAZ"),
        "itens": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"descricao": {"type": "string", "maxLength": 120}, "quantidade": _num,
                               "valor_unitario": _num, "valor_total": _num,
                               "categoria": {"type": "string", "enum": CATEGORIAS}},
                "required": ["descricao", "quantidade", "valor_unitario", "valor_total", "categoria"],
            },
            "maxItems": 60,
        },
        "subtotal": _num,
        "desconto": _num,
        "acrescimo": _num,
        "total": _num,
        "forma_pagamento": _txt(60, description="ex.: Cartão de Crédito, Cartão de Débito, Pix, Dinheiro"),
        "valor_pago": _num,
        "autorizacao_pagamento": _txt(30, description="código de autorização (AUT) ou NSU do cartão"),
        "categoria_despesa": {"type": "string", "enum": CATEGORIAS},
        "rasura": {"type": "boolean"},
        "assinatura": {"type": "boolean"},
        "carimbo_cnpj": {"type": "boolean"},
        "legibilidade": {"type": "string", "enum": ["boa", "media", "ruim"]},
        "sinais_suspeitos": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"descricao": {"type": "string", "maxLength": 200},
                               "gravidade": {"type": "string", "enum": ["baixa", "media", "alta"]}},
                "required": ["descricao", "gravidade"],
            },
            "maxItems": 6,
        },
        "observacoes": _txt(300),
    },
    "required": ["orientacao", "tipo_documento", "emitente", "destinatario", "numero_documento", "data_emissao",
                 "hora_emissao", "chave_acesso", "protocolo_autorizacao", "itens", "total",
                 "forma_pagamento", "valor_pago", "autorizacao_pagamento", "categoria_despesa", "rasura", "assinatura", "carimbo_cnpj",
                 "legibilidade", "sinais_suspeitos"],
}

PROMPT = """Você é um auditor de reembolsos corporativos no Brasil. Analise o documento da imagem
(nota fiscal, NFC-e, cupom SAT, NFS-e, recibo, comprovante de cartão etc.).

TAREFA 0 — ORIENTAÇÃO: em "orientacao", diga como o documento aparece na imagem: "normal" (texto na
posição de leitura), "de_cabeca_para_baixo" (texto invertido), "topo_a_direita" (documento deitado, com o
cabeçalho virado para a direita) ou "topo_a_esquerda" (deitado, com o cabeçalho para a esquerda).

TAREFA 1 — EXTRAÇÃO: transcreva os dados EXATAMENTE como aparecem.
- Valores numéricos em reais como número (ex.: 1234.56). Datas em DD/MM/AAAA.
- CNPJ/CPF/chave de acesso como aparecem impressos (pode manter pontuação).
- Se um campo não existir ou estiver ilegível, use null. NUNCA invente ou complete dados.
- "tipo_documento": "Documento Auxiliar da Nota Fiscal de Consumidor Eletrônica" ou "NFC-e" = nfce;
  DANFE de NF-e (modelo 55) = nfe_danfe.
- ITENS: leia cada linha da tabela com as colunas Qtd, Vl.Un (unitário) e Vl.Tot (total da linha).
  "quantidade" = Qtd, "valor_unitario" = Vl.Un, "valor_total" = Vl.Tot. Atenção a quantidades maiores
  que 1 (ex.: "2 UN 93,00 186,00" → quantidade 2, valor_unitario 93, valor_total 186).
- "chave_acesso": os 44 dígitos impressos perto de "Consulte pela Chave de Acesso" (só os dígitos).
  O endereço do site vai em "url_consulta". "protocolo_autorizacao": número do "Protocolo de Autorização".
- "numero_documento": número da nota (ex.: "NFC-e nº 6389" → 6389; "Nº 000.012.345" → 000.012.345).
- PAGAMENTO: "forma_pagamento" (ex.: Cartão de Crédito) e "valor_pago" ficam perto de "FORMA PAGAMENTO" /
  "VALOR PAGO". "autorizacao_pagamento": código AUT/autorização ou NSU do cartão, se impresso — NÃO é o
  Protocolo de Autorização da SEFAZ; se não houver código do cartão, use null.
- Para cada item, escolha a "categoria" que melhor o descreve (ex.: cerveja = bebida_alcoolica,
  refrigerante/suco/água = bebida_nao_alcoolica, prato/refeição = alimentacao, gasolina = combustivel,
  remédio = medicamento, diária de hotel = hospedagem, estacionamento/pedágio = pedagio_estacionamento).
  Em "categoria_despesa", a categoria predominante do documento.
- "rasura": true só se houver campo riscado, sobrescrito ou corrigido à mão. "assinatura": há assinatura
  manuscrita? "carimbo_cnpj": há carimbo com o CNPJ do emitente?

TAREFA 2 — AUDITORIA VISUAL: liste em "sinais_suspeitos" apenas indícios CONCRETOS que você vê, como:
- fontes, tamanhos ou alinhamentos diferentes dentro do mesmo campo (ex.: valor ou data);
- números com aparência colada, borrões ou fundo diferente ao redor de um campo;
- total que não corresponde à soma dos itens; datas ou números rasurados;
- itens incompatíveis com o tipo de estabelecimento;
- recibo genérico (modelo de papelaria) sem CNPJ/CPF, sem assinatura ou com dados vagos;
- aparência de documento gerado digitalmente/por IA (texto sem sentido, logotipos deformados).
Se não houver indícios, retorne lista vazia. Não especule sem evidência visual.
NÃO são indícios (não liste): dobras, manchas, amassados ou desgaste do papel; foto torta ou com
sombra; nome de estabelecimento "genérico"; somas e datas que conferem; pagamento em data posterior
à emissão (isso é normal). Gravidade "alta" só para sinal claro de adulteração.
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
                 "mas a imagem é a referência:\n---\n" + texto_pdf[:MAX_TEXTO_PDF] + "\n---\n")
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
    # text/event-stream sem charset: o requests assumiria ISO-8859-1 e corromperia os acentos.
    resp.encoding = "utf-8"
    partes, tempos, motivo_fim = [], {}, None
    for linha in resp.iter_lines(decode_unicode=True):
        if not linha or not linha.startswith("data: ") or linha == "data: [DONE]":
            continue
        ev = json.loads(linha[6:])
        if ev.get("error"):
            raise RuntimeError(f"servidor do modelo retornou erro: {ev['error']}")
        for ch in ev.get("choices") or []:
            partes.append((ch.get("delta") or {}).get("content") or "")
            motivo_fim = ch.get("finish_reason") or motivo_fim
        tempos = ev.get("timings") or tempos
    texto = "".join(partes)
    if motivo_fim == "length":
        raise ValueError("o modelo atingiu o limite de tokens sem concluir a resposta "
                         "(provável repetição). Tente reenviar o documento.")
    inicio, fim = texto.find("{"), texto.rfind("}")
    if inicio < 0 or fim < inicio:
        raise ValueError(f"resposta do modelo sem JSON: {texto[:200]!r}")
    try:
        dados = json.loads(texto[inicio:fim + 1])
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON incompleto na resposta do modelo (contexto cheio?): {e}") from e
    if tempos:
        dados["_desempenho"] = {
            "leitura_s": round(tempos.get("prompt_ms", 0) / 1000, 1),
            "tokens_entrada": tempos.get("prompt_n"),
            "geracao_s": round(tempos.get("predicted_ms", 0) / 1000, 1),
            "tokens_saida": tempos.get("predicted_n"),
            "tokens_por_s": round(tempos.get("predicted_per_second", 0), 2),
        }
    return dados
