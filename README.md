# Residencial Aurora: assistente virtual dos moradores

Assistente de chat do Residencial Aurora, construído com **Google ADK 2.10.0** e
exposto por uma API **FastAPI** em `http://localhost:8000`. Pelo chat, o morador
reserva e cancela áreas comuns, autoriza visitantes e tira dúvidas sobre o
regulamento.

O princípio do projeto: **o modelo decide o caminho, o código decide o que é
permitido.** As regras críticas estão nas tools, no banco e na camada de
serviço, e continuam valendo seja qual for a mensagem do morador.

```
aurora/
  agents.py       agente principal, especialistas e App (resumível)
  tools.py        tools dos especialistas (apartamento vem da sessão)
  db.py           SQLite do condomínio: esquema, índice único, restauração
  regulamento.py  leitura do regulamento por capítulo
  service.py      Runner, sessões persistidas, confirmações pendentes
  api.py          rotas HTTP do contrato
  cli.py          comandos `api` e `restaurar`
dados/            estado inicial (somente leitura, idêntico ao repositório base)
estado/           bancos SQLite gerados em execução (fora do Git)
```

## Arquitetura

```
                        morador (API)
                             │
                   ┌─────────▼──────────┐
                   │ assistente_aurora  │  principal: sem tools de dados,
                   │  (LlmAgent, chat)  │  sem regulamento nas instruções
                   └──┬──────┬───────┬──┘
     transfer_to_agent│      │       │AgentTool (sessão isolada em memória)
          ┌───────────▼┐ ┌───▼──────────────┐ ┌──▼─────────────────────┐
          │especialista│ │especialista      │ │especialista_regulamento│
          │_reservas   │ │_portaria         │ │ tool: consultar_       │
          │5 tools     │ │2 tools           │ │       regulamento      │
          └─────┬──────┘ └────────┬─────────┘ └────────────────────────┘
                └──── SQLite (estado/condominio.db) ────┘
```

