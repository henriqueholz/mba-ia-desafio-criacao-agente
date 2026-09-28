"""Comandos do projeto: ``uv run api`` e ``uv run restaurar``."""

import argparse

import uvicorn

from . import config
from . import db


def api() -> None:
    uvicorn.run("aurora.api:api", host="0.0.0.0", port=8000)


def restaurar() -> None:
    parser = argparse.ArgumentParser(
        description="Volta reservas e visitantes ao estado de dados/*.json."
    )
    parser.add_argument(
        "--apagar-sessoes",
        action="store_true",
        help="também apaga todas as conversas gravadas",
    )
    args = parser.parse_args()
    db.restaurar()
    print("Reservas e visitantes restaurados a partir de dados/.")
    if args.apagar_sessoes:
        with db.conexao() as con:
            con.execute("DELETE FROM sessoes")
        for sufixo in ("", "-wal", "-shm"):
            config.DB_SESSOES.with_name(config.DB_SESSOES.name + sufixo).unlink(
                missing_ok=True
            )
        print("Sessões apagadas.")
