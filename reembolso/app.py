"""Interface web local: python -m reembolso.app  →  http://127.0.0.1:7860"""
from __future__ import annotations

import math
import time
from html import escape
from pathlib import Path
from urllib.parse import urlparse

import gradio as gr
from gradio.data_classes import FileData

from . import forensics as fx
from . import llm
from .pipeline import Politica, analisar
from .validators import brl, para_float

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

RECIBO = """<svg width="92" height="116" viewBox="0 0 92 116" aria-hidden="true">
<path d="M8 4h76v104l-9.5-6-9.5 6-9.5-6-9.5 6-9.5-6-9.5 6-9.5-6z" fill="var(--ra-surface-2)" stroke="var(--ra-line-strong)" stroke-width="2" stroke-linejoin="round"/>
<g stroke="var(--ra-line-strong)" stroke-width="3" stroke-linecap="round">
<path d="M22 22h48"/><path d="M22 34h30"/><path d="M22 50h48"/><path d="M22 60h48"/><path d="M22 70h36"/>
</g><path d="M50 86h20" stroke="var(--ra-accent)" stroke-width="4" stroke-linecap="round"/></svg>"""


def _topo(online: bool | None = None) -> str:
    online = llm.disponivel() if online is None else online
    # Com o modelo no ar não há o que mostrar; fora do ar, o aviso explica por que a análise sai incompleta.
    status = "" if online else (
        "<div class='ra-status off' role='status'><span class='ra-ponto'></span>"
        "<div>Modelo de visão fora do ar<small>A análise sai incompleta. Inicie com <code>python main.py</code>"
        "</small></div></div>")
    return (f"<header class='ra-topo'><div class='ra-marca'>{LOGO}<div><h1>Auditoria de Reembolsos</h1>"
            "<p>Análise de risco de fraude em comprovantes"
            "<span class='ra-local' title='Nenhum dado do comprovante sai desta máquina'>● 100% local</span></p>"
            f"</div></div>{status}</header>")


def _vazio() -> str:
    chips = "".join(f"<span>{c}</span>" for c in (
        "CNPJ na Receita Federal", "Chave de acesso e QR Code", "Soma dos itens", "Limite e prazo",
        "Datas", "Duplicidade", "Metadados do arquivo", "Forense de imagem (ELA)"))
    return ("<section class='ra-estado vazio'>"
            "<div class='ra-estado-cab'><span class='ra-seta' aria-hidden='true'>←</span><div>"
            "<h2>Comece pelo comprovante</h2>"
            "<p>Solte a foto ou o PDF no painel <b>Documento</b>. O laudo de risco aparece aqui, "
            "ao lado do documento, para você conferir um contra o outro.</p></div></div>"
            "<ol class='ra-passos'>"
            "<li><strong>Envie o comprovante</strong>NFC-e, DANFE, cupom SAT, recibo — foto, print ou PDF.</li>"
            "<li><strong>Identifique o pedido</strong>Colaborador e valor solicitado, na barra acima.</li>"
            "<li><strong>Analise</strong>Score de 0 a 100, checagens e dados extraídos.</li></ol>"
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
            "<p>Com GPU leva alguns segundos; só em CPU, de <b>1 a 4 minutos</b>. "
            "O resultado aparece aqui automaticamente.</p>"
            "<div class='ra-barra'><i></i></div>"
            "<div class='ra-tempo'><div><span>Decorrido</span>"
            f"<strong data-ra-inicio='{inicio}'>00:00</strong></div>"
            "<div><span>Estimativa</span><strong>&lt; 4 min</strong></div></div>"
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
            f"stroke-dasharray='{max(score, 0.6):g} 100'/>{marcas}"
            f"<text class='num' x='100' y='90' text-anchor='middle' style='font-size:48px'>{score}</text>"
            "<text class='rot' x='100' y='106' text-anchor='middle' style='font-size:7.5px'>SCORE DE RISCO</text>"
            "</svg></div>")


def _laudo(r, colaborador: str, valor_solicitado) -> str:
    cont = {s: sum(c.status == s for c in r.checagens) for s in ORDEM}
    contagem = "".join(
        f"<div class='ra-cont st-{s}{' zero' if not cont[s] else ''}'><span class='ic' aria-hidden='true'>"
        f"{STATUS[s][1]}</span><strong>{cont[s]}</strong><span>{STATUS[s][0]}</span></div>" for s in ORDEM)
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
    return (f"<section class='ra-laudo {r.nivel}'><div class='ra-laudo-topo'>{_medidor(r.score)}"
            f"<div class='ra-veredito'><span class='ra-carimbo'>{NIVEIS[r.nivel]}</span>"
            f"<p class='ra-recomendacao'>{_e(r.recomendacao)}</p>"
            f"<div class='ra-meta'>{''.join(meta)}</div></div></div>"
            f"{banner}<div class='ra-contagem'>{contagem}</div></section>")


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
    texto_valor = str(valor_solicitado or "").strip()
    valor_solicitado = para_float(texto_valor) if texto_valor else None
    if texto_valor and valor_solicitado is None:
        raise gr.Error(f"Valor solicitado inválido: '{texto_valor}'. Use, por exemplo, 123,45.")
    valor_solicitado = valor_solicitado or None
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
    return (None, "", "", gr.Image(value=None, visible=False), gr.HTML(value=_vazio(), visible=True),
            gr.Column(visible=False), "", "", "", None, None, False)


