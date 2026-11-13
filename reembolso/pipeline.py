"""Orquestra a análise: carrega o documento, extrai dados, roda checagens e calcula o risco."""
from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date, datetime

from . import forensics as fx
from . import historico, llm, normas, online
from .validators import (acha_chave, brl, limpa, para_data, para_float, parse_chave,
                         valida_cnpj, valida_cpf)


@dataclass
class Politica:
    valor_maximo: float | None = None   # limite interno opcional (a IN 001-004 não fixa teto)
    prazo_dias: int | None = None       # None = prazo da memória normativa (IN 001-004 §4.3: 360 dias)
    online: bool = True          # consulta CNPJ na BrasilAPI
    registrar: bool = True       # grava no histórico para detectar duplicidade
    limiar_fraude: int = 50      # score a partir do qual há suspeita de fraude e o pedido é indeferido


@dataclass
class Checagem:
    categoria: str
    status: str   # "ok" | "info" | "alerta" | "falha"
    mensagem: str
    peso: int = 0


@dataclass
class Resultado:
    dados: dict = field(default_factory=dict)
    checagens: list[Checagem] = field(default_factory=list)
    score: int = 0
    nivel: str = "baixo"
    recomendacao: str = ""
    ela_mapa: object = None
    links_consulta: list[str] = field(default_factory=list)
    cnpj_receita: dict | None = None
    erro_llm: str | None = None
    id_historico: int | None = None
    decisao: str = "pendente"            # "deferido" | "deferido_parcial" | "indeferido" | "pendente"
    valor_documento: float | None = None
    valor_glosado: float = 0.0           # itens sem cobertura
    valor_reembolsavel: float | None = None
    glosas: list[str] = field(default_factory=list)
    pagina_corrigida: object = None      # 1ª página endireitada, quando a imagem veio girada
    motivos: list[str] = field(default_factory=list)      # por que foi indeferido / está pendente
    exigencias: list[str] = field(default_factory=list)   # condições das normas para o deferimento
    base_normativa: str = ""
    avisos_normas: list[str] = field(default_factory=list)

    def para_dict(self) -> dict:
        """Resultado em formato JSON (API e linha de comando)."""
        return {"decisao": self.decisao, "valor_reembolsavel": self.valor_reembolsavel,
                "valor_documento": self.valor_documento, "valor_glosado": self.valor_glosado,
                "glosas": self.glosas, "motivos": self.motivos, "exigencias": self.exigencias,
                "base_normativa": self.base_normativa, "avisos_normas": self.avisos_normas,
                "score": self.score, "nivel": self.nivel, "recomendacao": self.recomendacao,
                "checagens": [asdict(c) for c in self.checagens], "links_consulta": self.links_consulta,
                "id_historico": self.id_historico, "dados": self.dados}

    def add(self, categoria, status, mensagem, peso=0):
        self.checagens.append(Checagem(categoria, status, mensagem, peso if status in ("alerta", "falha") else 0))


def _norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().upper()
    for t in (" LTDA", " EIRELI", " S/A", " S.A.", " SA", " ME", " EPP", " MEI", "-", ".", ","):
        s = s.replace(t, " ")
    return " ".join(s.split())


def _lido(valor: str | None) -> str | None:
    """Normaliza um identificador lido pela IA; preenchimentos como "00000000000000" viram None."""
    v = limpa(valor)
    # chave real tem no máximo ~12 zeros seguidos (ex.: CNPJ do Banco do Brasil)
    if not v or len(set(v)) == 1 or re.search(r"0{16,}", v):
        return None
    return v


# Frases com que o modelo às vezes "lista" um não-indício
_NAO_INDICIO = re.compile(r"n[ãa]o (é|há|ha|foram?) (um )?ind[íi]cio|sem ind[íi]cios|não há rasura", re.I)


ROTULO_ORIENTACAO = {"de_cabeca_para_baixo": "de cabeça para baixo", "topo_a_direita": "deitada (topo à direita)",
                     "topo_a_esquerda": "deitada (topo à esquerda)"}


