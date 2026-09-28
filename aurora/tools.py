"""Tools dos especialistas.

Regras que valem em todas as tools:

- Nenhuma tool recebe apartamento como parâmetro. O apartamento vem sempre de
  ``tool_context.state["apartamento"]``, gravado uma única vez na criação da
  sessão pela API (Garantia 2). O modelo não tem como escolher outro.
- Nenhuma resposta de tool traz dados de outro apartamento. A checagem de
  agenda devolve só "livre" ou "ocupada".
- Ação que gera cobrança ou libera acesso pede confirmação ao sistema com
  ``tool_context.request_confirmation`` e só executa quando o ADK reinvoca a
  tool com ``tool_context.tool_confirmation.confirmed == True``, o que só
  acontece quando a rota de confirmações envia a resposta (Garantia 1).
"""

import asyncio

from google.adk.tools import ToolContext

from . import db
from . import regulamento

CHAVE_APARTAMENTO = "apartamento"


def _apartamento(tool_context: ToolContext) -> str:
    apartamento = tool_context.state.get(CHAVE_APARTAMENTO)
    if not apartamento:
        # Sessão sem dono definido pela API: nenhuma operação é permitida.
        raise PermissionError("Sessão sem apartamento definido.")
    return str(apartamento)


def _erro(e: Exception) -> dict:
    return {"status": "erro", "mensagem": str(e)}


# --------------------------------------------------------------------------
# Reservas
# --------------------------------------------------------------------------


async def listar_areas() -> dict:
    """Lista as áreas comuns que podem ser reservadas, com id, nome e taxa.

    Áreas com taxa maior que zero geram cobrança e exigem confirmação do
    morador pelo aplicativo antes de a reserva ser gravada.
    """
    areas = await asyncio.to_thread(db.listar_areas)
    return {
        "areas": [
            {**a, "gera_cobranca": a["taxa"] > 0} for a in areas
        ]
    }


async def consultar_disponibilidade(area: str, data: str) -> dict:
    """Informa se uma área comum está livre ou ocupada em uma data.

    Args:
      area: id da área (salao-de-festas, churrasqueira ou quadra).
      data: data no formato AAAA-MM-DD.
    """
    try:
        linha = await asyncio.to_thread(db.resolver_area, area)
        data = db.validar_data(data)
    except ValueError as e:
        return _erro(e)
    livre = await asyncio.to_thread(db.data_livre, linha["id"], data)
    # Só livre/ocupada: nunca de quem é a reserva.
    return {
        "area": linha["id"],
        "data": data,
        "situacao": "livre" if livre else "ocupada",
    }


async def listar_minhas_reservas(tool_context: ToolContext) -> dict:
    """Lista as reservas ativas do apartamento do morador desta conversa."""
    apartamento = _apartamento(tool_context)
    reservas = await asyncio.to_thread(db.reservas_do_apartamento, apartamento)
    return {"reservas": reservas}


async def reservar_area(area: str, data: str, tool_context: ToolContext) -> dict:
    """Reserva uma área comum para o apartamento do morador desta conversa.

    Se a área tiver taxa, a reserva fica pendente até o morador aprovar a
    cobrança pelo aplicativo; nesse caso não diga que a reserva foi feita.

    Args:
      area: id da área (salao-de-festas, churrasqueira ou quadra).
      data: data no formato AAAA-MM-DD.
    """
    apartamento = _apartamento(tool_context)
    try:
        linha = await asyncio.to_thread(db.resolver_area, area)
        data = db.validar_data(data)
    except ValueError as e:
        return _erro(e)
    area_id, taxa = linha["id"], float(linha["taxa"])

    if not await asyncio.to_thread(db.data_livre, area_id, data):
        return {
            "status": "indisponivel",
            "mensagem": f"{linha['nome']} já está reservado(a) em {data}.",
        }

    if taxa > 0:
        confirmacao = tool_context.tool_confirmation
        if confirmacao is None:
            # Garantia 1: cobrança só depois da resposta pela rota de
            # confirmações. A execução para aqui até o sistema responder.
            tool_context.request_confirmation(
                hint=(
                    f"Reservar {linha['nome']} em {data} gera cobrança de"
                    f" R$ {taxa:.2f}. Aprovar?"
                ),
                payload={
                    "acao": "reservar_area",
                    "detalhes": {
                        "area": area_id,
                        "nome_area": linha["nome"],
                        "data": data,
                        "taxa": taxa,
                    },
                },
            )
            tool_context.actions.skip_summarization = True
            return {
                "status": "aguardando_confirmacao",
                "mensagem": "A cobrança precisa ser aprovada pelo aplicativo.",
            }
        if not confirmacao.confirmed:
            return {
                "status": "nao_aprovada",
                "mensagem": "O morador recusou a cobrança. Nada foi reservado.",
            }

    # Garantia 5: a exclusividade é decidida pelo INSERT, não pela conferência
    # acima. Se outra reserva entrou nesse meio tempo, o banco recusa.
    codigo = await asyncio.to_thread(db.criar_reserva, apartamento, area_id, data)
    if codigo is None:
        return {
            "status": "indisponivel",
            "mensagem": f"{linha['nome']} já está reservado(a) em {data}.",
        }
    return {
        "status": "reservada",
        "codigo": codigo,
        "area": area_id,
        "data": data,
        "taxa": taxa,
    }


