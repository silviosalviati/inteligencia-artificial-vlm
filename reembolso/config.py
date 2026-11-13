"""Parâmetros de funcionamento e de aprovação automática (salvos em dados/parametros.json).

Ficam fora do Git (pasta dados/) porque são configuração da operação, não código.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

ARQUIVO = Path(__file__).resolve().parent.parent / "dados" / "parametros.json"

MODOS = {
    "assistido": "Assistido: o sistema analisa e recomenda; um analista aprova ou indefere.",
    "automatico": "Automático: o sistema aprova e lança no SAP sozinho, dentro dos limites abaixo; "
                  "o que passar dos limites vai para a fila de aprovação humana.",
}

# Contas contábeis de EXEMPLO por tipo de despesa. Substitua pelas do plano de contas da empresa.
CONTAS_PADRAO = {
    "alimentacao": "4110201", "bebida_nao_alcoolica": "4110201", "doces_lanches": "4110201",
    "transporte_publico": "4110301", "taxi_aplicativo": "4110301", "pedagio_estacionamento": "4110302",
    "combustivel": "4110303", "outro": "4110999",
}


@dataclass
class Parametros:
    # ---- Funcionamento
    modo: str = "assistido"                    # "assistido" | "automatico"
    consultar_cnpj: bool = True                # BrasilAPI (envia só o CNPJ do emitente)
    registrar_historico: bool = True           # necessário para detectar duplicidade
    prazo_dias: int | None = None              # None = prazo da memória normativa (IN 001-004 §4.3)
    limite_interno: float | None = None        # teto opcional por reembolso (a IN não define)
    limiar_fraude: int = 50                    # score que indefere por suspeita de fraude

    # ---- Aprovação automática (só no modo automático)
    limite_aprovacao_automatica: float = 300.0  # acima disso, sempre aprovação humana
    score_maximo_automatico: int = 19           # só risco baixo (0–19) é aprovado sem humano
    aprovar_com_condicoes: bool = False         # deferido com condições da norma (ex.: refeição) vai sozinho?
    aprovar_parcial: bool = True                # deferido parcialmente (com glosa) vai sozinho?
    indeferir_automaticamente: bool = True      # fraude, duplicidade e violação de norma: indefere sem humano

    # ---- Lançamento no SAP (simulado)
    sap_empresa: str = "1000"                   # BUKRS
    sap_tipo_documento: str = "KR"              # tipo de documento contábil
    sap_centro_custo: str = ""                  # KOSTL padrão
    sap_usuario_automatico: str = "AUTOAPROV"
    sap_contas: dict = field(default_factory=lambda: dict(CONTAS_PADRAO))

    def conta(self, categoria: str | None) -> str:
        return self.sap_contas.get(categoria or "outro") or self.sap_contas.get("outro") or "4110999"


def carregar(arquivo: Path | None = None) -> Parametros:
    arquivo = arquivo or ARQUIVO
    try:
        dados = json.loads(arquivo.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Parametros()
    validos = {f.name for f in fields(Parametros)}
    p = Parametros(**{k: v for k, v in dados.items() if k in validos})
    if p.modo not in MODOS:
        p.modo = "assistido"
    return p


def salvar(p: Parametros, arquivo: Path | None = None) -> Path:
    arquivo = arquivo or ARQUIVO
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    tmp = arquivo.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(p), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(arquivo)  # grava inteiro ou nada
    return arquivo
