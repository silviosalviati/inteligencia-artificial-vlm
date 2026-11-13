"""Carregamento do documento e sinais forenses (metadados, ELA, QR, hashes)."""
from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cv2
import imagehash
import numpy as np
import pymupdf
from PIL import ExifTags, Image, ImageChops, ImageOps

# Softwares que indicam edição/criação manual do arquivo.
EDITORES = [
    "photoshop", "gimp", "illustrator", "inkscape", "canva", "picsart", "pixlr",
    "affinity", "corel", "paint.net", "snapseed", "lightroom", "photopea", "fotor",
    "ilovepdf", "smallpdf", "sejda", "pdfescape", "pdf-xchange", "foxit phantom",
    "pdffiller", "dochub", "pdfelement", "nitro", "libreoffice", "microsoft word",
    "word", "excel", "google docs", "chatgpt", "dall-e", "midjourney", "stable diffusion",
]

FORMATOS_IMAGEM = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic"}


@dataclass
class Documento:
    caminho: Path
    sha256: str
    tipo: str  # "pdf" | "imagem"
    paginas: list[Image.Image]
    metadados: dict = field(default_factory=dict)
    texto_pdf: str = ""
    pdf_versoes: int = 1


def carregar(caminho: str | Path, max_paginas: int = 2) -> Documento:
    caminho = Path(caminho)
    dados = caminho.read_bytes()
    sha = hashlib.sha256(dados).hexdigest()
    ext = caminho.suffix.lower()

    if ext == ".pdf" or dados[:5] == b"%PDF-":
        doc = pymupdf.open(stream=dados, filetype="pdf")
        paginas, texto = [], []
        for i, pg in enumerate(doc):
            texto.append(pg.get_text())
            if i < max_paginas:
                pix = pg.get_pixmap(dpi=200)
                paginas.append(Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB"))
        meta = {k: v for k, v in (doc.metadata or {}).items() if v}
        versoes = getattr(doc, "version_count", 1) or 1
        return Documento(caminho, sha, "pdf", paginas, meta, "\n".join(texto), versoes)

    img = Image.open(io.BytesIO(dados))
    meta = {}
    try:
        exif = img.getexif()
        for k, v in exif.items():
            nome = ExifTags.TAGS.get(k, str(k))
            if nome in {"Software", "DateTime", "Make", "Model", "Artist", "ProcessingSoftware"}:
                meta[nome] = str(v)
    except Exception:
        pass
    meta["formato"] = img.format or ext.lstrip(".")
    img = ImageOps.exif_transpose(img).convert("RGB")
    return Documento(caminho, sha, "imagem", [img], meta)


def editor_detectado(metadados: dict) -> str | None:
    campos = " | ".join(
        str(metadados.get(k, ""))
        for k in ("producer", "creator", "Software", "ProcessingSoftware", "Artist")
    ).lower()
    for ed in EDITORES:
        if re.search(rf"\b{re.escape(ed)}\b", campos):
            return ed
    return None


def phash(img: Image.Image) -> str:
    return str(imagehash.phash(img, hash_size=16))


def distancia_phash(a: str, b: str) -> int:
    return imagehash.hex_to_hash(a) - imagehash.hex_to_hash(b)


def ela(img: Image.Image, qualidade: int = 90) -> tuple[Image.Image, float]:
    """Error Level Analysis. Retorna (mapa visual, escore de heterogeneidade).

    O escore mede o quanto alguns blocos "brilham" bem mais que a mediana:
    regiões coladas/editadas tendem a recomprimir de forma diferente.
    É um sinal FRACO: fotos de celular e prints geram falsos positivos.
    """
    base = img.convert("RGB")
    if max(base.size) > 1600:
        base.thumbnail((1600, 1600))
    buf = io.BytesIO()
    base.save(buf, "JPEG", quality=qualidade)
    recomp = Image.open(buf)
    diff = np.asarray(ImageChops.difference(base, recomp), dtype=np.float32).max(axis=2)

    h, w = diff.shape
    bs = 32
    blocos = [
        diff[y:y + bs, x:x + bs].mean()
        for y in range(0, h - bs + 1, bs)
        for x in range(0, w - bs + 1, bs)
    ]
    blocos = np.array(blocos) if blocos else np.array([0.0])
    mediana = float(np.median(blocos)) + 0.5
    p99 = float(np.percentile(blocos, 99))
    escore = p99 / mediana

    escala = 255.0 / max(diff.max(), 1.0)
    mapa = Image.fromarray(np.clip(diff * escala, 0, 255).astype(np.uint8)).convert("L")
    mapa = ImageOps.colorize(mapa, black="#000000", white="#ffeb3b", mid="#d32f2f")
    return mapa, round(escore, 2)


def ler_qrcodes(img: Image.Image) -> list[str]:
    """Tenta vários detectores/escalas: fotos de cupom costumam ter QR pequeno e inclinado."""
    cinza = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2GRAY)
    detectores = [cv2.QRCodeDetector()]
    if hasattr(cv2, "QRCodeDetectorAruco"):
        detectores.append(cv2.QRCodeDetectorAruco())
    achados: list[str] = []
    for escala in (1.0, 1.5, 2.0, 0.5):
        a = cinza if escala == 1.0 else cv2.resize(cinza, None, fx=escala, fy=escala,
                                                    interpolation=cv2.INTER_CUBIC)
        nitida = cv2.addWeighted(a, 1.8, cv2.GaussianBlur(a, (0, 0), 3), -0.8, 0)
        for det in detectores:
            for b in (a, nitida):
                try:
                    ok, textos, _, _ = det.detectAndDecodeMulti(b)
                except cv2.error:
                    ok, textos = False, []
                if ok:
                    achados += [t for t in textos if t and t not in achados]
                if achados:
                    return achados
    return achados


def chave_do_qr(texto: str) -> str | None:
    """NFC-e: URL com p=CHAVE|... ou chNFe=CHAVE. CF-e SAT: CHAVE|data|valor|..."""
    try:
        q = parse_qs(urlparse(texto).query)
        for k in ("p", "chNFe", "chave"):
            if k in q:
                cand = re.sub(r"\D", "", q[k][0].split("|")[0])
                if len(cand) == 44:
                    return cand
    except ValueError:
        pass
    m = re.search(r"(?<!\d)\d{44}(?!\d)", texto)
    return m.group(0) if m else None


# Provedores privados que hospedam NFS-e para prefeituras (não usam .gov.br).
PROVEDORES_NFSE = ("issnetonline.com.br", "ginfes.com.br", "betha.cloud", "betha.com.br",
                   "nfse.com.br", "webiss.com.br", "e-governeapps2.com.br", "govbr.cloud",
                   "ipm.com.br", "elotech.com.br", "fiorilli.com.br", "tinus.com.br")


def eh_url(texto: str) -> bool:
    return texto.strip().lower().startswith(("http://", "https://"))


def eh_url_consulta(texto: str) -> bool:
    """URL de consulta oficial: órgão público (.gov.br) ou provedor municipal de NFS-e conhecido."""
    if not eh_url(texto):
        return False
    try:
        host = urlparse(texto.strip()).hostname or ""
    except ValueError:
        return False
    return host.endswith(".gov.br") or any(host == d or host.endswith("." + d) for d in PROVEDORES_NFSE)