RODAPE = """<footer class='ra-rodape'>
<details><summary>Usar via API</summary><div class='ra-rodape-corpo'>
<p><b>Para quê:</b> integrar outro sistema (ERP, fluxo de aprovação, planilha) e analisar comprovantes
sem abrir esta tela. Funciona só nesta máquina (<code>127.0.0.1</code>), com a aplicação rodando.</p>
<p><b>Como:</b> instale o cliente uma vez (<code>pip install gradio_client</code>) e chame <code>/analisar</code>:</p>
<pre><code>from gradio_client import Client, handle_file

cliente = Client("http://127.0.0.1:7860/")
r = cliente.predict(handle_file("comprovante.pdf"), "Maria Souza", 123.45,
                    api_name="/analisar")
print(r["score"], r["nivel"], r["recomendacao"])</code></pre>
<p>Parâmetros, nesta ordem: arquivo, colaborador, valor solicitado e, opcionais, limite (1000), prazo em dias (90),
consultar CNPJ (<code>True</code>) e registrar no histórico (<code>True</code>). A resposta é um JSON com score,
nível, recomendação, checagens e dados extraídos. Sem programar, use o terminal:
<code>python -m reembolso.cli comprovante.pdf --json</code>.</p>
</div></details>
<details><summary>Configurações</summary><div class='ra-rodape-corpo'>
<p><b>Política de reembolso</b> (limite, prazo, consulta de CNPJ, histórico): clique em <i>Política de
reembolso</i>, na barra do pedido. Vale a partir da próxima análise.</p>
<p><b>Tema da tela:</b>
<span class='ra-temas' role='group' aria-label='Tema'>
<button type='button' data-ra-tema='claro'>Claro</button><button type='button' data-ra-tema='escuro'>Escuro</button><button type='button' data-ra-tema='auto'>Automático</button></span></p>
<p><b>Modelo de IA:</b> definido ao iniciar. <code>python main.py --gpu-layers 0</code> usa só a CPU;
<code>--ctx 4096</code> economiza memória. Detalhes no README.</p>
</div></details>
</footer>"""


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


# ---------------------------------------------------------------- API
def analisar_api(arquivo: FileData, colaborador: str = "", valor_solicitado: float | None = None,
                 limite: float = 1000, prazo_dias: int = 90, consultar_cnpj: bool = True,
                 registrar: bool = True) -> dict:
    """Analisa um comprovante (imagem ou PDF) e devolve score, nível, checagens e dados extraídos."""
    caminho = arquivo.path if isinstance(arquivo, FileData) else (arquivo or {}).get("path", arquivo)
    pol = Politica(valor_maximo=limite, prazo_dias=int(prazo_dias), online=consultar_cnpj, registrar=registrar)
    try:
        r = analisar(caminho, pol, colaborador=(colaborador or "").strip(),
                     valor_solicitado=valor_solicitado or None)
    except ValueError as e:
        raise gr.Error(str(e)) from e
    return r.para_dict()


