"""Configuração central: caminhos, modelos e nome da aplicação."""

import os
from pathlib import Path

from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent.parent
load_dotenv(RAIZ / ".env")

# Dados iniciais do condomínio (somente leitura, nunca alterados).
DIR_DADOS_INICIAIS = RAIZ / "dados"

# Estado vivo: banco do condomínio e banco das sessões do ADK (fora do Git).
DIR_ESTADO = Path(os.getenv("AURORA_DIR_ESTADO", RAIZ / "estado"))
DB_CONDOMINIO = DIR_ESTADO / "condominio.db"
DB_SESSOES = DIR_ESTADO / "sessoes.db"

APP_NAME = "residencial_aurora"

# `or` em vez do default do getenv: no .env copiado do exemplo a variável
# existe, mas vazia.
MODELO_PRINCIPAL = os.getenv("MODELO_PRINCIPAL") or "gemini-3.6-flash"
MODELO_ESPECIALISTAS = os.getenv("MODELO_ESPECIALISTAS") or MODELO_PRINCIPAL
