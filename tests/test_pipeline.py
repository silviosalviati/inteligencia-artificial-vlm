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
        "itens": [{"descricao": "Café", "valor_total": 10.0}, {"descricao": "Pão", "valor_total": 20.5}],
        "total": 30.5,
        "legibilidade": "boa",
        "sinais_suspeitos": [],
    }
    dados.update(extra)
    return dados


class TestPipeline(unittest.TestCase):
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

    def test_arquivo_corrompido_gera_erro_legivel(self):
        ruim = self.dir / "ruim.jpg"
        ruim.write_bytes(b"isto nao e uma imagem")
        with self.assertRaisesRegex(ValueError, "corrompida"):
            analisar(str(ruim), Politica(online=False, registrar=False))


if __name__ == "__main__":
    unittest.main()
