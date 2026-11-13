"""Interface web local: python -m reembolso.app  →  http://127.0.0.1:7860"""
from __future__ import annotations

import math
import time
from html import escape
from pathlib import Path
from urllib.parse import urlparse

import gradio as gr

from . import forensics as fx
from . import llm
from .pipeline import Politica, analisar
from .validators import brl

UI = Path(__file__).parent / "ui"
CSS = (UI / "estilo.css").read_text(encoding="utf-8")
JS = (UI / "app.js").read_text(encoding="utf-8")

ORDEM = {"falha": 0, "alerta": 1, "info": 2, "ok": 3}
STATUS = {  # rótulo do grupo, ícone, aberto por padrão
    "falha": ("Falhas", "✕", True),
    "alerta": ("Alertas", "!", True),
    "info": ("Informativos", "i", False),
    "ok": ("Aprovadas", "✓", False),
}
NIVEIS = {"baixo": "Risco baixo", "medio": "Risco médio", "alto": "Risco alto",
          "indeterminado": "Indeterminado"}
TIPOS = {"nfce": "NFC-e", "nfe_danfe": "NF-e (DANFE)", "cupom_sat": "Cupom fiscal SAT",
         "nfse": "NFS-e", "recibo_simples": "Recibo simples", "comprovante_cartao": "Comprovante de cartão",
         "fatura": "Fatura", "bilhete_transporte": "Bilhete de transporte", "outro": "Outro"}


def _e(v) -> str:
    return escape("" if v is None else str(v))


# ---------------------------------------------------------------- blocos de HTML
LOGO = """<svg width="44" height="44" viewBox="0 0 44 44" aria-hidden="true">
<rect x="1" y="1" width="42" height="42" rx="11" fill="var(--ra-accent)"/>
<path d="M13 9h14l5 5v17" fill="none" stroke="var(--ra-surface)" stroke-width="2.2" stroke-linejoin="round"/>
<path d="M13 9v24l2.5-1.6L18 33l2.5-1.6L23 33" fill="none" stroke="var(--ra-surface)" stroke-width="2.2" stroke-linejoin="round"/>
<path d="M17 16h9M17 20h6" stroke="var(--ra-surface)" stroke-width="2.2" stroke-linecap="round"/>
<circle cx="28.5" cy="27.5" r="5.2" fill="var(--ra-accent)" stroke="var(--ra-surface)" stroke-width="2.2"/>
<path d="M32.4 31.4l3.6 3.6" stroke="var(--ra-surface)" stroke-width="2.6" stroke-linecap="round"/>
</svg>"""

ICONE_RELOGIO = ("<svg width='15' height='15' viewBox='0 0 16 16' aria-hidden='true'><circle cx='8' cy='8' r='6.5' "
                 "fill='none' stroke='currentColor' stroke-width='1.5'/><path d='M8 4.5V8l2.5 1.5' fill='none' "
                 "stroke='currentColor' stroke-width='1.5' stroke-linecap='round'/></svg>")

RECIBO = """<svg width="92" height="116" viewBox="0 0 92 116" aria-hidden="true">
<path d="M8 4h76v104l-9.5-6-9.5 6-9.5-6-9.5 6-9.5-6-9.5 6-9.5-6z" fill="var(--ra-surface-2)" stroke="var(--ra-line-strong)" stroke-width="2" stroke-linejoin="round"/>
<g stroke="var(--ra-line-strong)" stroke-width="3" stroke-linecap="round">
<path d="M22 22h48"/><path d="M22 34h30"/><path d="M22 50h48"/><path d="M22 60h48"/><path d="M22 70h36"/>
</g><path d="M50 86h20" stroke="var(--ra-accent)" stroke-width="4" stroke-linecap="round"/></svg>"""


