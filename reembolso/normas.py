"""Memória normativa: regras de reembolso extraídas das Instruções Normativas.

Cada IN da pasta INs/ tem um arquivo de memória em normas/*.json, curado a partir do texto da IN e
com a referência de cada regra (ex.: "IN 001-004 §4.2.3"). O modelo de visão só LÊ e CLASSIFICA o
comprovante; as regras são aplicadas aqui de forma determinística e auditável.

Para incluir ou atualizar uma IN: coloque o PDF em INs/ e crie/revise o JSON correspondente em
normas/ (com o sha256 do PDF). Enquanto isso não for feito, o sistema avisa que há IN fora da memória.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .validators import brl, para_float

RAIZ = Path(__file__).resolve().parent.parent
PASTA_NORMAS = RAIZ / "normas"
PASTA_INS = RAIZ / "INs"

# Categorias que o modelo de visão atribui a cada item e ao documento (usadas pelas regras).
CATEGORIAS = [
    "alimentacao", "bebida_nao_alcoolica", "bebida_alcoolica", "doces_lanches", "medicamento",
    "higiene_pessoal", "combustivel", "transporte_publico", "taxi_aplicativo", "pedagio_estacionamento",
    "hospedagem", "locacao_veiculo", "passagem_aerea", "curso_treinamento", "material_escritorio",
    "eletronico_eletrodomestico", "limpeza_copa", "presente_flores_decoracao", "entretenimento",
    "software", "conta_consumo_taxa_imposto", "servico", "outro",
]


def _n(texto) -> str:
    """Minúsculas, sem acentos e com espaços simples (para casar palavras-chave)."""
    s = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9%]+", " ", s).split())


def _contem(texto_n: str, palavras) -> str | None:
    for p in palavras or ():
        if re.search(rf"(?<![a-z0-9]){re.escape(_n(p))}(?![a-z0-9])", texto_n):
            return p
    return None


@dataclass
class Achado:
    efeito: str          # "indeferir" | "condicionar" | "alertar" | "ok"
    titulo: str
    mensagem: str
    ref: str             # ex.: "IN 001-004 §4.2.3"
    escopo: str = "documento"   # "item": glosa só os itens citados; "documento": vale para a nota toda
    valor: float = 0.0          # valor dos itens citados (para a glosa)


DIAS = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]


def _valor_item(item: dict) -> float:
    v = para_float(item.get("valor_total")) or 0.0
    q, u = para_float(item.get("quantidade")), para_float(item.get("valor_unitario"))
    return max(v, round(q * u, 2)) if q and u else v


@dataclass
class Memoria:
    normas: list[dict] = field(default_factory=list)
    regras: list[dict] = field(default_factory=list)
    prazo_dias: int | None = None
    prazo_ref: str = ""
    comprovante: dict = field(default_factory=dict)
    comprovante_ref: str = ""
    vantagem_ref: str = ""
    pendencias: list[str] = field(default_factory=list)

    @property
    def base(self) -> str:
        """Ex.: "IN 001-004 v20"."""
        return ", ".join(f"{n['codigo']} v{n.get('versao', '?')}" for n in self.normas) or "nenhuma IN carregada"


def _sha256(caminho: Path) -> str:
    return hashlib.sha256(caminho.read_bytes()).hexdigest()


def carregar(pasta: Path | None = None, pasta_ins: Path | None = None) -> Memoria:
    """Lê normas/*.json e compara com os PDFs de INs/ (aviso se houver IN fora da memória)."""
    pasta = pasta or PASTA_NORMAS
    pasta_ins = pasta_ins or PASTA_INS
    mem = Memoria()
    for arq in sorted(pasta.glob("*.json")) if pasta.is_dir() else []:
        try:
            n = json.loads(arq.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            mem.pendencias.append(f"Memória ilegível: {arq.name} ({e}).")
            continue
        mem.normas.append(n)
        cod = n.get("codigo", arq.stem)
        prazo = n.get("prazo_solicitacao_dias") or {}
        # com várias INs, vale o prazo mais restritivo
        if prazo.get("valor") and (mem.prazo_dias is None or prazo["valor"] < mem.prazo_dias):
            mem.prazo_dias, mem.prazo_ref = int(prazo["valor"]), f"{cod} {prazo.get('ref', '')}".strip()
        if n.get("comprovante"):
            for k, v in n["comprovante"].items():
                if isinstance(v, bool):
                    mem.comprovante[k] = mem.comprovante.get(k, False) or v
            mem.comprovante_ref = mem.comprovante_ref or f"{cod} {n['comprovante'].get('ref', '')}".strip()
        if n.get("vantagem_indevida"):
            mem.vantagem_ref = mem.vantagem_ref or f"{cod} {n['vantagem_indevida'].get('ref', '')}".strip()
        for r in n.get("regras", []):
            mem.regras.append({**r, "_ref": f"{cod} {r.get('ref', '')}".strip()})

    conhecidos = {(n.get("fonte") or {}).get("sha256"): n for n in mem.normas}
    por_arquivo = {Path((n.get("fonte") or {}).get("arquivo", "")).name: n for n in mem.normas}
    for pdf in sorted(pasta_ins.glob("*.pdf")) if pasta_ins.is_dir() else []:
        try:
            h = _sha256(pdf)
        except OSError:
            continue
        if h in conhecidos:
            continue
        if pdf.name in por_arquivo:
            mem.pendencias.append(f"A IN \"{pdf.name}\" mudou desde que a memória foi criada: revise normas/.")
        else:
            mem.pendencias.append(f"A IN \"{pdf.name}\" ainda não está na memória: suas regras não são aplicadas.")
    return mem


def _contexto_refeicao(data_em: date, hora) -> str:
    """Ex.: "Almoço em dia útil (quarta-feira, 13:39): só se enquadra como almoço comercial ou jornada extra." """
    dia = DIAS[data_em.weekday()]
    m = re.search(r"(\d{1,2})[:h](\d{2})", str(hora or ""))
    if data_em.weekday() < 5 and m and 11 <= int(m.group(1)) <= 15:
        return (f"almoço em dia útil ({dia}, {int(m.group(1)):02d}:{m.group(2)}). Só é reembolsável se for "
                "almoço comercial com corretores (itens discriminados), em viagem ou em jornada extra com "
                "autorização prévia do gestor; senão, use o Vale Refeição.")
    return ""


def avaliar(mem: Memoria, ext: dict, *, tipo: str | None, data_em: date | None, hoje: date,
            total: float | None, valor_solicitado: float | None, prazo_dias: int | None = None) -> list[Achado]:
    """Confronta a extração do comprovante com a memória normativa."""
    achados: list[Achado] = []
    if not ext or not mem.normas:
        return achados

    # ---- Requisitos do comprovante
    c, ref_c = mem.comprovante, mem.comprovante_ref
    emit = ext.get("emitente") or {}
    itens = [i for i in (ext.get("itens") or []) if isinstance(i, dict)]
    if c.get("exige_emitente") and not (emit.get("cnpj") or emit.get("cpf") or emit.get("razao_social")):
        achados.append(Achado("indeferir", "Comprovante incompleto", "Comprovante sem identificação do emitente.", ref_c))
    if c.get("exige_data") and not data_em:
        achados.append(Achado("indeferir", "Comprovante incompleto", "Comprovante sem data de emissão legível.", ref_c))
    if c.get("exige_valor") and total is None:
        achados.append(Achado("indeferir", "Comprovante incompleto", "Comprovante sem valor total legível.", ref_c))
    if tipo == "comprovante_cartao":
        achados.append(Achado("indeferir", "Comprovante inadequado",
                              "Comprovante de cartão não substitui a Nota Fiscal ou o Recibo: anexe a NF/recibo "
                              "com a descrição da despesa.", ref_c))
    elif c.get("exige_descricao") and not itens and not (ext.get("observacoes") or "").strip():
        achados.append(Achado("indeferir", "Comprovante incompleto",
                              "Comprovante sem descrição do bem ou serviço.", ref_c))
    if c.get("proibe_rasura") and ext.get("rasura") is True:
        achados.append(Achado("indeferir", "Comprovante com rasura", "O comprovante apresenta rasura.", ref_c))
    if tipo == "recibo_simples":
        if c.get("recibo_exige_numero") and not ext.get("numero_documento"):
            achados.append(Achado("indeferir", "Recibo incompleto", "Recibo sem numeração.", ref_c))
        if c.get("recibo_exige_assinatura_ou_carimbo") and not (ext.get("assinatura") or ext.get("carimbo_cnpj")):
            achados.append(Achado("indeferir", "Recibo incompleto",
                                  "Recibo sem assinatura e sem carimbo do CNPJ do emitente.", ref_c))

    # ---- Prazo de solicitação
    prazo = prazo_dias or mem.prazo_dias
    if prazo and data_em and data_em <= hoje:
        dias = (hoje - data_em).days
        if dias > prazo:
            achados.append(Achado("indeferir", "Fora do prazo",
                                  f"Gasto de {data_em:%d/%m/%Y} ({dias} dias): o prazo para pedir reembolso é de "
                                  f"{prazo} dias.", mem.prazo_ref))
        else:
            achados.append(Achado("ok", "Prazo", f"Dentro do prazo de {prazo} dias ({dias} dias desde o gasto).",
                                  mem.prazo_ref))

    # ---- Vantagem indevida
    if valor_solicitado is not None and total is not None and valor_solicitado - total > 0.01:
        achados.append(Achado("indeferir", "Vantagem indevida",
                              f"Valor solicitado ({brl(valor_solicitado)}) maior que o do comprovante ({brl(total)}).",
                              mem.vantagem_ref))

    # ---- Itens e tipo de despesa
    nome_emit = _n(" ".join(str(emit.get(k) or "") for k in ("razao_social", "nome_fantasia")))
    cat_doc = ext.get("categoria_despesa")
    for regra in mem.regras:
        efeito = regra.get("efeito", "alertar")
        cats = set(regra.get("categorias") or [])
        excecoes = regra.get("excecoes") or []
        # A categoria dada pela IA só vale para regras SEM palavras-chave (ex.: "Refeições"). Nas demais,
        # ela confunde (ex.: "ÁGUA MINERAL GASOSA" classificada como combustível): vale a descrição do item.
        usa_categoria = not regra.get("palavras")
        por_palavra, por_categoria, valor_palavra = [], [], 0.0
        for item in itens:
            desc = str(item.get("descricao") or "")
            dn = _n(desc)
            if _contem(dn, excecoes):
                continue
            v = _valor_item(item)
            rotulo = f"{desc.strip()[:60]} ({brl(v)})"
            if _contem(dn, regra.get("palavras")):
                por_palavra.append(rotulo)
                valor_palavra += v
            elif usa_categoria and item.get("categoria") in cats:
                por_categoria.append(rotulo)
        # Nome do estabelecimento (ex.: "Auto Posto") só conta se a nota não tem itens ou se algum item for da
        # categoria: água comprada na conveniência do posto não é combustível.
        sinal_emit = bool(_contem(nome_emit, regra.get("emitente"))) and (
            not itens or bool(por_palavra) or (usa_categoria and any(i.get("categoria") in cats for i in itens)))
        sinal_doc = cat_doc in cats and not itens

        titulo = regra.get("titulo", regra.get("id", "Regra"))
        condicao = regra.get("condicao", "")
        if regra.get("id") == "refeicao" and data_em:
            condicao = _contexto_refeicao(data_em, ext.get("hora_emissao")) or condicao
        lista = "; ".join((por_palavra or por_categoria)[:5])
        if efeito == "condicionar" and (por_palavra or por_categoria or sinal_emit or sinal_doc):
            itens_txt = f" Itens: {lista}." if lista else ""
            achados.append(Achado("condicionar", titulo, f"{titulo}: {condicao}{itens_txt}", regra["_ref"]))
        elif por_palavra and efeito == "indeferir":
            achados.append(Achado("indeferir", titulo, f"Sem cobertura ({titulo}): {lista}."
                                  + (f" {condicao}" if condicao else ""), regra["_ref"],
                                  escopo="item", valor=round(valor_palavra, 2)))
        elif por_palavra:
            achados.append(Achado("alertar", titulo, f"Confira ({titulo}): {lista}." + (f" {condicao}" if condicao else ""),
                                  regra["_ref"]))
        elif por_categoria or sinal_emit or sinal_doc:
            # Sinal indireto (classificação da IA ou nome do estabelecimento): nunca indefere sozinho.
            origem = (f"a IA classificou {lista}" if por_categoria
                      else f"o estabelecimento \"{emit.get('razao_social') or emit.get('nome_fantasia')}\""
                      if sinal_emit else "a IA classificou a despesa")
            achados.append(Achado("alertar", titulo, f"Confira: {origem} como \"{titulo}\", que a norma não cobre."
                                  + (f" {condicao}" if condicao else ""), regra["_ref"]))
    return achados
