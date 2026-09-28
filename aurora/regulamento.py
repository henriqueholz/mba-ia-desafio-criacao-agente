"""Consulta ao regulamento interno por capítulo (Garantia 4).

O regulamento nunca é carregado inteiro em instrução nem em evento. O
especialista de regulamento recebe só o índice de capítulos (título de cada
um) e pede um capítulo por vez; a tool devolve apenas os artigos daquele
capítulo, filtrados pelos termos da dúvida quando possível.
"""

import re
import unicodedata
from dataclasses import dataclass
from functools import cache

from . import config

_ROMANOS = {
    "I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7,
    "VIII": 8, "IX": 9, "X": 10, "XI": 11, "XII": 12, "XIII": 13, "XIV": 14,
    "XV": 15, "XVI": 16, "XVII": 17, "XVIII": 18, "XIX": 19, "XX": 20,
}


@dataclass(frozen=True)
class Capitulo:
    numero: int
    romano: str
    titulo: str
    artigos: tuple[str, ...]


def _sem_acento(texto: str) -> str:
    return (
        unicodedata.normalize("NFKD", texto)
        .encode("ascii", "ignore")
        .decode()
        .lower()
    )


@cache
def capitulos() -> tuple[Capitulo, ...]:
    texto = (config.DIR_DADOS_INICIAIS / "regulamento.md").read_text("utf-8")
    blocos = re.split(r"^## ", texto, flags=re.MULTILINE)[1:]
    resultado = []
    for bloco in blocos:
        cabecalho, _, corpo = bloco.partition("\n")
        m = re.match(r"Capítulo ([IVXL]+):\s*(.+)", cabecalho.strip())
        if not m:
            continue
        romano, titulo = m.group(1), m.group(2).strip()
        # Cada artigo começa em "**Art." e leva junto seus incisos e parágrafos.
        artigos = [
            a.strip()
            for a in re.split(r"(?=^\*\*Art\.)", corpo, flags=re.MULTILINE)
            if a.strip()
        ]
        resultado.append(
            Capitulo(_ROMANOS[romano], romano, titulo, tuple(artigos))
        )
    return tuple(resultado)


def indice() -> str:
    """Só os títulos dos capítulos, usado na instrução do especialista."""
    return "\n".join(
        f"- Capítulo {c.numero} ({c.romano}): {c.titulo}" for c in capitulos()
    )


def _achar_capitulo(capitulo: str) -> Capitulo | None:
    alvo = str(capitulo).strip().upper().removeprefix("CAPÍTULO").strip()
    for c in capitulos():
        if alvo in (str(c.numero), c.romano):
            return c
    return None


def consultar(capitulo: str, termos: str = "") -> dict:
    """Artigos de UM capítulo; se houver termos, só os artigos que os citam."""
    c = _achar_capitulo(capitulo)
    if c is None:
        return {
            "erro": f"Capítulo {capitulo!r} não existe.",
            "capitulos_validos": [c.numero for c in capitulos()],
        }
    palavras = [
        p for p in re.findall(r"\w+", _sem_acento(termos)) if len(p) >= 4
    ]
    artigos = list(c.artigos)
    if palavras:
        filtrados = [
            a
            for a in artigos
            if any(p[:6] in _sem_acento(a) for p in palavras)
        ]
        artigos = filtrados or artigos
    return {
        "capitulo": f"Capítulo {c.romano}: {c.titulo}",
        "artigos": artigos,
    }