def _topo(online: bool | None = None) -> str:
    online = llm.disponivel() if online is None else online
    if online:
        status = ("<div class='ra-status on' role='status'><span class='ra-ponto'></span>"
                  "<div>Modelo de visão conectado<small>Extração por IA disponível</small></div></div>")
    else:
        status = ("<div class='ra-status off' role='status'><span class='ra-ponto'></span>"
                  "<div>Modelo de visão offline<small>Inicie o modelo com <code>python main.py</code></small>"
                  "</div></div>")
    return (f"<header class='ra-topo'><div class='ra-marca'>{LOGO}<div><h1>Auditoria de Reembolsos</h1>"
            "<p>Análise de risco de fraude em comprovantes"
            "<span class='ra-local' title='Nenhum dado do comprovante sai desta máquina'>● 100% local</span></p>"
            f"</div></div>{status}</header>")


def _vazio() -> str:
    chips = "".join(f"<span>{c}</span>" for c in (
        "CNPJ e Receita Federal", "Chave de acesso e QR Code", "Soma dos itens", "Limite e prazo da política",
        "Datas", "Duplicidade no histórico", "Metadados do arquivo", "Forense de imagem (ELA)"))
    return (f"<section class='ra-estado'>{RECIBO}<h2>Nenhum comprovante analisado ainda</h2>"
            "<p>Envie a foto ou o PDF de um comprovante (NFC-e, DANFE, cupom SAT, recibo) para receber "
            "o score de risco, as checagens e os dados extraídos.</p>"
            "<ol class='ra-passos'>"
            "<li><strong>Envie o comprovante</strong>Foto do celular, print ou PDF.</li>"
            "<li><strong>Identifique o pedido</strong>Colaborador e valor solicitado.</li>"
            "<li><strong>Clique em Analisar</strong>Leva de 1 a 4 minutos em CPU.</li></ol>"
            "<div class='ra-verifica'><span class='ra-rotulo'>O que é verificado</span>"
            f"<div class='ra-chips'>{chips}</div></div></section>")


def _carregando(online: bool) -> str:
    inicio = int(time.time() * 1000)
    aviso = ("" if online else "<div class='ra-aviso'>Modelo de visão offline: a análise será parcial "
             "(metadados, forense e duplicidade) e o resultado ficará indeterminado.</div>")
    etapas = "".join(f"<li>{t}</li>" for t in (
        "Metadados e forense", "Leitura de QR Code", "Extração com IA", "Regras fiscais e política",
        "Duplicidade"))
    return (f"<section class='ra-estado carregando' aria-busy='true' aria-live='polite'>"
            f"<div class='ra-scan'>{RECIBO}<span class='ra-feixe'></span></div>"
            "<h2>Analisando o comprovante…</h2>"
            "<p>Em CPU, a análise com IA leva de <b>1 a 4 minutos</b>. Você pode continuar com esta aba aberta; "
            "o resultado aparece aqui automaticamente.</p>"
            "<div class='ra-barra'><i></i></div>"
            "<div class='ra-tempo'><div><span>Decorrido</span>"
            f"<strong data-ra-inicio='{inicio}'>00:00</strong></div>"
            "<div><span>Estimativa</span><strong>1–4 min</strong></div></div>"
            f"<ul class='ra-etapas'>{etapas}</ul>{aviso}</section>")


def _erro() -> str:
    return (f"<section class='ra-estado erro'>{RECIBO}<h2>A análise não foi concluída</h2>"
            "<p>Confira a mensagem de erro exibida no canto da tela, ajuste o arquivo ou os dados "
            "e clique em <b>Analisar</b> novamente.</p></section>")


