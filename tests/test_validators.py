"""Testes das validações fiscais. Rodar todos: python -m unittest"""
import unittest

from reembolso.forensics import chave_do_qr, eh_url_consulta
from reembolso.validators import (acha_chave, brl, dv_chave, para_data, para_float, parse_chave,
                                  valida_cnpj, valida_cpf)


def chave_valida() -> str:
    base = "35" + "2611" + "11222333000181" + "65" + "001" + "000012345" + "1" + "12345678"
    return base + str(dv_chave(base))


class TestValidators(unittest.TestCase):
    def test_cnpj(self):
        self.assertTrue(valida_cnpj("11.222.333/0001-81"))
        self.assertFalse(valida_cnpj("11.222.333/0001-82"))
        self.assertFalse(valida_cnpj("00.000.000/0000-00"))
        self.assertTrue(valida_cnpj("12.ABC.345/01DE-35"))  # exemplo oficial da RFB (alfanumérico)
        self.assertFalse(valida_cnpj("12.ABC.345/01DE-36"))
        self.assertFalse(valida_cnpj(None))

    def test_cpf(self):
        self.assertTrue(valida_cpf("529.982.247-25"))
        self.assertFalse(valida_cpf("529.982.247-24"))
        self.assertFalse(valida_cpf("111.111.111-11"))

    def test_chave(self):
        chave = chave_valida()
        p = parse_chave(chave)
        self.assertTrue(p["dv_ok"])
        self.assertEqual((p["uf"], p["modelo"], p["ano_mes"]), ("SP", "NFC-e", "2026-11"))
        self.assertEqual((p["cnpj_emitente"], p["numero"]), ("11222333000181", "12345"))
        errada = chave[:-1] + str((int(chave[-1]) + 1) % 10)
        self.assertFalse(parse_chave(errada)["dv_ok"])
        self.assertIsNone(parse_chave(chave[:-1]))
        self.assertEqual(acha_chave("Chave: " + " ".join(chave[i:i + 4] for i in range(0, 44, 4))), chave)

    def test_qr(self):
        chave = chave_valida()
        url = f"https://www.nfce.fazenda.sp.gov.br/qrcode?p={chave}|2|1|1|ABCDEF"
        self.assertEqual(chave_do_qr(url), chave)
        self.assertTrue(eh_url_consulta(url))
        self.assertFalse(eh_url_consulta("https://golpe.com/?sefaz.gov.br"))
        self.assertFalse(eh_url_consulta("https://sefaz.gov.br.golpe.com/"))

    def test_para_float(self):
        self.assertEqual(para_float("R$ 1.234,56"), 1234.56)
        self.assertEqual(para_float("1,234.56"), 1234.56)
        self.assertEqual(para_float("12,50"), 12.5)
        self.assertEqual(para_float(12.5), 12.5)
        self.assertEqual(para_float("1234.56"), 1234.56)
        self.assertIsNone(para_float("abc"))
        self.assertIsNone(para_float(None))

    def test_para_data(self):
        self.assertEqual(str(para_data("05/11/2026")), "2026-11-05")
        self.assertEqual(str(para_data("05/11/26")), "2026-11-05")
        self.assertEqual(str(para_data("05/11/26 14:30")), "2026-11-05")
        self.assertEqual(str(para_data("2026-11-05")), "2026-11-05")
        self.assertIsNone(para_data("31/02/2026"))
        self.assertIsNone(para_data(None))

    def test_brl(self):
        self.assertEqual(brl(1234.5), "R$ 1.234,50")
        self.assertEqual(brl(None), "-")


if __name__ == "__main__":
    unittest.main()