# ---------------------------------------------------------------- layout
# "Mesa de auditoria": o documento (a evidência) fica fixo à esquerda; à direita, de cima para
# baixo, o pedido (barra de ação), o veredito e os detalhes — lidos lado a lado com o documento.
with gr.Blocks(title="Auditoria de Reembolsos", analytics_enabled=False) as demo:
    topo = gr.HTML(_topo(True), elem_id="ra-topo", padding=False, container=False)
    with gr.Row(equal_height=False, elem_id="ra-mesa"):
        with gr.Column(scale=5, min_width=320, elem_id="ra-doc"):
            gr.HTML("<div class='ra-painel-tit'><span>Documento</span><small>foto, print ou PDF</small></div>",
                    padding=False, container=False)
            arquivo = gr.File(label="Comprovante", show_label=False, type="filepath",
                              file_types=["image", ".pdf"], elem_id="ra-upload")
            previa = gr.Image(label="Prévia do documento", show_label=False, type="pil", interactive=False,
                              visible=False, elem_id="ra-previa", height=640, buttons=["fullscreen"])

        with gr.Column(scale=7, min_width=360, elem_id="ra-analise"):
            with gr.Column(elem_id="ra-pedido"):
                with gr.Row(elem_id="ra-pedido-linha", equal_height=False):
                    colaborador = gr.Textbox(label="Colaborador", placeholder="Matrícula ou nome",
                                             scale=3, min_width=160)
                    valor_sol = gr.Textbox(label="Valor solicitado", placeholder="R$ 0,00",
                                           scale=2, min_width=120, elem_id="ra-valor")
                    with gr.Row(elem_id="ra-acoes"):  # os dois botões andam sempre juntos
                        botao = gr.Button("Analisar comprovante", variant="primary", size="lg",
                                          min_width=180, elem_id="ra-analisar")
                        novo = gr.Button("Nova análise", variant="secondary", size="lg",
                                         min_width=120, elem_id="ra-nova")
                with gr.Accordion("Política de reembolso", open=False, elem_id="ra-politica"):
                    with gr.Row():
                        valor_max = gr.Number(label="Limite por reembolso (R$)", value=1000, minimum=0,
                                              min_width=150)
                        prazo = gr.Number(label="Prazo máximo (dias)", value=90, minimum=0, precision=0,
                                          min_width=150)
                    with gr.Row():
                        online = gr.Checkbox(label="Consultar CNPJ na Receita (BrasilAPI)", value=True,
                                             info="Envia só o CNPJ do emitente; nada do colaborador sai da máquina.")
                        registrar = gr.Checkbox(label="Registrar no histórico", value=True,
                                                info="Necessário para detectar reembolsos duplicados.")

            estado = gr.HTML(_vazio(), elem_id="ra-estado", padding=False, container=False)
            with gr.Column(visible=False, elem_id="ra-resultados") as resultados:
                cabecalho = gr.HTML(elem_id="ra-laudo", padding=False, container=False)
                with gr.Tabs(elem_id="ra-abas"):
                    with gr.Tab("Checagens"):
                        checks = gr.HTML(elem_id="ra-checagens", padding=False, container=False)
                    with gr.Tab("Dados extraídos"):
                        resumo = gr.HTML(elem_id="ra-dados", padding=False, container=False)
                    with gr.Tab("Forense (ELA)"):
                        gr.HTML(padding=False, container=False, value="<p class='ra-ela-legenda'>O ELA (Error Level Analysis) realça diferenças de "
                                "compressão. Áreas claras <b>isoladas</b> sobre valores, datas ou nomes indicam "
                                "possível edição; brilho uniforme em textos e bordas é normal.</p>")
                        ela = gr.Image(label="Mapa ELA", show_label=False, type="pil",
                                       interactive=False, buttons=["download", "fullscreen"])
                    with gr.Tab("JSON"):
                        bruto = gr.JSON(label="Dados completos da análise", show_label=False)

    gr.HTML(RODAPE, padding=False, container=False, elem_id="ra-rodape")
    ok = gr.State(False)

    # Eventos internos da tela não aparecem na API; o endpoint público é só /analisar.
    interno = {"api_visibility": "private", "show_progress": "hidden"}
    demo.load(_status_llm, outputs=topo, **interno)
    gr.Timer(30).tick(_status_llm, outputs=topo, concurrency_id="status", concurrency_limit=None, **interno)
    arquivo.change(_previa, arquivo, previa, concurrency_id="previa", concurrency_limit=None,
                   api_visibility="private", show_progress="minimal")
    botao.click(_iniciar, outputs=[botao, novo, topo, estado, resultados, ok], **interno).then(
        _executar, [arquivo, colaborador, valor_sol, valor_max, prazo, online, registrar],
        [cabecalho, resumo, checks, ela, bruto, ok], **interno).then(
        _finalizar, [ok, arquivo], [botao, novo, estado, resultados], **interno)
    novo.click(_nova_analise, outputs=[arquivo, colaborador, valor_sol, previa, estado, resultados,
                                       cabecalho, resumo, checks, ela, bruto, ok], **interno)
    gr.api(analisar_api, api_name="analisar")


# Força os textos nativos do Gradio (upload, botões, mensagens) em pt-BR,
# independentemente do idioma configurado no navegador.
FORCAR_PT_BR = """<script>
for (const p of ["language", "languages"]) {
  Object.defineProperty(navigator, p, {get: () => p === "language" ? "pt-BR" : ["pt-BR", "pt"]});
}
document.documentElement.lang = "pt-BR";
</script>"""


def main():
    demo.queue().launch(server_name="127.0.0.1", server_port=7860, theme=TEMA, css=CSS, footer_links=[],
                        head=FORCAR_PT_BR + f"<script>{JS}</script>")


if __name__ == "__main__":
    main()