def _medidor(score: int) -> str:
    """Medidor semicircular 0–100 com as faixas de risco (0–19, 20–49, 50–100)."""
    arco = "M20 100 A80 80 0 0 1 180 100"

    def ponto(v, r):
        a = math.pi * (1 - v / 100)
        return 100 + r * math.cos(a), 100 - r * math.sin(a)

    marcas = ""
    for v in (0, 20, 50, 100):
        x1, y1 = ponto(v, 70)
        x2, y2 = ponto(v, 90)
        tx, ty = ponto(v, 100)
        anc = "end" if v < 30 else ("middle" if v < 70 else "start")
        marcas += (f"<line x1='{x1:.1f}' y1='{y1:.1f}' x2='{x2:.1f}' y2='{y2:.1f}' stroke='var(--ra-surface)' "
                   f"stroke-width='2'/><text class='marca' x='{tx:.1f}' y='{ty + 3:.1f}' text-anchor='{anc}'>{v}</text>")
    return (f"<div class='ra-medidor' role='img' aria-label='Score de risco {score} de 100'>"
            "<svg viewBox='0 -6 200 112'>"
            f"<path class='trilho' d='{arco}' pathLength='100' fill='none' stroke-width='16'/>"
            f"<path class='zona-b' d='{arco}' pathLength='100' fill='none' stroke-width='16' stroke-dasharray='20 100'/>"
            f"<path class='zona-m' d='{arco}' pathLength='100' fill='none' stroke-width='16' "
            "stroke-dasharray='0 20 30 100'/>"
            f"<path class='zona-a' d='{arco}' pathLength='100' fill='none' stroke-width='16' "
            "stroke-dasharray='0 50 50 100'/>"
            f"<path class='valor' d='{arco}' pathLength='100' fill='none' stroke-width='16' "
            f"stroke-dasharray='{max(score, 0.6):g} 100'/>{marcas}</svg>"
            f"<div class='ra-score'><strong>{score}</strong><span>/100</span><em>score de risco</em></div></div>")


def _laudo(r, colaborador: str, valor_solicitado) -> str:
    cont = {s: sum(c.status == s for c in r.checagens) for s in ORDEM}
    contagem = "".join(
        f"<div class='ra-cont st-{s}{' zero' if not cont[s] else ''}'><strong>{cont[s]}</strong>"
        f"<span>{STATUS[s][0]}</span></div>" for s in ORDEM)
    nome = (r.dados.get("arquivo") or {}).get("nome")
    meta = []
    if nome:
        meta.append(f"<span>Arquivo <b>{_e(nome)}</b></span>")
    if colaborador:
        meta.append(f"<span>Colaborador <b>{_e(colaborador)}</b></span>")
    if valor_solicitado is not None:
        meta.append(f"<span>Solicitado <b>{_e(brl(float(valor_solicitado)))}</b></span>")
    if r.id_historico:
        meta.append(f"<span>Histórico <b>#{r.id_historico}</b></span>")
    banner = ""
    if r.erro_llm:
        banner = (f"<div class='ra-banner'><b>Extração por IA indisponível.</b> {_e(r.erro_llm)} "
                  "Os dados do documento não foram lidos nem conferidos; reanalise com o modelo conectado.</div>")
    return (f"<section class='ra-laudo {r.nivel}'>{_medidor(r.score)}<div class='ra-veredito'>"
            f"<span class='ra-carimbo'>{NIVEIS[r.nivel]}</span>"
            f"<p class='ra-recomendacao'>{_e(r.recomendacao)}</p>"
            f"<div class='ra-meta'>{''.join(meta)}</div>"
            f"<div class='ra-contagem'>{contagem}</div></div>{banner}</section>")


def _checagens(checagens) -> str:
    if not checagens:
        return "<p class='ra-nota'>Nenhuma checagem registrada.</p>"
    grupos = []
    for s in ORDEM:
        itens = [c for c in checagens if c.status == s]
        if not itens:
            continue
        rotulo, icone, aberto = STATUS[s]
        linhas = "".join(
            f"<tr><td class='st'><span class='ra-icone' aria-hidden='true'>{icone}</span></td>"
            f"<td class='cat'>{_e(c.categoria)}</td>"
            f"<td><span class='cat-m'>{_e(c.categoria)}</span>{_e(c.mensagem)}</td>"
            f"<td class='peso'><span class='ra-peso{'' if c.peso else ' nulo'}'>"
            f"{'+' + str(c.peso) if c.peso else '—'}</span></td></tr>"
            for c in sorted(itens, key=lambda c: -c.peso))
        grupos.append(
            f"<details class='ra-grupo st-{s}'{' open' if aberto else ''}><summary>"
            f"<span class='n'>{len(itens)}</span>{rotulo}<span class='seta' aria-hidden='true'>›</span></summary>"
            "<table class='ra-tabela'><thead><tr><th class='st'><span class='sr'></span></th>"
            "<th class='cat'>Categoria</th><th>Detalhe</th><th class='peso'>Peso</th></tr></thead>"
            f"<tbody>{linhas}</tbody></table></details>")
    return f"<div class='ra-checks'>{''.join(grupos)}</div>"