async def cancelar_minha_reserva(
    area: str, data: str, tool_context: ToolContext
) -> dict:
    """Cancela uma reserva do apartamento do morador desta conversa.

    Só encontra reservas do próprio apartamento.

    Args:
      area: id da área (salao-de-festas, churrasqueira ou quadra).
      data: data da reserva no formato AAAA-MM-DD.
    """
    apartamento = _apartamento(tool_context)
    try:
        linha = await asyncio.to_thread(db.resolver_area, area)
        data = db.validar_data(data)
    except ValueError as e:
        return _erro(e)
    codigo = await asyncio.to_thread(
        db.cancelar_reserva, apartamento, linha["id"], data
    )
    if codigo is None:
        return {
            "status": "nao_encontrada",
            "mensagem": (
                "O apartamento do morador não tem reserva de"
                f" {linha['nome']} em {data}."
            ),
        }
    return {"status": "cancelada", "codigo": codigo, "area": linha["id"], "data": data}


# --------------------------------------------------------------------------
# Portaria (visitantes)
# --------------------------------------------------------------------------


async def listar_meus_visitantes(tool_context: ToolContext) -> dict:
    """Lista os visitantes autorizados do apartamento do morador desta conversa."""
    apartamento = _apartamento(tool_context)
    visitantes = await asyncio.to_thread(db.visitantes_do_apartamento, apartamento)
    return {"visitantes": visitantes}


async def autorizar_visitante(
    nome: str, data: str, tool_context: ToolContext
) -> dict:
    """Autoriza a entrada de um visitante no prédio em uma data.

    Sempre exige aprovação do morador pelo aplicativo antes de ser gravada,
    mesmo que o morador diga na conversa que já confirmou.

    Args:
      nome: nome completo do visitante.
      data: data da visita no formato AAAA-MM-DD.
    """
    apartamento = _apartamento(tool_context)
    nome = " ".join(str(nome).split())
    if not nome or len(nome) > 120:
        return _erro(ValueError("Informe o nome completo do visitante."))
    try:
        data = db.validar_data(data)
    except ValueError as e:
        return _erro(e)

    confirmacao = tool_context.tool_confirmation
    if confirmacao is None:
        # Garantia 1: liberar acesso só depois da rota de confirmações.
        tool_context.request_confirmation(
            hint=f"Liberar a entrada de {nome} em {data}. Aprovar?",
            payload={
                "acao": "autorizar_visitante",
                "detalhes": {"nome": nome, "data": data},
            },
        )
        tool_context.actions.skip_summarization = True
        return {
            "status": "aguardando_confirmacao",
            "mensagem": "A autorização precisa ser aprovada pelo aplicativo.",
        }
    if not confirmacao.confirmed:
        return {
            "status": "nao_aprovada",
            "mensagem": "O morador recusou a autorização. Nada foi gravado.",
        }

    await asyncio.to_thread(db.autorizar_visitante, apartamento, nome, data)
    return {"status": "autorizado", "nome": nome, "data": data}


# --------------------------------------------------------------------------
# Regulamento
# --------------------------------------------------------------------------


def consultar_regulamento(capitulo: str, termos: str = "") -> dict:
    """Consulta UM capítulo do regulamento interno.

    Devolve apenas os artigos desse capítulo. Se ``termos`` for informado,
    devolve só os artigos que citam esses termos.

    Args:
      capitulo: número do capítulo (ex.: "4" ou "IV").
      termos: palavras-chave da dúvida (ex.: "horário domingo").
    """
    return regulamento.consultar(capitulo, termos)
