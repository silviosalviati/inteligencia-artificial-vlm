"""Testes do pipeline com o modelo de visão simulado (não precisa do llama-server nem de internet)."""
from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from reembolso import historico
from reembolso.pipeline import Politica, analisar
from tests.test_validators import chave_valida

HOJE = date(2026, 11, 20)


def extracao(**extra):
    dados = {
        "tipo_documento": "nfce",
        "emitente": {"razao_social": "Padaria Teste", "cnpj": "11.222.333/0001-81", "cpf": None,
                     "cidade": "São Paulo", "uf": "SP"},
        "destinatario": {"nome": None, "cpf_cnpj": None},
        "numero_documento": "000.012.345",
        "data_emissao": "05/11/2026",
        "chave_acesso": chave_valida(),
        "itens": [{"descricao": "Café", "valor_total": 10.0, "categoria": "alimentacao"},
                  {"descricao": "Pão", "valor_total": 20.5, "categoria": "alimentacao"}],
        "categoria_despesa": "alimentacao", "rasura": False, "assinatura": False, "carimbo_cnpj": False,
        "total": 30.5,
        "legibilidade": "boa",
        "sinais_suspeitos": [],
    }
    dados.update(extra)
    return dados


class BasePipeline(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        p = patch.object(historico, "DB", self.dir / "historico.sqlite")
        p.start()
        self.addCleanup(p.stop)
        self.img = self.dir / "cupom.png"
        im = Image.new("RGB", (400, 600), "white")
        ImageDraw.Draw(im).text((20, 20), "PADARIA TESTE  TOTAL 30,50", fill="black")
        im.save(self.img)

    def rodar(self, ext=None, disponivel=True, **pol):
        politica = Politica(online=False, **pol)
        with patch("reembolso.llm.disponivel", return_value=disponivel), \
                patch("reembolso.llm.analisar_imagem", return_value=ext or extracao()):
            return analisar(str(self.img), politica, colaborador="ana", hoje=HOJE)

    def mensagens(self, r, status):
        return [c.mensagem for c in r.checagens if c.status == status]



class TestPipeline(BasePipeline):
    def test_documento_correto_tem_risco_baixo(self):
        r = self.rodar()
        self.assertEqual(r.nivel, "baixo", r.checagens)
        # número "000.012.345" impresso deve bater com o número da chave (12345)
        self.assertFalse(any("Número na chave" in m for m in self.mensagens(r, "alerta")))
        self.assertIsNotNone(r.id_historico)

    def test_duplicata_detectada_na_segunda_analise(self):
        self.rodar()
        r = self.rodar()
        self.assertEqual(r.nivel, "alto")
        self.assertTrue(any("duplicata" in m for m in self.mensagens(r, "falha")))

    def test_varias_duplicatas_viram_uma_checagem(self):
        for _ in range(3):
            self.rodar()
        r = self.rodar()
        dup = [c for c in r.checagens if c.categoria == "Duplicidade"]
        self.assertEqual(len(dup), 1)
        self.assertIn("Também coincide com 2 outro(s): #2, #3", dup[0].mensagem)
        self.assertEqual(len(r.dados["duplicatas"]), 3)

    def test_ia_indisponivel_nao_registra_no_historico(self):
        r = self.rodar(disponivel=False)
        self.assertEqual(r.nivel, "indeterminado")
        self.assertIsNone(r.id_historico)
        # com a IA de volta, o mesmo arquivo não pode virar "duplicata de si mesmo"
        r = self.rodar()
        self.assertFalse(any("duplicata" in m for m in self.mensagens(r, "falha")))

    def test_soma_divergente_e_cnpj_invalido(self):
        ext = extracao(total=99.0, emitente={"razao_social": "X", "cnpj": "11.222.333/0001-82"})
        r = self.rodar(ext, registrar=False)
        self.assertTrue(any("Soma dos itens" in m for m in self.mensagens(r, "alerta")))
        self.assertTrue(any("INVÁLIDO" in m for m in self.mensagens(r, "falha")))
        self.assertEqual(r.nivel, "alto")

    def test_chave_lida_como_link_do_qr(self):
        url = f"https://www.nfce.fazenda.sp.gov.br/qrcode?p={chave_valida()}|2|1|1|ABCDEF"
        r = self.rodar(extracao(chave_acesso=url), registrar=False)
        self.assertEqual(r.dados["resumo"]["chave_acesso"], chave_valida())
        self.assertFalse(any("tamanho inválido" in m for m in self.mensagens(r, "falha")))
        # a URL lida pela IA não vira link (ela erra letras); entra o portal oficial da UF da chave
        self.assertEqual(r.links_consulta, ["https://www.nfce.fazenda.sp.gov.br/qrcode"])

    def test_quantidade_lida_errado_nao_vira_alerta_de_fraude(self):
        # Nota: "SALADA + ATUM 2 UN 93,00 186,00"; a IA leu quantidade 1 e total 93,00.
        itens = [{"descricao": "SALADA + MIGNON", "quantidade": 1, "valor_unitario": 93.0, "valor_total": 93.0,
                  "categoria": "alimentacao"},
                 {"descricao": "SALADA + ATUM", "quantidade": 1, "valor_unitario": 93.0, "valor_total": 93.0,
                  "categoria": "alimentacao"},
                 {"descricao": "Gorjeta Sugerida", "quantidade": 1, "valor_unitario": 40.04, "valor_total": 40.04,
                  "categoria": "outro"}]
        r = self.rodar(extracao(itens=itens, total=319.04), registrar=False)
        self.assertFalse(any("Soma dos itens" in m for m in self.mensagens(r, "alerta")))
        self.assertTrue(any("provável quantidade lida errado" in m for m in self.mensagens(r, "info")))

    def test_quantidade_vezes_unitario_fecha_a_soma(self):
        itens = [{"descricao": "SALADA + ATUM", "quantidade": 2, "valor_unitario": 93.0, "valor_total": 93.0,
                  "categoria": "alimentacao"}]
        r = self.rodar(extracao(itens=itens, total=186.0), registrar=False)
        self.assertTrue(any("quantidade × valor unitário" in m for m in self.mensagens(r, "ok")))

    def test_preenchimento_com_zeros_e_nao_indicios(self):
        ext = extracao(emitente={"razao_social": "X", "cnpj": "00000000000000"},
                       chave_acesso="5725" + "0" * 40,
                       sinais_suspeitos=[{"descricao": "Soma confere — não é indício.", "gravidade": "baixa"},
                                         {"descricao": "Valor com fonte diferente", "gravidade": "alta"}])
        r = self.rodar(ext, registrar=False)
        self.assertFalse(any("INVÁLIDO" in m for m in self.mensagens(r, "falha")))
        self.assertIsNone(r.dados["resumo"]["cnpj"])
        visuais = [c for c in r.checagens if c.categoria == "Análise visual (IA)"]
        self.assertEqual([(c.status, c.peso) for c in visuais], [("alerta", 20)])

    def test_cnpj_do_banco_do_brasil_na_chave_nao_e_descartado(self):
        from reembolso.validators import dv_chave
        base = "53" + "2610" + "00000000000191" + "55" + "001" + "000000123" + "1" + "00000001"
        chave = base + str(dv_chave(base))
        ext = extracao(emitente={"razao_social": "Banco do Brasil", "cnpj": "00.000.000/0001-91"},
                       chave_acesso=chave, numero_documento="123", data_emissao="05/10/2026")
        r = self.rodar(ext, registrar=False)
        self.assertEqual(r.dados["resumo"]["chave_acesso"], chave)
        self.assertEqual(self.mensagens(r, "falha"), [])

    def test_chave_da_ia_com_dv_errado_nao_gera_alertas_cruzados(self):
        errada = chave_valida()[:43] + str((int(chave_valida()[43]) + 1) % 10)
        r = self.rodar(extracao(chave_acesso=errada, data_emissao="05/10/2026"), registrar=False)
        msgs = self.mensagens(r, "alerta")
        self.assertTrue(any("Dígito verificador" in m for m in msgs))
        self.assertFalse(any("Mês da chave" in m for m in msgs))

    def test_documento_de_cabeca_para_baixo_e_endireitado_e_relido(self):
        from PIL import Image
        leituras = [extracao(orientacao="de_cabeca_para_baixo", total=None, itens=[]), extracao(orientacao="normal")]
        politica = Politica(online=False, registrar=False)
        with patch("reembolso.llm.disponivel", return_value=True),                 patch("reembolso.llm.analisar_imagem", side_effect=leituras) as ia:
            r = analisar(str(self.img), politica, hoje=HOJE)
        self.assertEqual(ia.call_count, 2)
        original = Image.open(self.img).convert("RGB")
        self.assertEqual(list(r.pagina_corrigida.getdata())[:50], list(original.rotate(180).getdata())[:50])
        self.assertTrue(any("de cabeça para baixo" in m for m in self.mensagens(r, "info")))
        self.assertEqual(r.decisao, "deferido")

    def test_documento_que_continua_girado_fica_pendente(self):
        girado = extracao(orientacao="topo_a_direita")
        with patch("reembolso.llm.disponivel", return_value=True),                 patch("reembolso.llm.analisar_imagem", side_effect=[girado, girado, girado]) as ia:
            r = analisar(str(self.img), Politica(online=False, registrar=False), hoje=HOJE)
        self.assertEqual(ia.call_count, 3)   # leitura + no máximo 2 correções
        self.assertEqual(r.decisao, "pendente")

    def test_arquivo_corrompido_gera_erro_legivel(self):
        ruim = self.dir / "ruim.jpg"
        ruim.write_bytes(b"isto nao e uma imagem")
        with self.assertRaisesRegex(ValueError, "corrompida"):
            analisar(str(ruim), Politica(online=False, registrar=False))


if __name__ == "__main__":
    unittest.main()
