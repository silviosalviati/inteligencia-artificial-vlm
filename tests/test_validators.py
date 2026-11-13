"""Rodar: .venv/bin/python -m tests.test_validators"""
from reembolso.forensics import chave_do_qr, eh_url_consulta
from reembolso.validators import (acha_chave, dv_chave, para_data, para_float, parse_chave,
                                  valida_cnpj, valida_cpf)


def test_cnpj():
    assert valida_cnpj("11.222.333/0001-81")
    assert not valida_cnpj("11.222.333/0001-82")
    assert not valida_cnpj("00.000.000/0000-00")
    assert valida_cnpj("12.ABC.345/01DE-35")       # exemplo oficial da RFB (alfanumérico)
    assert not valida_cnpj("12.ABC.345/01DE-36")


def test_cpf():
    assert valida_cpf("529.982.247-25")
    assert not valida_cpf("529.982.247-24")
    assert not valida_cpf("111.111.111-11")


def test_chave():
    base = "35" + "2611" + "11222333000181" + "65" + "001" + "000012345" + "1" + "12345678"
    chave = base + str(dv_chave(base))
    p = parse_chave(chave)
    assert p["dv_ok"] and p["uf"] == "SP" and p["modelo"] == "NFC-e"
    assert p["ano_mes"] == "2026-11" and p["cnpj_emitente"] == "11222333000181" and p["numero"] == "12345"
    errada = chave[:-1] + str((int(chave[-1]) + 1) % 10)
    assert not parse_chave(errada)["dv_ok"]
    assert acha_chave("Chave: " + " ".join(chave[i:i + 4] for i in range(0, 44, 4))) == chave
    url = f"https://www.nfce.fazenda.sp.gov.br/qrcode?p={chave}|2|1|1|ABCDEF"
    assert chave_do_qr(url) == chave and eh_url_consulta(url)
    assert not eh_url_consulta("https://golpe.com/?sefaz.gov.br")
    return chave


def test_conversoes():
    assert para_float("R$ 1.234,56") == 1234.56
    assert para_float(12.5) == 12.5
    assert para_float("1234.56") == 1234.56
    assert str(para_data("05/11/2026")) == "2026-11-05"


if __name__ == "__main__":
    for nome, f in list(globals().items()):
        if nome.startswith("test_"):
            f()
            print("ok", nome)
