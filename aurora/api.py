"""API HTTP do assistente, conforme o contrato do desafio."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi import HTTPException
from pydantic import BaseModel

from . import db
from . import service


@asynccontextmanager
async def lifespan(_: FastAPI):
    await asyncio.to_thread(db.inicializar)
    yield
    await service.runner.close()


api = FastAPI(title="Assistente do Residencial Aurora", lifespan=lifespan)


class NovaSessao(BaseModel):
    apartamento: str


class NovaMensagem(BaseModel):
    texto: str


class RespostaConfirmacao(BaseModel):
    id: str
    confirmado: bool


def _sessao_404(session_id: str) -> HTTPException:
    return HTTPException(404, f"Sessão {session_id} não encontrada.")


@api.post("/sessoes", status_code=201)
async def criar_sessao(corpo: NovaSessao):
    apartamento = corpo.apartamento.strip()
    if not await asyncio.to_thread(db.apartamento_existe, apartamento):
        raise HTTPException(422, f"Apartamento {apartamento} não existe.")
    return {"session_id": await service.criar_sessao(apartamento)}


@api.post("/sessoes/{session_id}/mensagens")
async def enviar_mensagem(session_id: str, corpo: NovaMensagem):
    try:
        return await service.enviar_mensagem(session_id, corpo.texto)
    except service.SessaoInexistente:
        raise _sessao_404(session_id)


@api.post("/sessoes/{session_id}/confirmacoes")
async def responder_confirmacao(session_id: str, corpo: RespostaConfirmacao):
    try:
        return await service.responder_confirmacao(
            session_id, corpo.id, corpo.confirmado
        )
    except service.SessaoInexistente:
        raise _sessao_404(session_id)
    except service.ConfirmacaoInexistente:
        raise HTTPException(
            409, "Não existe confirmação pendente com esse id nesta sessão."
        )


@api.get("/sessoes/{session_id}/eventos")
async def listar_eventos(session_id: str):
    try:
        return await service.eventos(session_id)
    except service.SessaoInexistente:
        raise _sessao_404(session_id)


# Rotas de verificação: leem o banco direto, sem passar pelo modelo.


@api.get("/apartamentos/{numero}/reservas")
async def reservas_do_apartamento(numero: str):
    return await asyncio.to_thread(db.reservas_do_apartamento, numero)


@api.get("/apartamentos/{numero}/visitantes")
async def visitantes_do_apartamento(numero: str):
    return await asyncio.to_thread(db.visitantes_do_apartamento, numero)
