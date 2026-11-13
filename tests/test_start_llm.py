"""Testes do inicializador, sem baixar modelos ou executar o llama-server."""

import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import start_llm


class TestStartLlm(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "bin" / "llama-b12292").mkdir(parents=True)
        (self.root / "models").mkdir()
        for nome in ("Qwen3VL-4B-Instruct-Q4_K_M.gguf", "mmproj-Qwen3VL-4B-Instruct-Q8_0.gguf"):
            (self.root / "models" / nome).touch()
        self.root_patch = patch.object(start_llm, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def test_windows_usa_executavel_e_gpu(self):
        servidor = self.root / "bin" / "llama-b12292" / "llama-server.exe"
        servidor.touch()
        with patch.object(start_llm.sys, "platform", "win32"), \
                patch.object(start_llm.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(start_llm.iniciar(), 0)
        comando = run.call_args.args[0]
        self.assertEqual(comando[0], str(servidor))
        self.assertEqual(comando[-2:], ["-ngl", "99"])
        self.assertIn("--mmproj", comando)
        self.assertEqual(comando[comando.index("--port") + 1], "8080")

    def test_linux_preserva_parametros_sem_forcar_gpu(self):
        servidor = self.root / "bin" / "llama-b12292" / "llama-server"
        servidor.touch()
        with patch.object(start_llm.sys, "platform", "linux"), \
                patch.object(start_llm.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(start_llm.iniciar(), 0)
        comando = run.call_args.args[0]
        self.assertEqual(comando[0], str(servidor))
        self.assertNotIn("-ngl", comando)
        self.assertEqual(comando[comando.index("-c") + 1], "4096")
        self.assertEqual(comando[comando.index("--image-max-tokens") + 1], "768")

    def test_permite_selecionar_cpu(self):
        (self.root / "bin" / "llama-b12292" / "llama-server.exe").touch()
        with patch.object(start_llm.sys, "platform", "win32"), \
                patch.object(start_llm.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(start_llm.iniciar(0), 0)
        self.assertEqual(run.call_args.args[0][-2:], ["-ngl", "0"])

    def test_arquivos_ausentes_sao_reportados(self):
        erro = io.StringIO()
        with patch.object(start_llm.sys, "platform", "win32"), \
                patch.object(start_llm.subprocess, "run") as run, \
                contextlib.redirect_stderr(erro):
            self.assertEqual(start_llm.iniciar(), 1)
        run.assert_not_called()
        self.assertIn("llama-server.exe", erro.getvalue())

    def test_sistema_nao_suportado(self):
        with patch.object(start_llm.sys, "platform", "darwin"), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(start_llm.iniciar(), 1)

    def test_erro_ao_executar_e_reportado(self):
        (self.root / "bin" / "llama-b12292" / "llama-server.exe").touch()
        erro = io.StringIO()
        with patch.object(start_llm.sys, "platform", "win32"), \
                patch.object(start_llm.subprocess, "run", side_effect=OSError("falha")), \
                contextlib.redirect_stderr(erro):
            self.assertEqual(start_llm.iniciar(), 1)
        self.assertIn("falha", erro.getvalue())

    def test_propagacao_do_codigo_de_saida(self):
        (self.root / "bin" / "llama-b12292" / "llama-server").touch()
        with patch.object(start_llm.sys, "platform", "linux"), \
                patch.object(start_llm.subprocess, "run") as run:
            run.return_value.returncode = 42
            self.assertEqual(start_llm.iniciar(), 42)


if __name__ == "__main__":
    unittest.main()