def _cartao(rotulo, valor, classe="", extra="", mono=False) -> str:
    vazio = valor in (None, "", "-")
    v = "Não identificado" if vazio else _e(valor)
    cls = "vazio" if vazio else ("ra-chave" if mono else "")
    return (f"<div class='ra-cartao {classe}'><span>{rotulo}</span><strong class='{cls}'>{v}</strong>"
            f"{extra}</div>")


def _resumo(r, colaborador: str, valor_solicitado) -> str:
    res = r.dados.get("resumo", {})
    total = res.get("total")
    chave = res.get("chave_acesso")
    chave_fmt = " ".join(chave[i:i + 4] for i in range(0, len(chave), 4)) if chave else None

    extra_sol = ""
    if valor_solicitado is not None and total is not None:
        dif = float(valor_solicitado) - total
        extra_sol = ("<small class='ruim'>Acima do valor do documento</small>" if dif > 0.01
                     else "<small>Dentro do valor do documento</small>")
    cartoes = [
        _cartao("Total do documento", brl(total), "destaque largo"),
        _cartao("Valor solicitado", brl(float(valor_solicitado)) if valor_solicitado is not None else None,
                "largo", extra_sol),
        _cartao("Emitente", res.get("emitente"), "largo"),
        _cartao("CNPJ/CPF", res.get("cnpj"), mono=True),
        _cartao("Tipo", TIPOS.get(res.get("tipo"), res.get("tipo"))),
        _cartao("Data de emissão", res.get("data_emissao")),
        _cartao("Número", res.get("numero"), mono=True),
        _cartao("Pagamento", res.get("forma_pagamento")),
        _cartao("Colaborador", colaborador or None),
        _cartao("Chave de acesso", chave_fmt, "inteiro", mono=True),
    ]
    if r.id_historico:
        cartoes.append(_cartao("Registro no histórico", f"#{r.id_historico}"))
    rf = r.cnpj_receita or {}
    if rf.get("status") == "ok":
        local = "/".join(x for x in (rf.get("municipio"), rf.get("uf")) if x)
        cartoes.append(_cartao("Cadastro na Receita", rf.get("razao_social"), "largo",
                               f"<small>{_e(rf.get('situacao'))}{' · ' + _e(local) if local else ''}</small>"))

    html = f"<div class='ra-cartoes'>{''.join(cartoes)}</div>"
    links = [u for u in dict.fromkeys(r.links_consulta) if u.lower().startswith(("http://", "https://"))]
    html += "<h3 class='ra-sub'>Consulta na SEFAZ</h3>"
    if links:
        itens = "".join(
            f"<a href='{_e(u)}' target='_blank' rel='noopener noreferrer'><div><b>Consultar em "
            f"{_e(urlparse(u).hostname or 'SEFAZ')}</b><code>{_e(u[:110])}{'…' if len(u) > 110 else ''}</code>"
            "</div><span class='ir' aria-hidden='true'>↗</span></a>" for u in links)
        html += (f"<div class='ra-links'>{itens}</div><p class='ra-nota'>Confira se a nota existe e se o valor "
                 "bate. Os links abrem o portal oficial no navegador.</p>")
    else:
        html += "<p class='ra-nota'>Nenhum link oficial de consulta foi encontrado no documento.</p>"
    return html


# ---------------------------------------------------------------- ações
def _executar(arquivo, colaborador, valor_solicitado, valor_maximo, prazo, online, registrar):
    if not arquivo:
        raise gr.Error("Envie a imagem ou o PDF do comprovante.")
    padrao = Politica()
    pol = Politica(
        valor_maximo=float(valor_maximo) if valor_maximo else padrao.valor_maximo,
        prazo_dias=int(prazo) if prazo else padrao.prazo_dias,
        online=online, registrar=registrar)
    colaborador = (colaborador or "").strip()
    valor_solicitado = valor_solicitado if valor_solicitado else None
    try:
        r = analisar(arquivo, pol, colaborador=colaborador, valor_solicitado=valor_solicitado)
    except ValueError as e:  # arquivo vazio, corrompido, protegido por senha...
        raise gr.Error(str(e)) from e

    r.checagens.sort(key=lambda c: (ORDEM[c.status], -c.peso))
    return (_laudo(r, colaborador, valor_solicitado), _resumo(r, colaborador, valor_solicitado),
            _checagens(r.checagens), r.ela_mapa, r.dados, True)


