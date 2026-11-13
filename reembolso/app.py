"""Interface web local: python -m reembolso.app  →  http://127.0.0.1:7860"""
from __future__ import annotations

import gradio as gr

from . import llm
from .pipeline import Politica, analisar
from .validators import brl

ICONES = {"ok": "✅", "info": "ℹ️", "alerta": "⚠️", "falha": "❌"}
CORES = {"baixo": "#2e7d32", "medio": "#ef6c00", "alto": "#c62828", "indeterminado": "#616161"}


def _executar(arquivo, colaborador, valor_solicitado, valor_maximo, prazo, online, registrar):
    if not arquivo:
        raise gr.Error("Envie a imagem ou o PDF do comprovante.")
    padrao = Politica()
    pol = Politica(
        valor_maximo=float(valor_maximo) if valor_maximo else padrao.valor_maximo,
        prazo_dias=int(prazo) if prazo else padrao.prazo_dias,
        online=online, registrar=registrar)
    try:
        r = analisar(arquivo, pol, colaborador=(colaborador or "").strip(),
                     valor_solicitado=valor_solicitado if valor_solicitado else None)
    except ValueError as e:  # arquivo vazio, corrompido, protegido por senha...
        raise gr.Error(str(e)) from e

    res = r.dados.get("resumo", {})
    cor = CORES[r.nivel]
    cab = (f"<div style='border-left:6px solid {cor};padding:8px 14px'>"
           f"<h2 style='margin:0;color:{cor}'>Risco {r.nivel.upper()} — {r.score}/100</h2>"
           f"<p style='margin:4px 0 0'>{r.recomendacao}</p></div>")
    linhas = [
        "| Campo | Valor |", "|---|---|",
        f"| Tipo | {res.get('tipo') or '-'} |",
        f"| Emitente | {res.get('emitente') or '-'} |",
        f"| CNPJ/CPF | {res.get('cnpj') or '-'} |",
        f"| Data de emissão | {res.get('data_emissao') or '-'} |",
        f"| Número | {res.get('numero') or '-'} |",
        f"| Total | {brl(res.get('total'))} |",
        f"| Pagamento | {res.get('forma_pagamento') or '-'} |",
        f"| Chave de acesso | `{res.get('chave_acesso') or '-'}` |",
    ]
    if r.id_historico:
        linhas.append(f"| Registro no histórico | #{r.id_historico} |")
    if r.links_consulta:
        linhas.append("\n**Consulta na SEFAZ** (confira se a nota existe e o valor bate):")
        linhas += [f"- <{u}>" for u in dict.fromkeys(r.links_consulta)]

    ordem = {"falha": 0, "alerta": 1, "info": 2, "ok": 3}
    tabela = [[f"{ICONES[c.status]} {c.status}", c.categoria, c.mensagem, c.peso]
              for c in sorted(r.checagens, key=lambda c: ordem[c.status])]
    return cab, "\n".join(linhas), tabela, r.ela_mapa, r.dados


def _status_llm():
    return ("🟢 Modelo de visão conectado." if llm.disponivel()
            else "🔴 Modelo de visão OFFLINE — rode `python start_llm.py` em outro terminal.")


with gr.Blocks(title="Análise de Reembolso") as demo:
    gr.Markdown("# Análise de Reembolso\nExtração de dados e indícios de fraude, 100% local.")
    status = gr.Markdown(_status_llm())
    with gr.Row():
        with gr.Column(scale=1):
            arquivo = gr.File(label="Comprovante (imagem ou PDF)", type="filepath",
                              file_types=["image", ".pdf"])
            colaborador = gr.Textbox(label="Colaborador (matrícula ou nome)")
            valor_sol = gr.Number(label="Valor solicitado (R$)", value=None)
            with gr.Accordion("Política", open=False):
                valor_max = gr.Number(label="Limite por reembolso (R$)", value=1000)
                prazo = gr.Number(label="Prazo máximo (dias desde a emissão)", value=90)
                online = gr.Checkbox(label="Consultar CNPJ na Receita (BrasilAPI)", value=True)
                registrar = gr.Checkbox(label="Registrar no histórico (detecção de duplicidade)", value=True)
            botao = gr.Button("Analisar", variant="primary")
            gr.Markdown("_Em CPU a análise com IA leva de 1 a 4 minutos._")
        with gr.Column(scale=2):
            cabecalho = gr.HTML()
            resumo = gr.Markdown()
            checks = gr.Dataframe(headers=["Status", "Categoria", "Detalhe", "Peso"],
                                  label="Checagens", wrap=True, interactive=False)
            with gr.Accordion("Mapa ELA (forense de compressão)", open=False):
                ela = gr.Image(label="Áreas claras isoladas = recompressão diferente", type="pil")
            with gr.Accordion("Dados completos (JSON)", open=False):
                bruto = gr.JSON()

    botao.click(_status_llm, outputs=status).then(
        _executar, [arquivo, colaborador, valor_sol, valor_max, prazo, online, registrar],
        [cabecalho, resumo, checks, ela, bruto])


# Força os textos nativos do Gradio (upload, botões, mensagens) em pt-BR,
# independentemente do idioma configurado no navegador.
FORCAR_PT_BR = """<script>
for (const p of ["language", "languages"]) {
  Object.defineProperty(navigator, p, {get: () => p === "language" ? "pt-BR" : ["pt-BR", "pt"]});
}
document.documentElement.lang = "pt-BR";
</script>"""


def main():
    demo.queue().launch(server_name="127.0.0.1", server_port=7860, head=FORCAR_PT_BR)


if __name__ == "__main__":
    main()
