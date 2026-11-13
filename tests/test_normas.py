"""Decisão deferido/indeferido com base na memória normativa (normas/*.json)."""
from datetime import date
from pathlib import Path
import tempfile
import unittest
import unittest.mock

from reembolso import normas
from tests.test_pipeline import BasePipeline, extracao


def item(descricao, valor, categoria="alimentacao"):
    return {"descricao": descricao, "valor_total": valor, "categoria": categoria}


class TestMemoria(unittest.TestCase):
    def test_memoria_da_in_001_004(self):
        mem = normas.carregar()
        self.assertIn("IN 001-004 v20", mem.base)
        self.assertEqual(mem.prazo_dias, 360)
        self.assertEqual(mem.pendencias, [])   # o PDF em INs/ confere com o sha256 da memória

    def test_in_nova_sem_memoria_gera_aviso(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "IN 999-001 Nova.pdf").write_bytes(b"%PDF-1.7 nova")
            mem = normas.carregar(pasta_ins=Path(d))
        self.assertTrue(any("IN 999-001" in p and "não está na memória" in p for p in mem.pendencias))

    def test_palavra_chave_decide_e_categoria_da_ia_e_ignorada(self):
        mem = normas.carregar()
        args = dict(tipo="nfce", data_em=date(2026, 11, 5), hoje=date(2026, 11, 20), total=30.0,
                    valor_solicitado=None)
        a = normas.avaliar(mem, extracao(itens=[item("CERVEJA HEINEKEN 600ML", 18.9, "bebida_alcoolica")]), **args)
        self.assertTrue(any(x.efeito == "indeferir" and "Bebidas alcoólicas" in x.titulo for x in a))
        a = normas.avaliar(mem, extracao(itens=[item("SUCO DE LARANJA", 9.0, "bebida_alcoolica")]), **args)
        self.assertFalse(any("Bebidas" in x.titulo for x in a))

    def test_agua_gasosa_classificada_como_combustivel_nao_vira_combustivel(self):
        mem = normas.carregar()
        args = dict(tipo="nfce", data_em=date(2026, 11, 5), hoje=date(2026, 11, 20), total=14.0,
                    valor_solicitado=None)
        ext = extracao(itens=[item("AGUA MINERAL GASOSA", 14.0, "combustivel")],
                       emitente={"razao_social": "AUTO POSTO CENTRAL LTDA", "cnpj": "11.222.333/0001-81"})
        self.assertFalse(any("Combustível" in x.titulo for x in normas.avaliar(mem, ext, **args)))
        ext = extracao(itens=[item("GASOLINA COMUM", 250.0, "combustivel")])
        self.assertTrue(any("Combustível" in x.titulo and x.efeito == "condicionar"
                            for x in normas.avaliar(mem, ext, **args)))

    def test_falsos_positivos_conhecidos(self):
        mem = normas.carregar()
        itens = [item("PASTA AO SUGO", 45.0), item("ESTACIONAMENTO DIARIA", 30.0, "pedagio_estacionamento"),
                 item("CAFE EM CAPSULA", 4.0, "bebida_nao_alcoolica"), item("TAXA DE SERVICO 10%", 4.9)]
        a = normas.avaliar(mem, extracao(itens=itens), tipo="nfce", data_em=date(2026, 11, 5),
                           hoje=date(2026, 11, 20), total=83.9, valor_solicitado=None)
        self.assertEqual([x.mensagem for x in a if x.efeito == "indeferir"], [])

    def test_excecao_anbima(self):
        mem = normas.carregar()
        a = normas.avaliar(mem, extracao(itens=[item("INSCRICAO CERTIFICACAO ANBIMA CPA-20", 450.0, "curso_treinamento")]),
                           tipo="recibo_simples", data_em=date(2026, 11, 5), hoje=date(2026, 11, 20),
                           total=450.0, valor_solicitado=None)
        self.assertFalse(any("Cursos" in x.titulo and x.efeito == "indeferir" for x in a))