def _status_llm():
    return _topo()


def _previa(arquivo):
    if not arquivo:
        return gr.Image(value=None, visible=False)
    try:
        img = fx.carregar(arquivo, max_paginas=1).paginas[0].copy()
        img.thumbnail((1400, 1400))
    except Exception:  # noqa: BLE001  prévia é cosmética; o erro real aparece na análise
        return gr.Image(value=None, visible=False)
    return gr.Image(value=img, visible=True)


def _iniciar():
    online = llm.disponivel()
    return (gr.Button(value="Analisando…", interactive=False), gr.Button(interactive=False),
            _topo(online), gr.HTML(value=_carregando(online), visible=True), gr.Column(visible=False), False)


def _finalizar(ok, arquivo):
    estado = gr.HTML(visible=False) if ok else gr.HTML(value=_erro() if arquivo else _vazio(), visible=True)
    return (gr.Button(value="Analisar comprovante", interactive=True), gr.Button(interactive=True),
            estado, gr.Column(visible=bool(ok)))


def _nova_analise():
    return (None, "", None, gr.Image(value=None, visible=False), gr.HTML(value=_vazio(), visible=True),
            gr.Column(visible=False), "", "", "", None, None, False)


# ---------------------------------------------------------------- tema
PETROLEO = gr.themes.Color(
    c50="#eef7f8", c100="#d5ebee", c200="#acd6dc", c300="#7bbbc5", c400="#4a9aa8",
    c500="#1f7b8c", c600="#0b5d6e", c700="#0a4d5b", c800="#0b3f4a", c900="#0b343d", c950="#062027",
    name="petroleo")

TEMA = gr.themes.Base(
    primary_hue=PETROLEO, secondary_hue=PETROLEO, neutral_hue=gr.themes.colors.slate, radius_size="md",
    font=["Segoe UI Variable Text", "Segoe UI", "system-ui", "-apple-system", "Helvetica Neue", "sans-serif"],
    font_mono=["Cascadia Mono", "Cascadia Code", "Consolas", "ui-monospace", "monospace"],
).set(
    body_background_fill="#f3f1ec", body_background_fill_dark="#0a1016",
    block_background_fill="#ffffff", block_background_fill_dark="#121a21",
    block_border_color="#dcdfe2", block_border_color_dark="#26313b",
    border_color_primary="#dcdfe2", border_color_primary_dark="#26313b",
    input_background_fill="#fbfaf8", input_background_fill_dark="#0e151b",
    button_primary_background_fill="#0b5d6e", button_primary_background_fill_dark="#62c6d8",
    button_primary_background_fill_hover="#0a4d5b", button_primary_background_fill_hover_dark="#86d4e2",
    button_primary_text_color="#ffffff", button_primary_text_color_dark="#06222a",
    button_large_radius="10px", block_radius="12px",
    link_text_color="#0b5d6e", link_text_color_dark="#62c6d8",
)


