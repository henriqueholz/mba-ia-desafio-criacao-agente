"""Execução do assistente: sessões, mensagens e confirmações.

Aqui fica a ponte entre a API e o Runner do ADK:

- ``criar_sessao`` grava o apartamento no state da sessão uma única vez
  (Garantia 2). Nenhuma rota permite alterá-lo depois.
- ``confirmacoes_pendentes`` lê a sessão persistida: um pedido
  ``adk_request_confirmation`` sem resposta é uma pendência (Garantia 1).
- ``responder_confirmacao`` só aceita ids pendentes; qualquer outro id
  levanta ``ConfirmacaoInexistente`` (409) sem tocar no Runner.
- As sessões ficam em SQLite, então sobrevivem ao reinício (Garantia 3).
"""

import asyncio
import logging
from collections import defaultdict
from typing import Any

from google.adk.agents.run_config import RunConfig
from google.adk.runners import Runner
from google.adk.sessions import Session
from google.adk.sessions.sqlite_session_service import SqliteSessionService
from google.genai import errors as genai_errors
from google.genai import types

from . import config
from . import db
from .agents import app
from .tools import CHAVE_APARTAMENTO

FC_CONFIRMACAO = "adk_request_confirmation"

logger = logging.getLogger(__name__)


class SessaoInexistente(Exception):
    pass


class ConfirmacaoInexistente(Exception):
    pass


config.DIR_ESTADO.mkdir(parents=True, exist_ok=True)
session_service = SqliteSessionService(str(config.DB_SESSOES))
runner = Runner(app=app, session_service=session_service)

# Uma execução por vez em cada sessão. Sessões diferentes rodam em paralelo.
_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# Teto de chamadas ao modelo por mensagem, para um laço de transferências não
# consumir a cota do projeto.
_RUN_CONFIG = RunConfig(max_llm_calls=25)


async def criar_sessao(apartamento: str) -> str:
    sessao = await session_service.create_session(
        app_name=config.APP_NAME,
        user_id=apartamento,
        state={CHAVE_APARTAMENTO: apartamento},
    )
    await asyncio.to_thread(db.registrar_sessao, sessao.id, apartamento)
    return sessao.id


async def obter_sessao(session_id: str) -> Session:
    apartamento = await asyncio.to_thread(db.apartamento_da_sessao, session_id)
    sessao = None
    if apartamento is not None:
        sessao = await session_service.get_session(
            app_name=config.APP_NAME, user_id=apartamento, session_id=session_id
        )
    if sessao is None:
        raise SessaoInexistente(session_id)
    return sessao


def confirmacoes_pendentes(sessao: Session) -> list[dict[str, Any]]:
    """Pedidos de confirmação da sessão que ainda não receberam resposta."""
    pedidos: dict[str, dict[str, Any]] = {}
    respondidos: set[str] = set()
    for evento in sessao.events:
        for fc in evento.get_function_calls():
            if fc.name == FC_CONFIRMACAO and fc.id:
                pedidos[fc.id] = fc.args or {}
        for fr in evento.get_function_responses():
            if fr.name == FC_CONFIRMACAO and fr.id:
                respondidos.add(fr.id)

    pendentes = []
    for fc_id, args in pedidos.items():
        if fc_id in respondidos:
            continue
        confirmacao = args.get("toolConfirmation") or {}
        original = args.get("originalFunctionCall") or {}
        payload = confirmacao.get("payload") or {}
        pendentes.append(
            {
                "id": fc_id,
                "acao": payload.get("acao") or original.get("name", ""),
                "detalhes": payload.get("detalhes") or original.get("args", {}),
                "mensagem": confirmacao.get("hint", ""),
            }
        )
    return pendentes


async def _executar(sessao: Session, mensagem: types.Content) -> dict[str, Any]:
    textos: list[str] = []
    try:
        async for evento in runner.run_async(
            user_id=sessao.user_id,
            session_id=sessao.id,
            new_message=mensagem,
            run_config=_RUN_CONFIG,
        ):
            if evento.partial or evento.author == "user" or not evento.content:
                continue
            texto = "".join(
                p.text for p in evento.content.parts or [] if p.text and not p.thought
            ).strip()
            if texto:
                textos.append(texto)
    except genai_errors.APIError:
        # Falha da Gemini API (cota, instabilidade) depois das retentativas.
        # O que as tools já gravaram continua valendo e as pendências são lidas
        # da sessão, então a resposta segue o contrato em vez de virar 500.
        logger.exception("Falha ao chamar o modelo na sessão %s", sessao.id)
        textos.append(
            "O assistente está temporariamente indisponível. Tente novamente"
            " em instantes."
        )
    atualizada = await obter_sessao(sessao.id)
    return {
        "resposta": "\n\n".join(textos),
        "confirmacoes_pendentes": [
            {k: p[k] for k in ("id", "acao", "detalhes")}
            for p in confirmacoes_pendentes(atualizada)
        ],
    }


async def enviar_mensagem(session_id: str, texto: str) -> dict[str, Any]:
    async with _locks[session_id]:
        sessao = await obter_sessao(session_id)
        mensagem = types.Content(role="user", parts=[types.Part(text=texto)])
        return await _executar(sessao, mensagem)


async def responder_confirmacao(
    session_id: str, confirmacao_id: str, confirmado: bool
) -> dict[str, Any]:
    async with _locks[session_id]:
        sessao = await obter_sessao(session_id)
        pendentes = {p["id"] for p in confirmacoes_pendentes(sessao)}
        if confirmacao_id not in pendentes:
            # Id inexistente, de outra sessão ou já respondido: nada executa.
            raise ConfirmacaoInexistente(confirmacao_id)
        resposta = types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id=confirmacao_id,
                        name=FC_CONFIRMACAO,
                        response={"confirmed": bool(confirmado)},
                    )
                )
            ],
        )
        return await _executar(sessao, resposta)


async def eventos(session_id: str) -> list[dict[str, Any]]:
    sessao = await obter_sessao(session_id)
    return [e.model_dump(mode="json", exclude_none=True) for e in sessao.events]