def _endireitar(r: Resultado, doc, ext: dict, max_correcoes: int = 2) -> dict:
    """Se a IA disser que a imagem está girada, endireita as páginas e lê de novo (no máximo 2 vezes)."""
    giros = []
    for _ in range(max_correcoes):
        ori = ext.get("orientacao") or "normal"
        graus = llm.ORIENTACOES.get(ori, 0)
        if not graus:
            break
        giros.append(ori)
        doc.paginas = [p.rotate(graus, expand=True) for p in doc.paginas]
        ext = llm.analisar_imagem(doc.paginas, doc.texto_pdf)
    if giros:
        r.pagina_corrigida = doc.paginas[0]
        ainda = (ext.get("orientacao") or "normal") != "normal"
        r.add("Documento", "alerta" if ainda else "info",
              f"A imagem estava {ROTULO_ORIENTACAO.get(giros[0], giros[0])} e foi girada automaticamente para a leitura."
              + (" Ainda não ficou na posição normal: confira a leitura ou peça outra foto." if ainda else ""))
        if ainda:
            ext["legibilidade"] = "ruim"   # leva a decisão para "pendente"
    return ext


def _conferir_soma(r: Resultado, itens: list[dict], total: float, desc: float, acr: float) -> None:
    """Confere a soma dos itens com o total, tolerando erros típicos de leitura da IA."""
    tol = max(0.05, total * 0.005)

    def soma(usar_qtd: bool) -> float:
        s = 0.0
        for i in itens:
            v = para_float(i.get("valor_total"))
            q, u = para_float(i.get("quantidade")), para_float(i.get("valor_unitario"))
            if usar_qtd and q and u:
                v = round(q * u, 2)
            s += v or 0.0
        return round(s - desc + acr, 2)

    esperado = soma(False)
    if abs(esperado - total) <= tol:
        r.add("Valores", "ok", f"Soma dos itens confere com o total ({brl(total)}).")
        return
    if abs(soma(True) - total) <= tol:  # Vl.Tot lido errado, mas quantidade × unitário fecha
        r.add("Valores", "ok", f"Soma dos itens (quantidade × valor unitário) confere com o total ({brl(total)}).")
        return
    dif = round(total - esperado, 2)
    # A diferença é exatamente o valor de um item (ou um múltiplo pequeno)? Então a IA quase certamente
    # leu a quantidade errado (ex.: "2 UN 93,00 186,00" lido como 93,00). Não é indício de fraude.
    for i in itens:
        u = para_float(i.get("valor_unitario")) or para_float(i.get("valor_total"))
        if u and dif > 0 and any(abs(dif - k * u) <= tol for k in (1, 2, 3)):
            r.add("Valores", "info", f"Soma dos itens lidos ({brl(esperado)}) fica {brl(dif)} abaixo do total "
                  f"({brl(total)}), exatamente o valor de \"{str(i.get('descricao') or '').strip()}\": "
                  "provável quantidade lida errado pela IA. Confira o item na nota.")
            return
    r.add("Valores", "alerta", f"Soma dos itens ({brl(esperado)}) ≠ total ({brl(total)}).", 25)


# Portais oficiais de consulta (a página pede a chave e um captcha). NFC-e é estadual: só as UFs
# com endereço conferido entram aqui; para as demais, a tela orienta a usar o portal da SEFAZ da UF.
PORTAL_NFE = "https://www.nfe.fazenda.gov.br/portal/consultaRecaptcha.aspx?tipoConsulta=resumo&tipoConteudo=7PhJ+gAVw2g="
PORTAIS_NFCE = {"SP": "https://www.nfce.fazenda.sp.gov.br/qrcode"}


def consulta_sefaz(pc: dict) -> list[str]:
    if pc.get("modelo") == "NF-e":
        return [PORTAL_NFE]
    if pc.get("modelo") == "NFC-e" and pc.get("uf") in PORTAIS_NFCE:
        return [PORTAIS_NFCE[pc["uf"]]]
    return []


