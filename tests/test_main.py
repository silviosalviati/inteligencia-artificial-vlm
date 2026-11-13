"""Testes do inicializador, sem baixar modelos nem executar processos de verdade."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import main


class TestComandoModelo(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "bin" / main.VERSAO_LLAMA).mkdir(parents=True)
        (self.root / "models").mkdir()
        for nome in (main.MODELO, main.MMPROJ):
            (self.root / "models" / nome).touch()
        p = patch.object(main, "ROOT", self.root)
        p.start()
        self.addCleanup(p.stop)

    def servidor(self, nome):
        caminho = self.root / "bin" / main.VERSAO_LLAMA / nome
        caminho.touch()
        return caminho

    def test_windows_usa_executavel_e_gpu(self):
        servidor = self.servidor("llama-server.exe")
        with patch.object(main.sys, "platform", "win32"):
            comando = main.comando_modelo()
        self.assertEqual(comando[0], str(servidor))
        self.assertEqual(comando[-2:], ["-ngl", "99"])
        self.assertIn("--mmproj", comando)
        self.assertEqual(comando[comando.index("--port") + 1], str(main.PORTA_MODELO))
        self.assertEqual(comando[comando.index("--host") + 1], "127.0.0.1")

    def test_linux_nao_forca_gpu(self):
        servidor = self.servidor("llama-server")
        with patch.object(main.sys, "platform", "linux"):
            comando = main.comando_modelo()
        self.assertEqual(comando[0], str(servidor))
        self.assertNotIn("-ngl", comando)
        self.assertEqual(comando[comando.index("-c") + 1], str(main.CONTEXTO_PADRAO))
        self.assertEqual(comando[comando.index("--image-max-tokens") + 1], "768")

    def test_cpu_contexto_e_extras(self):
        self.servidor("llama-server.exe")
        with patch.object(main.sys, "platform", "win32"):
            comando = main.comando_modelo(0, 16384, ["--flash-attn", "on"])
        self.assertEqual(comando[comando.index("-ngl") + 1], "0")
        self.assertEqual(comando[comando.index("-c") + 1], "16384")
        self.assertEqual(comando[-2:], ["--flash-attn", "on"])

    def test_arquivos_ausentes_e_download_incompleto(self):
        (self.root / "models" / main.MODELO).rename(self.root / "models" / (main.MODELO + ".part"))
        erro = io.StringIO()
        with patch.object(main.sys, "platform", "win32"), contextlib.redirect_stderr(erro):
            self.assertIsNone(main.comando_modelo())
        self.assertIn("llama-server.exe", erro.getvalue())
        self.assertIn("download incompleto", erro.getvalue())

    def test_sistema_nao_suportado(self):
        with patch.object(main.sys, "platform", "darwin"), contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(main.comando_modelo())

    def test_python_do_projeto_prefere_venv(self):
        self.assertEqual(main.python_do_projeto(), main.sys.executable)
        venv = self.root / ".venv" / "Scripts" / "python.exe"
        venv.parent.mkdir(parents=True)
        venv.touch()
        self.assertEqual(main.python_do_projeto(), str(venv))


class TestMain(unittest.TestCase):
    def test_sem_arquivos_nao_inicia_nada(self):
        with patch.object(main, "responde", return_value=False), \
                patch.object(main, "comando_modelo", return_value=None), \
                patch.object(main.subprocess, "Popen") as popen, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main.main([]), 1)
        popen.assert_not_called()

    def test_reaproveita_o_que_ja_esta_rodando(self):
        with patch.object(main, "responde", return_value=True), \
                patch.object(main.subprocess, "Popen") as popen, \
                patch.object(main.webbrowser, "open") as abrir, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main.main([]), 0)
        popen.assert_not_called()
        abrir.assert_called_once_with(f"http://127.0.0.1:{main.PORTA_WEB}/")

    def test_sobe_modelo_e_web_e_encerra_juntos(self):
        modelo, web = MagicMock(), MagicMock()
        modelo.poll.return_value = None          # modelo segue rodando
        web.poll.side_effect = [None, 0, 0, 0]   # interface encerra depois de pronta
        web.returncode = 0
        estados = iter([False, False] + [True] * 10)  # nada rodando no início; depois tudo responde
        with tempfile.TemporaryDirectory() as d, \
                patch.object(main, "ROOT", Path(d)), \
                patch.object(main, "responde", side_effect=lambda *_a, **_k: next(estados)), \
                patch.object(main, "comando_modelo", return_value=["llama-server"]), \
                patch.object(main.subprocess, "Popen", side_effect=[modelo, web]) as popen, \
                patch.object(main.webbrowser, "open") as abrir, \
                patch.object(main.time, "sleep"), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main.main(["--sem-navegador"]), 1)
        self.assertEqual(popen.call_count, 2)
        self.assertEqual(popen.call_args_list[1].args[0][1:], ["-m", "reembolso.app"])
        abrir.assert_not_called()
        modelo.terminate.assert_called_once()   # web caiu → modelo é encerrado junto

    def test_rejeita_gpu_layers_negativo(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main.main(["--gpu-layers", "-1"])


if __name__ == "__main__":
    unittest.main()
