"""Uso: python -m reembolso.cli comprovante.jpg [--valor 123.45] [--colaborador X] [--offline] [--nao-registrar]"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .pipeline import Politica, analisar
from .validators import brl


def main():
    # No Windows, saída redirecionada (> arquivo.json) usa cp1252 e quebra com "≠", "→", "—".
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap =argparse.ArgumentParser(description="Analisa um comprovante de reembolso.")
    ap.add_argument("arquivo")
    ap.add_argument("--valor", type=float, help="valor solicitado no reembolso")
    ap.add_argument("--colaborador", default="")
    ap.add_argument("--limite", type=float, default=None, help="limite interno opcional (R$)")
    ap.add_argument("--prazo", type=int, default=None, help="prazo em dias (padrão: o da memória normativa)")
    ap.add_argument("--limiar-fraude", type=int, default=50, help="score que indefere por suspeita de fraude")
    ap.add_argument("--offline", action="store_true", help="não consulta CNPJ na internet")
    ap.add_argument("--nao-registrar", action="store_true", help="não grava no histórico")
    ap.add_argument("--json", action="store_true", help="saída em JSON")
    a = ap.parse_args()
    if not Path(a.arquivo).is_file():
        ap.error(f"arquivo não encontrado: {a.arquivo}")

    pol = Politica(a.limite, a.prazo, online=not a.offline, registrar=not a.nao_registrar,
                   limiar_fraude=a.limiar_fraude)
    try:
        r = analisar(a.arquivo, pol, a.colaborador, a.valor)
    except ValueError as e:
        print(f"Erro: {e}", file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(r.para_dict(), ensure_ascii=False, indent=2, default=str))
        return 0
    print(f"\n{r.decisao.upper()} — {r.recomendacao}")
    print(f"Risco de fraude: {r.nivel} ({r.score}/100) · Base normativa: {r.base_normativa}")
    if r.valor_reembolsavel is not None and r.valor_documento is not None:
        print(f"Valor reembolsável: {brl(r.valor_reembolsavel)} de {brl(r.valor_documento)}"
              + (f" (glosa {brl(r.valor_glosado)})" if r.valor_glosado else ""))
    if r.decisao in ("deferido", "deferido_parcial"):
        listas = [("Itens glosados", r.glosas), ("Condições", r.exigencias)]
    else:
        listas = [("Motivos", r.motivos)]
    for titulo, lista in listas:
        if lista:
            print(f"{titulo}:")
            for m in lista:
                print(f"  • {m}")
    print()
    for c in r.checagens:
        print(f"  [{c.status:6}] {c.categoria}: {c.mensagem}" + (f"  (+{c.peso})" if c.peso else ""))
    print("\nResumo:", json.dumps(r.dados.get("resumo", {}), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