def _similar(a: str | None, b: str | None) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def analisar(caminho: str, politica: Politica | None = None, colaborador: str = "",
             valor_solicitado: float | None = None, hoje: date | None = None) -> Resultado:
    pol = politica or Politica()
    hoje = hoje or date.today()
    r = Resultado()

    # ---------- 1. Arquivo, metadados e forense ----------
    doc = fx.carregar(caminho)
    pagina = doc.paginas[0]
    r.dados["arquivo"] = {"nome": doc.caminho.name, "tipo": doc.tipo, "sha256": doc.sha256,
                          "metadados": doc.metadados}

    editor = fx.editor_detectado(doc.metadados)
    if editor:
        r.add("Metadados", "falha", f"Arquivo gerado/salvo por editor: '{editor}'.", 30)
    if doc.tipo == "pdf":
        if doc.pdf_versoes > 1:
            r.add("Metadados", "alerta", f"PDF com {doc.pdf_versoes} revisões salvas (editado após a criação).", 20)
        cri, mod = doc.metadados.get("creationDate"), doc.metadados.get("modDate")
        if cri and mod and cri[:16] != mod[:16]:
            r.add("Metadados", "alerta", f"PDF modificado depois de criado ({cri[2:16]} → {mod[2:16]}).", 10)
        if not doc.texto_pdf.strip():
            r.add("Metadados", "info", "PDF sem camada de texto (é uma imagem escaneada/foto).")
    elif not editor:
        r.add("Metadados", "ok" if doc.metadados.get("Make") else "info",
              "Foto de câmera: " + f"{doc.metadados.get('Make', '')} {doc.metadados.get('Model', '')}".strip()
              if doc.metadados.get("Make") else "Imagem sem dados de câmera (normal para prints e WhatsApp).")

    mapa, escore_ela = fx.ela(pagina)
    r.ela_mapa = mapa
    if escore_ela >= 6:
        r.add("Forense", "alerta", f"ELA: regiões com recompressão bem diferente do resto (escore {escore_ela}). "
              "Confira o mapa: áreas que brilham isoladas sobre valores/datas são suspeitas.", 10)
    else:
        r.add("Forense", "ok", f"ELA sem regiões discrepantes relevantes (escore {escore_ela}).")

    # QR Code (NFC-e / CF-e SAT)
    qrs = []
    for p in doc.paginas:
        qrs += fx.ler_qrcodes(p)
    chave_qr = next((c for c in (fx.chave_do_qr(q) for q in qrs) if c), None)
    r.links_consulta += [q for q in qrs if fx.eh_url_consulta(q)]
    r.dados["qrcodes"] = qrs
    for q in qrs:
        if fx.eh_url(q) and not fx.eh_url_consulta(q):
            r.add("QR Code", "alerta", f"QR Code aponta para site não oficial: {q[:80]}", 5)
        elif fx.eh_url_consulta(q):
            r.add("QR Code", "ok", f"QR Code de consulta oficial: {q[:80]}")

    # ---------- 2. Extração com o modelo de visão ----------
    ext: dict = {}
    if llm.disponivel():
        try:
            ext = llm.analisar_imagem(doc.paginas, doc.texto_pdf)
            ext = _endireitar(r, doc, ext)
        except Exception as e:  # noqa: BLE001
            r.erro_llm = f"{type(e).__name__}: {e}"
    else:
        r.erro_llm = "Servidor do modelo não está rodando (python main.py)."
    # pHash depois da correção de orientação: a mesma nota fotografada de cabeça para baixo é duplicata.
    ph = fx.phash(doc.paginas[0])
    if r.erro_llm:
        r.add("Extração", "alerta", f"Extração por IA indisponível: {r.erro_llm}", 0)
        if pol.registrar:
            r.add("Duplicidade", "info", "Análise incompleta: não registrada no histórico.")
    r.dados["extraido"] = ext

    emit = ext.get("emitente") or {}
    cnpj = _lido(emit.get("cnpj"))
    cpf = _lido(emit.get("cpf"))
    total = para_float(ext.get("total"))
    data_em = para_data(ext.get("data_emissao"))
    # "Nº 000.012.345" (DANFE) → "12345", para comparar com a chave e com o histórico
    numero = limpa(ext.get("numero_documento")).lstrip("0") or None
    tipo = ext.get("tipo_documento")

    # chave: QR > texto do PDF > leitura do modelo
    chave_pdf = acha_chave(doc.texto_pdf)
    # O modelo às vezes devolve o link do QR Code no lugar da chave: extrai a chave do link.
    chave_bruta = str(ext.get("chave_acesso") or "")
    # URL lida pela IA NÃO vira link (ela erra letras: "nfce.fezenda..."); os links oficiais são
    # montados a partir da chave validada (consulta_sefaz) ou vêm do QR Code decodificado.
    if fx.eh_url(chave_bruta):
        chave_llm = fx.chave_do_qr(chave_bruta) or acha_chave(chave_bruta)
    else:
        chave_llm = _lido(chave_bruta)
    chave = chave_qr or chave_pdf or chave_llm

    if ext.get("legibilidade") == "ruim":
        r.add("Extração", "alerta", "Documento pouco legível: peça reenvio de uma imagem melhor.", 10)

    # o modelo às vezes repete o protocolo da SEFAZ no campo do código do cartão
    if ext.get("autorizacao_pagamento") and limpa(ext.get("autorizacao_pagamento")) == limpa(ext.get("protocolo_autorizacao")):
        ext["autorizacao_pagamento"] = None

    # ---------- 3. Regras determinísticas ----------
    # Emitente
    if cnpj:
        if valida_cnpj(cnpj):
            r.add("Emitente", "ok", f"CNPJ {cnpj} com dígitos verificadores válidos.")
        else:
            r.add("Emitente", "falha", f"CNPJ {emit.get('cnpj')} é INVÁLIDO (dígito verificador não confere).", 35)
    elif cpf:
        st = "ok" if valida_cpf(cpf) else "falha"
        r.add("Emitente", st, f"Emitente pessoa física, CPF {'válido' if st == 'ok' else 'INVÁLIDO'}.", 30)
    elif ext:
        r.add("Emitente", "alerta", "Documento sem CNPJ/CPF do emitente identificável.", 20)

    if cnpj and valida_cnpj(cnpj) and pol.online:
        rf = online.consulta_cnpj(cnpj)
        r.cnpj_receita = rf
        if rf["status"] == "nao_encontrado":
            r.add("Receita Federal", "falha", "CNPJ não encontrado na base da Receita.", 40)
        elif rf["status"] == "erro":
            r.add("Receita Federal", "info", f"Consulta de CNPJ indisponível ({rf['erro']}).")
        else:
            if (rf.get("situacao") or "").upper() != "ATIVA":
                r.add("Receita Federal", "falha", f"CNPJ com situação cadastral '{rf.get('situacao')}'.", 30)
            else:
                r.add("Receita Federal", "ok", f"CNPJ ATIVO — {rf.get('razao_social')} ({rf.get('municipio')}/{rf.get('uf')}).")
            ini = para_data(rf.get("data_inicio_atividade"))
            if ini and data_em and data_em < ini:
                r.add("Receita Federal", "falha", f"Documento emitido ({data_em:%d/%m/%Y}) antes da abertura "
                      f"da empresa ({ini:%d/%m/%Y}).", 40)
            sim = max(_similar(emit.get("razao_social"), rf.get("razao_social")),
                      _similar(emit.get("razao_social"), rf.get("nome_fantasia")),
                      _similar(emit.get("nome_fantasia"), rf.get("nome_fantasia")))
            if emit.get("razao_social") and sim < 0.6:
                r.add("Receita Federal", "alerta", f"Nome no documento ('{emit.get('razao_social')}') difere do "
                      f"cadastro ('{rf.get('razao_social')}').", 15)
            if emit.get("uf") and rf.get("uf") and emit["uf"].upper() != rf["uf"].upper():
                r.add("Receita Federal", "alerta", f"UF do documento ({emit['uf']}) ≠ UF do cadastro ({rf['uf']}).", 10)
            if rf.get("cnae"):
                r.add("Receita Federal", "info", f"Atividade (CNAE): {rf['cnae']} — confira se combina com a despesa.")

    # Chave de acesso
    if chave:
        pc = parse_chave(chave)
        fonte = "QR Code" if chave == chave_qr else ("texto do PDF" if chave == chave_pdf else "leitura da IA")
        if not pc and fonte == "leitura da IA":
            # leitura parcial pela IA (foto/scan) não é prova de adulteração
            r.add("Chave de acesso", "alerta", f"Chave lida pela IA com {len(chave)} posições (esperado: 44): "
                  "confira a chave impressa manualmente.", 10)
        elif not pc:
            r.add("Chave de acesso", "falha", f"Chave de acesso com tamanho inválido ({len(chave)} posições).", 30)
        else:
            r.dados["chave"] = pc
            if pc["dv_ok"]:
                r.add("Chave de acesso", "ok", f"Chave válida ({pc['modelo']}, {pc['uf']}, {pc['ano_mes']}), lida via {fonte}.")
            else:
                peso = 40 if fonte != "leitura da IA" else 15  # IA pode errar um dígito
                r.add("Chave de acesso", "falha" if peso == 40 else "alerta",
                      f"Dígito verificador da chave NÃO confere (fonte: {fonte}).", peso)
            # Conferências cruzadas só com chave confiável: se a IA já errou dígitos da chave (DV inválido),
            # comparar mês/número/CNPJ com ela gera alarme falso.
            if pc["dv_ok"] or fonte != "leitura da IA":
                if cnpj and valida_cnpj(cnpj) and pc["cnpj_emitente"] != cnpj:
                    r.add("Chave de acesso", "falha", f"CNPJ embutido na chave ({pc['cnpj_emitente']}) ≠ CNPJ impresso ({cnpj}).", 35)
                if data_em and pc["ano_mes"] != f"{data_em:%Y-%m}":
                    r.add("Chave de acesso", "alerta", f"Mês da chave ({pc['ano_mes']}) ≠ data de emissão ({data_em:%m/%Y}).", 25)
                if numero and pc["numero"] != numero:
                    r.add("Chave de acesso", "alerta", f"Número na chave ({pc['numero']}) ≠ número impresso ({numero}).", 15)
        if chave_qr and chave_llm and len(chave_llm) == 44 and chave_qr != chave_llm:
            dif = sum(a != b for a, b in zip(chave_qr, chave_llm))
            if dif > 3:  # poucas diferenças = provável erro de leitura da IA
                r.add("Chave de acesso", "falha", "Chave impressa difere da chave do QR Code.", 40)
        if pc and pc["dv_ok"]:
            r.links_consulta += consulta_sefaz(pc)
    elif tipo in ("nfce", "nfe_danfe", "cupom_sat"):
        r.add("Chave de acesso", "alerta", "Documento fiscal eletrônico sem chave de acesso legível.", 15)
    elif tipo == "recibo_simples":
        r.add("Chave de acesso", "info", "Recibo simples não tem chave fiscal: validação depende de outras evidências.")

    # Valores
    itens = [i for i in (ext.get("itens") or []) if isinstance(i, dict)]
    desc = para_float(ext.get("desconto")) or 0.0
    acr = para_float(ext.get("acrescimo")) or 0.0
    if total is None and ext:
        r.add("Valores", "alerta", "Valor total não identificado.", 15)
    if total is not None and itens:
        _conferir_soma(r, itens, total, desc, acr)
    pago = para_float(ext.get("valor_pago"))
    if pago is not None and total is not None and pago + 0.01 < total:
        r.add("Valores", "alerta", f"Valor pago ({brl(pago)}) menor que o total da nota ({brl(total)}).", 10)
    if total is not None:
        if pol.valor_maximo and total > pol.valor_maximo:
            r.add("Política", "alerta", f"Valor {brl(total)} acima do limite da política ({brl(pol.valor_maximo)}).", 10)
        if tipo == "recibo_simples" and total >= 50 and total == int(total) and int(total) % 10 == 0:
            r.add("Valores", "alerta", f"Recibo manual com valor redondo ({brl(total)}).", 5)
    if doc.texto_pdf.strip() and total is not None:
        alvo = brl(total).replace("R$ ", "")
        if alvo not in doc.texto_pdf and f"{total:.2f}" not in doc.texto_pdf:
            r.add("Valores", "alerta", f"Total lido ({alvo}) não aparece no texto do PDF: imagem e texto divergem.", 15)

    # Datas
    if data_em:
        if data_em > hoje:
            r.add("Datas", "falha", f"Data de emissão no futuro ({data_em:%d/%m/%Y}).", 30)
        else:
            r.add("Datas", "ok", f"Emitido em {data_em:%d/%m/%Y}.")
        if data_em.weekday() >= 5:
            r.add("Datas", "info", f"Emitido em fim de semana ({['sáb', 'dom'][data_em.weekday() - 5]}).")
    elif ext:
        r.add("Datas", "alerta", "Data de emissão não identificada.", 15)

    # Sinais visuais apontados pelo modelo
    # "baixa" é só observação (o modelo de 4B gera muito ruído aí); média/alta somam até 35.
    pesos_g = {"media": 10, "alta": 20}
    acumulado = 0
    for s in ext.get("sinais_suspeitos") or []:
        if _NAO_INDICIO.search(s.get("descricao") or ""):
            continue
        grav = s.get("gravidade")
        texto = f"[{grav}] {s.get('descricao')}"
        if grav not in pesos_g:
            r.add("Análise visual (IA)", "info", texto)
            continue
        p = min(pesos_g[grav], 35 - acumulado)
        acumulado += p
        r.add("Análise visual (IA)", "alerta", texto, p)

    # Portal de consulta pelo tipo e UF, mesmo se a chave lida tiver erro de dígito (o portal pede a chave impressa)
    if not r.links_consulta:
        uf = (emit.get("uf") or "").upper() or ((r.dados.get("chave") or {}).get("uf") or "")
        modelo = {"nfce": "NFC-e", "nfe_danfe": "NF-e"}.get(tipo)
        if modelo:
            r.links_consulta += consulta_sefaz({"modelo": modelo, "uf": uf})

    # ---------- 4. Normas (memória das Instruções Normativas) ----------
    mem = normas.carregar()
    r.base_normativa, r.avisos_normas = mem.base, mem.pendencias
    for aviso in mem.pendencias:
        r.add("Normas", "info", aviso)
    status_norma = {"indeferir": "falha", "condicionar": "alerta", "alertar": "alerta", "ok": "ok"}
    achados = normas.avaliar(mem, ext, tipo=tipo, data_em=data_em, hoje=hoje, total=total,
                             valor_solicitado=valor_solicitado, prazo_dias=pol.prazo_dias)
    for a in achados:
        # Regras de norma decidem o deferimento, mas não somam no score de FRAUDE —
        # exceto pedir mais do que o comprovante, que é tentativa de vantagem indevida.
        peso = 35 if a.titulo == "Vantagem indevida" else 0
        r.add(f"Norma · {a.ref}", status_norma[a.efeito], a.mensagem, peso)

    # ---------- 5. Duplicidade ----------
    dups = historico.buscar_duplicados(doc.sha256, ph, chave, cnpj, numero,
                                       f"{data_em:%Y-%m-%d}" if data_em else None, total)
    if dups:
        # Uma única checagem, por mais registros que batam: o mais antigo em detalhe, os demais por número.
        d = dups[0]
        quem = f" por '{d['colaborador']}'" if d["colaborador"] else ""
        texto = (f"Possível duplicata do reembolso #{d['id']} ({d['motivo']}), "
                 f"enviado em {d['em'].replace('T', ' ')}{quem}.")
        if len(dups) > 1:
            outros = ", ".join(f"#{x['id']}" for x in dups[1:6]) + (" …" if len(dups) > 6 else "")
            texto += f" Também coincide com {len(dups) - 1} outro(s): {outros}."
        r.dados["duplicatas"] = dups
        r.add("Duplicidade", "falha", texto, 60)
    else:
        r.add("Duplicidade", "ok", "Nenhum reembolso semelhante no histórico.")

    # ---------- 6. Score ----------
    r.score = min(100, sum(c.peso for c in r.checagens))
    if r.score >= 50:
        r.nivel, r.recomendacao = "alto", "Bloquear e encaminhar para auditoria."
    elif r.score >= 20:
        r.nivel, r.recomendacao = "medio", "Revisão humana antes de aprovar."
    else:
        r.nivel, r.recomendacao = "baixo", "Sem indícios relevantes; seguir fluxo normal."
    if r.erro_llm and r.nivel != "alto":
        r.nivel = "indeterminado"
        r.recomendacao = "Análise incompleta (IA indisponível): dados não foram extraídos nem conferidos."

    # ---------- 7. Decisão: deferido / indeferido ----------
    if dups:
        r.motivos.append(f"Reembolso em duplicidade com o registro #{dups[0]['id']} ({dups[0]['motivo']})"
                         + (f" — {mem.vantagem_ref}" if mem.vantagem_ref else "") + ".")
    fraude = r.score >= pol.limiar_fraude
    if fraude:
        r.motivos.append(f"Suspeita de fraude: score de risco {r.score}/100 (limite {pol.limiar_fraude}).")
    # Problemas do DOCUMENTO (prazo, comprovante, vantagem) indeferem tudo; itens sem cobertura só são glosados.
    r.motivos += [f"{a.mensagem} ({a.ref})" for a in achados if a.efeito == "indeferir" and a.escopo == "documento"]
    glosas = [a for a in achados if a.efeito == "indeferir" and a.escopo == "item"]
    r.glosas = [f"{a.mensagem} ({a.ref})" for a in glosas]
    r.exigencias = [f"{a.mensagem} ({a.ref})" for a in achados if a.efeito in ("condicionar", "alertar")]
    r.valor_documento = total
    r.valor_glosado = round(sum(a.valor for a in glosas), 2)
    if r.motivos:
        r.decisao, r.valor_reembolsavel = "indeferido", 0.0
        r.recomendacao = "Indeferir: " + ("suspeita de fraude." if fraude or dups
                                           else "não atende às normas de reembolso.")
    elif r.erro_llm:
        r.decisao = "pendente"
        r.motivos.append("Análise incompleta: a IA não leu o comprovante, então as normas não puderam ser "
                         "verificadas. Reanalise com o modelo de visão no ar.")
    elif ext.get("legibilidade") == "ruim":
        r.decisao = "pendente"
        r.motivos.append("Comprovante pouco legível: peça uma imagem melhor antes de decidir.")
    else:
        base = total if valor_solicitado is None else min(total, valor_solicitado)
        r.valor_reembolsavel = round(max(0.0, min(base, total - r.valor_glosado)), 2)
        if glosas and r.valor_reembolsavel <= 0.01:
            r.decisao, r.valor_reembolsavel = "indeferido", 0.0
            r.motivos += r.glosas
            r.recomendacao = "Indeferir: nenhum item do comprovante tem cobertura pela norma."
        elif glosas:
            r.decisao = "deferido_parcial"
            r.recomendacao = (f"Deferir parcialmente: reembolsar {brl(r.valor_reembolsavel)} e glosar "
                              f"{brl(r.valor_glosado)} em itens sem cobertura.")
        else:
            r.decisao = "deferido"
            r.recomendacao = (f"Deferir {brl(r.valor_reembolsavel)}"
                              + (", atendidas as condições listadas." if r.exigencias
                                 else ": atende às normas e não há indícios de fraude."))

    # Análise incompleta não vai para o histórico: senão, ao reanalisar o mesmo arquivo
    # com a IA no ar, ele seria acusado como duplicata de si mesmo.
    if pol.registrar and not r.erro_llm:
        r.id_historico = historico.registrar(colaborador, doc.caminho.name, doc.sha256, ph, chave, cnpj,
                                             numero, f"{data_em:%Y-%m-%d}" if data_em else None, total, r.score)
    r.dados["resumo"] = {
        "tipo": tipo, "emitente": emit.get("razao_social"), "cnpj": cnpj or cpf,
        "data_emissao": f"{data_em:%d/%m/%Y}" if data_em else None, "numero": numero,
        "chave_acesso": chave, "total": total, "forma_pagamento": ext.get("forma_pagamento"),
        "valor_pago": para_float(ext.get("valor_pago")), "hora_emissao": ext.get("hora_emissao"),
        "protocolo_autorizacao": ext.get("protocolo_autorizacao"),
        "autorizacao_pagamento": ext.get("autorizacao_pagamento"),
        "analisado_em": datetime.now().isoformat(timespec="seconds"),
    }
    return r