class TestDecisao(BasePipeline):
    def test_documento_regular_e_deferido_com_condicao_de_refeicao(self):
        r = self.rodar(registrar=False)
        self.assertEqual(r.decisao, "deferido", r.motivos)
        self.assertTrue(any("Refeições" in e for e in r.exigencias))
        self.assertIn("IN 001-004", r.base_normativa)

    def test_duplicado_e_indeferido(self):
        self.rodar()
        r = self.rodar()
        self.assertEqual(r.decisao, "indeferido")
        self.assertTrue(any("duplicidade" in m for m in r.motivos))

    def test_item_sem_cobertura_e_glosado_e_o_resto_deferido(self):
        ext = extracao(itens=[item("Prato executivo", 40.0), item("CERVEJA BRAHMA LATA", 9.0, "bebida_alcoolica")],
                       total=49.0)
        r = self.rodar(ext, registrar=False)
        self.assertEqual(r.decisao, "deferido_parcial")
        self.assertEqual((r.valor_documento, r.valor_glosado, r.valor_reembolsavel), (49.0, 9.0, 40.0))
        self.assertTrue(any("CERVEJA BRAHMA" in g and "§4.2.3" in g for g in r.glosas))

    def test_so_itens_sem_cobertura_e_indeferido(self):
        ext = extracao(itens=[item("CERVEJA BRAHMA LATA", 9.0, "bebida_alcoolica")], total=9.0)
        r = self.rodar(ext, registrar=False)
        self.assertEqual((r.decisao, r.valor_reembolsavel), ("indeferido", 0.0))

    def test_reembolsavel_respeita_o_valor_solicitado(self):
        from reembolso.pipeline import Politica, analisar
        with unittest.mock.patch("reembolso.llm.disponivel", return_value=True),                 unittest.mock.patch("reembolso.llm.analisar_imagem", return_value=extracao()):
            r = analisar(str(self.img), Politica(online=False, registrar=False), valor_solicitado=20.0,
                         hoje=date(2026, 11, 20))
        self.assertEqual((r.decisao, r.valor_reembolsavel), ("deferido", 20.0))

    def test_quantidade_conta_na_glosa(self):
        cerveja = {"descricao": "CERVEJA HEINEKEN", "quantidade": 3, "valor_unitario": 12.0, "valor_total": 12.0,
                   "categoria": "bebida_alcoolica"}
        r = self.rodar(extracao(itens=[item("Prato", 50.0), cerveja], total=86.0), registrar=False)
        self.assertEqual((r.valor_glosado, r.valor_reembolsavel), (36.0, 50.0))

    def test_fora_do_prazo_de_360_dias_e_indeferido(self):
        r = self.rodar(extracao(data_emissao="01/10/2025"), registrar=False)
        self.assertEqual(r.decisao, "indeferido")
        self.assertTrue(any("360 dias" in m and "§4.3" in m for m in r.motivos))

    def test_pedir_mais_que_o_comprovante_e_indeferido(self):
        with unittest.mock.patch("reembolso.llm.disponivel", return_value=True), \
                unittest.mock.patch("reembolso.llm.analisar_imagem", return_value=extracao()):
            from reembolso.pipeline import Politica, analisar
            r = analisar(str(self.img), Politica(online=False, registrar=False), valor_solicitado=99.0,
                         hoje=date(2026, 11, 20))
        self.assertEqual(r.decisao, "indeferido")
        self.assertTrue(any("maior que o do comprovante" in m for m in r.motivos))

    def test_recibo_sem_assinatura_nem_carimbo_e_indeferido(self):
        ext = extracao(tipo_documento="recibo_simples", chave_acesso=None, numero_documento="15")
        r = self.rodar(ext, registrar=False)
        self.assertEqual(r.decisao, "indeferido")
        self.assertTrue(any("assinatura" in m for m in r.motivos))

    def test_comprovante_de_cartao_sozinho_e_indeferido(self):
        r = self.rodar(extracao(tipo_documento="comprovante_cartao", chave_acesso=None, itens=[]), registrar=False)
        self.assertEqual(r.decisao, "indeferido")

    def test_ia_fora_do_ar_fica_pendente(self):
        r = self.rodar(disponivel=False, registrar=False)
        self.assertEqual(r.decisao, "pendente")

    def test_suspeita_de_fraude_indefere(self):
        ext = extracao(total=99.0, emitente={"razao_social": "X", "cnpj": "11.222.333/0001-82"})
        r = self.rodar(ext, registrar=False)
        self.assertEqual(r.decisao, "indeferido")
        self.assertTrue(any("Suspeita de fraude" in m for m in r.motivos))


if __name__ == "__main__":
    unittest.main()
