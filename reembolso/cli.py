"""Uso: python -m reembolso.cli comprovante.jpg [--valor 123.45] [--colaborador X] [--offline] [--nao-registrar]"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .pipeline import Politica, analisar


def main():
    # No Windows, saída redirecionada (> arquivo.json) usa cp1252 e quebra com "≠", "→", "—".
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap =argparse.ArgumentParser(description="Analisa um comprovante de reembolso.")
    ap.add_argument("arquivo")
    ap.add_argument("--valor", type=float, help="valor solicitado no reembolso")
    ap.add_argument("--colaborador", default="")
    ap.add_argument("--limite", type=float, default=1000)
    ap.add_argument("--prazo", type=int, default=90)
    ap.add_argument("--offline", action="store_true", help="não consulta CNPJ na internet")
    ap.add_argument("--nao-registrar", action="store_true", help="não grava no histórico")
    ap.add_argument("--json", action="store_true", help="saída em JSON")
    a = ap.parse_args()
    if not Path(a.arquivo).is_file():
        ap.error(f"arquivo não encontrado: {a.arquivo}")

    pol = Politica(a.limite, a.prazo, online=not a.offline, registrar=not a.nao_registrar)
    try:
        r = analisar(a.arquivo, pol, a.colaborador, a.valor)
    except ValueError as e:
        print(f"Erro: {e}", file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(r.para_dict(), ensure_ascii=False, indent=2, default=str))
        return 0
    print(f"\nRISCO {r.nivel.upper()} — {r.score}/100 — {r.recomendacao}\n")
    for c in r.checagens:
        print(f"  [{c.status:6}] {c.categoria}: {c.mensagem}" + (f"  (+{c.peso})" if c.peso else ""))
    print("\nResumo:", json.dumps(r.dados.get("resumo", {}), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