| Agente | Responsabilidade | Como é acionado | Por quê |
|---|---|---|---|
| `assistente_aurora` ([agents.py:126](aurora/agents.py#L126)) | Conversa com o morador e decide quem atende. Não lê nem grava dados. | É o `root_agent` do `App`. | Um ponto de entrada só, com uma instrução curta. Como não tem tools de dados, não consegue agir fora dos especialistas. |
| `especialista_reservas` ([agents.py:42](aurora/agents.py#L42)) | Listar áreas, consultar disponibilidade, reservar, listar e cancelar reservas do próprio apartamento. | Sub-agente, por **transferência** (`transfer_to_agent`). | A reserva de área com taxa pede confirmação. O pedido de confirmação precisa ficar na **sessão persistida**, e a resposta precisa voltar ao agente que o emitiu. Com transferência, os eventos do especialista ficam na sessão e o Runner resumível encontra o autor da chamada pendente, inclusive depois de reiniciar a API. |
| `especialista_portaria` ([agents.py:77](aurora/agents.py#L77)) | Autorizar visitantes e listar os autorizados do próprio apartamento. | Sub-agente, por **transferência**. | Mesmo motivo: autorizar visitante sempre pede confirmação. |
| `especialista_regulamento` ([agents.py:102](aurora/agents.py#L102)) | Responder dúvidas com base em `dados/regulamento.md`. | **`AgentTool`** do agente principal. | O `AgentTool` roda o especialista num `Runner` próprio, com `InMemorySessionService`. As consultas ao regulamento e as tentativas de achar o capítulo certo ficam nessa sessão descartável. Na sessão do morador entra só a pergunta e a resposta final, curta. Isso mantém o texto do regulamento fora do histórico (Garantia 4). |

Os especialistas de reservas e portaria podem devolver a conversa ao
principal ou passar para o outro especialista quando o pedido muda de assunto.
O `App` usa `ResumabilityConfig(is_resumable=True)`
([agents.py:153](aurora/agents.py#L153)). Com isso, a resposta a uma
confirmação é roteada ao agente que fez a chamada pendente e a invocação
pausada é retomada.

**Armazenamento.** SQLite em dois arquivos dentro de `estado/`:

- `condominio.db`: áreas, apartamentos, reservas, visitantes, histórico de
  códigos e dono de cada sessão. É acessado com `sqlite3`.
- `sessoes.db`: sessões e eventos do ADK, via `SqliteSessionService`.

Não há serviço externo. Os bancos são criados na primeira subida da API ou
pelo comando de restauração.

**Modelos.** Todos os agentes usam `gemini-3.5-flash` por padrão. Dá para
trocar em `.env` com `MODELO_PRINCIPAL` e `MODELO_ESPECIALISTAS`. Cada
mensagem tem um teto de 25 chamadas ao modelo (`RunConfig(max_llm_calls=25)`
em [service.py:49](aurora/service.py#L49)), para que um laço de transferências
não consuma a cota.

## Garantias

### Garantia 1: cobrança ou acesso só com confirmação

- **Pedido de confirmação nas tools.**
  - [`tools.reservar_area`](aurora/tools.py#L85) lê a taxa da área no banco. Se ela for maior que zero e não houver `tool_context.tool_confirmation`, a tool chama `tool_context.request_confirmation(...)` ([tools.py:114](aurora/tools.py#L114)) com `acao` e `detalhes` (área, data, taxa) e retorna sem gravar.
  - [`tools.autorizar_visitante`](aurora/tools.py#L200) faz o mesmo sempre ([tools.py:224](aurora/tools.py#L224)), com nome e data.
  - A gravação só acontece quando o ADK reinvoca a tool com `tool_confirmation.confirmed == True`. Se a resposta for negativa, a tool retorna sem gravar ([tools.py:134](aurora/tools.py#L134), [tools.py:236](aurora/tools.py#L236)).
  - Área com taxa zero, cancelamento e consultas não pedem confirmação.
- **Pendências vêm da sessão persistida.** [`service.confirmacoes_pendentes`](aurora/service.py#L74) lista as chamadas `adk_request_confirmation` que ainda não têm resposta. Esse é o campo `confirmacoes_pendentes` da API.
- **Rota de confirmações.** [`service.responder_confirmacao`](aurora/service.py#L134) aceita só um id que esteja nessa lista. Qualquer outro id levanta `ConfirmacaoInexistente` ([service.py:142](aurora/service.py#L142)) e a API responde 409 antes de chamar o Runner. Isso vale para id inexistente, id de outra sessão e id já respondido. Um id válido vira um `FunctionResponse(name="adk_request_confirmation", response={"confirmed": ...})` enviado ao Runner.

**Por que não depende do modelo.**
- A rota de mensagens só envia texto ao Runner. Um `tool_confirmation` só existe quando o ADK recebe um `FunctionResponse` de confirmação, e ele só é montado pela rota de confirmações. Escrever "já confirmei" não cria esse objeto.
- Ao retomar, o ADK confere se a chamada original tem o mesmo nome e os mesmos argumentos que estão no histórico. Assim, o que executa é exatamente o que foi mostrado em `detalhes`.
- Um lock por sessão ([service.py:45](aurora/service.py#L45)) serializa mensagens e confirmações da mesma sessão. Isso impede que um reenvio entre antes de a primeira resposta ser gravada.

### Garantia 2: cada sessão pertence a um apartamento

- [`service.criar_sessao`](aurora/service.py#L52) grava `{"apartamento": ...}` no state da sessão ([service.py:56](aurora/service.py#L56)). Isso acontece uma única vez, na criação (`POST /sessoes`), e nenhuma rota ou tool altera essa chave depois.
- Nenhuma tool tem parâmetro de apartamento. Todas obtêm o apartamento por [`tools._apartamento`](aurora/tools.py#L26), que lê `tool_context.state["apartamento"]`.
- Toda consulta e alteração no banco filtra por esse apartamento no próprio SQL:
  - `WHERE apartamento = ?` em [db.py:215](aurora/db.py#L215) e [db.py:293](aurora/db.py#L293);
  - `UPDATE ... WHERE apartamento = ? AND area = ? AND data = ?` em [db.py:278](aurora/db.py#L278).
- Uma reserva de outro apartamento não é encontrada. Nesse caso a tool devolve `nao_encontrada` e nada é alterado.
- A checagem de agenda ([`db.data_livre`](aurora/db.py#L221), [`tools.consultar_disponibilidade`](aurora/tools.py#L57)) devolve só `livre`/`ocupada`. Reservar numa data ocupada devolve só `indisponivel`. Nenhuma resposta de tool traz código, apartamento ou nome de outro morador.

**Por que não depende do modelo.** O modelo não tem como informar outro apartamento, porque esse parâmetro não existe. Os dados de outro apartamento nunca chegam ao contexto, então não há o que vazar para a resposta nem para os eventos. As instruções pedem que o assistente recuse pedidos sobre outros apartamentos, mas a proteção de fato está nas tools e no SQL.

### Garantia 3: nada se perde no reinício

- Sessões e eventos ficam em SQLite, via `SqliteSessionService` ([service.py:41](aurora/service.py#L41)). Reservas, cancelamentos e visitantes ficam em `estado/condominio.db` ([db.py](aurora/db.py)).
- Para achar a sessão pelo `session_id`, a tabela `sessoes` guarda o dono de cada sessão ([`db.registrar_sessao`](aurora/db.py#L316)).
- As pendências são recalculadas a partir dos eventos persistidos, então uma confirmação pedida antes do reinício pode ser aprovada depois dele.
- Os códigos novos (`AUR-000001`, `AUR-000002`, ...) saem da tabela `codigos_emitidos` ([db.py:35](aurora/db.py#L35)). Ela registra todo código já usado, inclusive os iniciais, e nunca é apagada, nem por cancelamento nem pela restauração. Um código nunca se repete.

**Por que não depende do modelo.** Nada fica só em memória. O `Runner` é recriado a cada subida e lê tudo do disco.

### Garantia 4: o regulamento é consultado, não carregado

- O agente principal ([agents.py:126](aurora/agents.py#L126)) não recebe o regulamento nas instruções. O especialista de regulamento recebe só o índice de títulos dos capítulos ([`regulamento.indice`](aurora/regulamento.py#L63)).
- A tool `consultar_regulamento` chama [`regulamento.consultar`](aurora/regulamento.py#L78), que devolve artigos de **um único capítulo**, filtrados pelos termos da dúvida. Para a piscina aos domingos, ela devolve só os Arts. 22 e 27.
- O especialista é um `AgentTool` ([agents.py:149](aurora/agents.py#L149)). As chamadas de tool dele ficam numa sessão em memória descartada ao fim da consulta. Na sessão do morador entram só a chamada `especialista_regulamento` e a resposta final, curta.

**Por que não depende do modelo.** Não existe tool que devolva o regulamento inteiro nem mais de um capítulo. Mesmo se o especialista abrir o capítulo errado, isso acontece fora da sessão persistida.

### Garantia 5: dois moradores, uma reserva

- O índice único parcial `idx_reserva_ativa_unica ON reservas (area, data) WHERE status = 'ativa'` ([db.py:53](aurora/db.py#L53)) proíbe, no próprio banco, duas reservas ativas para a mesma área e data. Reservas canceladas não contam.
- [`db.criar_reserva`](aurora/db.py#L234) grava dentro de `BEGIN IMMEDIATE`. Se o `INSERT` violar o índice, captura `sqlite3.IntegrityError` ([db.py:260](aurora/db.py#L260)), desfaz a transação e devolve `None`. A tool transforma isso em `status: "indisponivel"`, e o modelo responde normalmente, com HTTP 200.
- A conferência de agenda feita antes em `reservar_area` serve só para responder mais cedo. Quem decide é o `INSERT`.

**Por que não depende do modelo.** A exclusividade é uma restrição do SQLite e vale no instante da gravação, qualquer que seja a ordem das requisições. Num teste com 24 threads gravando a mesma área e data ao mesmo tempo, sem a conferência prévia, uma gravação venceu e 23 foram recusadas.

## Como rodar

**Pré-requisitos**

- [uv](https://docs.astral.sh/uv/). Ele instala o Python 3.12 fixado em `.python-version`, se necessário.
- Uma chave da Gemini API do [Google AI Studio](https://aistudio.google.com/apikey).
- Não há serviço externo para subir. O armazenamento é SQLite local.

**Variáveis do `.env`**

```bash
cp .env.example .env
```

| Variável | Obrigatória | Descrição |
|---|---|---|
| `GOOGLE_API_KEY` | sim | chave do Google AI Studio |
| `GOOGLE_GENAI_USE_VERTEXAI` | não | deixe `FALSE` (usa a Gemini API, não a Vertex AI) |
| `MODELO_PRINCIPAL` | não | modelo do agente principal (padrão `gemini-3.5-flash`) |
| `MODELO_ESPECIALISTAS` | não | modelo dos especialistas (padrão: igual ao principal) |

**Instalar**

```bash
uv sync
```

**Restaurar os dados iniciais**

Volta reservas e visitantes ao estado de `dados/*.json`:

```bash
uv run restaurar
```

Por padrão as conversas são mantidas. Para apagar também todas as sessões:

```bash
uv run restaurar --apagar-sessoes
```

O histórico de códigos de reserva nunca é apagado.

**Subir a API** em `http://localhost:8000`:

```bash
uv run api
```

Para parar, use Ctrl+C. Para subir de novo, rode o mesmo comando. Os dados e as
sessões continuam em `estado/`.

**Exemplo rápido**

```bash
S=$(curl -s -X POST localhost:8000/sessoes -H 'content-type: application/json' \
      -d '{"apartamento":"101"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["session_id"])')
curl -s -X POST localhost:8000/sessoes/$S/mensagens -H 'content-type: application/json' \
     -d '{"texto":"Reserve o salão de festas para 2030-04-20"}'
# copie o id em confirmacoes_pendentes e aprove:
curl -s -X POST localhost:8000/sessoes/$S/confirmacoes -H 'content-type: application/json' \
     -d '{"id":"<id>","confirmado":true}'
curl -s localhost:8000/apartamentos/101/reservas
```

**Rotas**

| Método e caminho | Descrição |
|---|---|
| `POST /sessoes` | cria a sessão do apartamento (201) |
| `POST /sessoes/{id}/mensagens` | envia mensagem; devolve `resposta` e `confirmacoes_pendentes` |
| `POST /sessoes/{id}/confirmacoes` | responde uma confirmação pendente; 409 se o id não estiver pendente nesta sessão |
| `GET /sessoes/{id}/eventos` | todos os eventos da sessão, em ordem |
| `GET /apartamentos/{n}/reservas` | verificação: reservas ativas, lidas direto do banco |
| `GET /apartamentos/{n}/visitantes` | verificação: visitantes autorizados, lidos direto do banco |

As rotas com `{id}` respondem 404 para sessão inexistente.
