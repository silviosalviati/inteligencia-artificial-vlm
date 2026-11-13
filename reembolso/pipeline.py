"""Orquestra a análise: carrega o documento, extrai dados, roda checagens e calcula o risco."""
from __future__ import annotations

import difflib
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime

from . import forensics as fx
from . import historico, llm, online
from .validators import (acha_chave, brl, limpa, para_data, para_float, parse_chave,
                         valida_cnpj, valida_cpf)


@dataclass
class Politica:
    valor_maximo: float = 1000.0
    prazo_dias: int = 90
    online: bool = True          # consulta CNPJ na BrasilAPI
    registrar: bool = True       # grava no histórico para detectar duplicidade


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

    def add(self, categoria, status, mensagem, peso=0):
        self.checagens.append(Checagem(categoria, status, mensagem, peso if status in ("alerta", "falha") else 0))


def _norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().upper()
    for t in (" LTDA", " EIRELI", " S/A", " S.A.", " SA", " ME", " EPP", " MEI", "-", ".", ","):
        s = s.replace(t, " ")
    return " ".join(s.split())


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
    ph = fx.phash(pagina)
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
        except Exception as e:  # noqa: BLE001
            r.erro_llm = f"{type(e).__name__}: {e}"
    else:
        r.erro_llm = "Servidor do modelo não está rodando (./start_llm.sh)."
    if r.erro_llm:
        r.add("Extração", "alerta", f"Extração por IA indisponível: {r.erro_llm}", 0)
    r.dados["extraido"] = ext

    emit = ext.get("emitente") or {}
    cnpj = limpa(emit.get("cnpj")) or None
    cpf = limpa(emit.get("cpf")) or None
    total = para_float(ext.get("total"))
    data_em = para_data(ext.get("data_emissao"))
    numero = (ext.get("numero_documento") or "").lstrip("0") or None
    tipo = ext.get("tipo_documento")

    # chave: QR > texto do PDF > leitura do modelo
    chave_pdf = acha_chave(doc.texto_pdf)
    chave_llm = limpa(ext.get("chave_acesso")) or None
    chave = chave_qr or chave_pdf or chave_llm

    if ext.get("legibilidade") == "ruim":
        r.add("Extração", "alerta", "Documento pouco legível: peça reenvio de uma imagem melhor.", 10)

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
        if not pc:
            r.add("Chave de acesso", "falha", f"Chave de acesso com tamanho inválido ({len(chave)} posições).", 30)
        else:
            r.dados["chave"] = pc
            fonte = "QR Code" if chave == chave_qr else ("texto do PDF" if chave == chave_pdf else "leitura da IA")
            if pc["dv_ok"]:
                r.add("Chave de acesso", "ok", f"Chave válida ({pc['modelo']}, {pc['uf']}, {pc['ano_mes']}), lida via {fonte}.")
            else:
                peso = 40 if fonte != "leitura da IA" else 15  # IA pode errar um dígito
                r.add("Chave de acesso", "falha" if peso == 40 else "alerta",
                      f"Dígito verificador da chave NÃO confere (fonte: {fonte}).", peso)
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
        if pc and pc["modelo"] == "NF-e":
            r.links_consulta.append("https://www.nfe.fazenda.gov.br/portal/consultaRecaptcha.aspx?tipoConsulta=resumo")
    elif tipo in ("nfce", "nfe_danfe", "cupom_sat"):
        r.add("Chave de acesso", "alerta", "Documento fiscal eletrônico sem chave de acesso legível.", 15)
    elif tipo == "recibo_simples":
        r.add("Chave de acesso", "info", "Recibo simples não tem chave fiscal: validação depende de outras evidências.")

    # Valores
    itens = ext.get("itens") or []
    soma = sum(v for v in (para_float(i.get("valor_total")) for i in itens) if v is not None)
    desc = para_float(ext.get("desconto")) or 0.0
    acr = para_float(ext.get("acrescimo")) or 0.0
    if total is None and ext:
        r.add("Valores", "alerta", "Valor total não identificado.", 15)
    if total is not None and itens and soma > 0:
        esperado = round(soma - desc + acr, 2)
        if abs(esperado - total) <= max(0.05, total * 0.005):
            r.add("Valores", "ok", f"Soma dos itens confere com o total ({brl(total)}).")
        else:
            r.add("Valores", "alerta", f"Soma dos itens ({brl(esperado)}) ≠ total ({brl(total)}).", 25)
    if total is not None:
        if total > pol.valor_maximo:
            r.add("Política", "alerta", f"Valor {brl(total)} acima do limite da política ({brl(pol.valor_maximo)}).", 10)
        if tipo == "recibo_simples" and total >= 50 and total == int(total) and int(total) % 10 == 0:
            r.add("Valores", "alerta", f"Recibo manual com valor redondo ({brl(total)}).", 5)
        if valor_solicitado is not None and valor_solicitado - total > 0.01:
            r.add("Política", "falha", f"Valor solicitado ({brl(valor_solicitado)}) é MAIOR que o do documento ({brl(total)}).", 35)
    if doc.texto_pdf.strip() and total is not None:
        alvo = brl(total).replace("R$ ", "")
        if alvo not in doc.texto_pdf and f"{total:.2f}" not in doc.texto_pdf:
            r.add("Valores", "alerta", f"Total lido ({alvo}) não aparece no texto do PDF: imagem e texto divergem.", 15)

    # Datas
    if data_em:
        if data_em > hoje:
            r.add("Datas", "falha", f"Data de emissão no futuro ({data_em:%d/%m/%Y}).", 30)
        elif (hoje - data_em).days > pol.prazo_dias:
            r.add("Política", "alerta", f"Documento com {(hoje - data_em).days} dias (prazo: {pol.prazo_dias}).", 15)
        else:
            r.add("Datas", "ok", f"Emitido em {data_em:%d/%m/%Y}, dentro do prazo.")
        if data_em.weekday() >= 5:
            r.add("Datas", "info", f"Emitido em fim de semana ({['sáb', 'dom'][data_em.weekday() - 5]}).")
    elif ext:
        r.add("Datas", "alerta", "Data de emissão não identificada.", 15)

    # Sinais visuais apontados pelo modelo
    pesos_g = {"baixa": 5, "media": 10, "alta": 20}
    acumulado = 0
    for s in ext.get("sinais_suspeitos") or []:
        p = min(pesos_g.get(s.get("gravidade"), 5), 35 - acumulado)
        acumulado += p
        r.add("Análise visual (IA)", "alerta", f"[{s.get('gravidade')}] {s.get('descricao')}", max(p, 0))

    # ---------- 4. Duplicidade ----------
    dups = historico.buscar_duplicados(doc.sha256, ph, chave, cnpj, numero,
                                       f"{data_em:%Y-%m-%d}" if data_em else None, total)
    for d in dups:
        quem = f" por '{d['colaborador']}'" if d["colaborador"] else ""
        r.add("Duplicidade", "falha", f"Possível duplicata do reembolso #{d['id']} ({d['motivo']}), "
              f"enviado em {d['em']}{quem}.", 60)
    if not dups:
        r.add("Duplicidade", "ok", "Nenhum reembolso semelhante no histórico.")

    # ---------- 5. Score ----------
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

    if pol.registrar:
        r.id_historico = historico.registrar(colaborador, doc.caminho.name, doc.sha256, ph, chave, cnpj,
                                             numero, f"{data_em:%Y-%m-%d}" if data_em else None, total, r.score)
    r.dados["resumo"] = {
        "tipo": tipo, "emitente": emit.get("razao_social"), "cnpj": cnpj or cpf,
        "data_emissao": f"{data_em:%d/%m/%Y}" if data_em else None, "numero": numero,
        "chave_acesso": chave, "total": total, "forma_pagamento": ext.get("forma_pagamento"),
        "analisado_em": datetime.now().isoformat(timespec="seconds"),
    }
    return r