# ---------------------------------------------------------------- layout
with gr.Blocks(title="Auditoria de Reembolsos", analytics_enabled=False) as demo:
    topo = gr.HTML(_topo(False), elem_id="ra-topo")
    with gr.Row(equal_height=False):
        with gr.Column(scale=4, min_width=320, elem_id="ra-entrada"):
            gr.HTML("<div class='ra-secao'><b>1</b>Comprovante</div>")
            arquivo = gr.File(label="Imagem ou PDF do comprovante", type="filepath",
                              file_types=["image", ".pdf"])
            previa = gr.Image(label="Prévia do documento", type="pil", interactive=False, visible=False,
                              elem_id="ra-previa", height=360, buttons=["fullscreen"])
            gr.HTML("<div class='ra-secao'><b>2</b>Pedido de reembolso</div>")
            colaborador = gr.Textbox(label="Colaborador (matrícula ou nome)", placeholder="Ex.: 10234 ou Maria Souza")
            valor_sol = gr.Number(label="Valor solicitado (R$)", value=None, minimum=0, placeholder="0,00")
            with gr.Accordion("Política de reembolso", open=False):
                valor_max = gr.Number(label="Limite por reembolso (R$)", value=1000, minimum=0)
                prazo = gr.Number(label="Prazo máximo (dias desde a emissão)", value=90, minimum=0, precision=0)
                online = gr.Checkbox(label="Consultar CNPJ na Receita (BrasilAPI)", value=True,
                                     info="Envia apenas o CNPJ do emitente; nenhum dado do colaborador sai da máquina.")
                registrar = gr.Checkbox(label="Registrar no histórico (detecção de duplicidade)", value=True)
            with gr.Row(elem_id="ra-botoes"):
                botao = gr.Button("Analisar comprovante", variant="primary", size="lg", scale=3, elem_id="ra-analisar")
                novo = gr.Button("Nova análise", variant="secondary", size="lg", scale=2, elem_id="ra-nova")
            gr.HTML(f"<p class='ra-dica'>{ICONE_RELOGIO}<span>Em CPU, a análise com IA leva de 1 a 4 minutos. "
                    "O botão fica bloqueado até o resultado sair.</span></p>")

        with gr.Column(scale=7, min_width=360):
            estado = gr.HTML(_vazio(), elem_id="ra-estado")
            with gr.Column(visible=False) as resultados:
                cabecalho = gr.HTML(elem_id="ra-laudo")
                with gr.Tabs(elem_id="ra-abas"):
                    with gr.Tab("Checagens"):
                        checks = gr.HTML(elem_id="ra-checagens")
                    with gr.Tab("Dados extraídos"):
                        resumo = gr.HTML(elem_id="ra-dados")
                    with gr.Tab("Mapa forense (ELA)"):
                        gr.HTML("<p class='ra-ela-legenda'>O ELA (Error Level Analysis) realça diferenças de "
                                "compressão. Áreas claras <b>isoladas</b> sobre valores, datas ou nomes indicam "
                                "possível edição; brilho uniforme em textos e bordas é normal.</p>")
                        ela = gr.Image(label="Áreas claras isoladas = recompressão diferente", type="pil",
                                       interactive=False, buttons=["download", "fullscreen"])
                    with gr.Tab("JSON completo"):
                        bruto = gr.JSON(label="Dados completos da análise")

    ok = gr.State(False)

    demo.load(_status_llm, outputs=topo, show_progress="hidden")
    gr.Timer(30).tick(_status_llm, outputs=topo, show_progress="hidden", concurrency_id="status",
                      concurrency_limit=None)
    arquivo.change(_previa, arquivo, previa, show_progress="minimal", concurrency_id="previa",
                   concurrency_limit=None)

    botao.click(_iniciar, outputs=[botao, novo, topo, estado, resultados, ok], show_progress="hidden").then(
        _executar, [arquivo, colaborador, valor_sol, valor_max, prazo, online, registrar],
        [cabecalho, resumo, checks, ela, bruto, ok], show_progress="hidden").then(
        _finalizar, [ok, arquivo], [botao, novo, estado, resultados], show_progress="hidden")
    novo.click(_nova_analise, outputs=[arquivo, colaborador, valor_sol, previa, estado, resultados,
                                       cabecalho, resumo, checks, ela, bruto, ok], show_progress="hidden")


# Força os textos nativos do Gradio (upload, botões, mensagens) em pt-BR,
# independentemente do idioma configurado no navegador.
FORCAR_PT_BR = """<script>
for (const p of ["language", "languages"]) {
  Object.defineProperty(navigator, p, {get: () => p === "language" ? "pt-BR" : ["pt-BR", "pt"]});
}
document.documentElement.lang = "pt-BR";
</script>"""


def main():
    demo.queue().launch(server_name="127.0.0.1", server_port=7860, theme=TEMA, css=CSS, footer_links=["api", "settings"],
                        head=FORCAR_PT_BR + f"<script>{JS}</script>")


if __name__ == "__main__":
    main()
