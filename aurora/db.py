"""Armazenamento do condomínio em SQLite.

As regras que não podem depender do modelo moram aqui, no esquema do banco:

- ``idx_reserva_ativa_unica``: índice único parcial em (area, data) para
    reservas ativas. A exclusividade vale no instante do INSERT, então duas
    gravações simultâneas nunca produzem duas reservas ativas (Garantia 5).
- ``codigos_emitidos``: registro permanente de todo código de reserva já usado.
    Nunca é apagado, nem por cancelamento nem pela restauração, então um código
    novo nunca repete o de outra reserva.
- Toda consulta e alteração de reservas e visitantes recebe o apartamento
    explicitamente e filtra por ele (Garantia 2).
"""

import json
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import date

from . import config

ESQUEMA = """
CREATE TABLE IF NOT EXISTS apartamentos (
        numero  TEXT PRIMARY KEY,
        morador TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS areas (
        id   TEXT PRIMARY KEY,
        nome TEXT NOT NULL,
        taxa REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS codigos_emitidos (
        seq    INTEGER PRIMARY KEY AUTOINCREMENT,
        codigo TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS reservas (
        codigo       TEXT PRIMARY KEY REFERENCES codigos_emitidos(codigo),
        apartamento  TEXT NOT NULL,
        area         TEXT NOT NULL REFERENCES areas(id),
        data         TEXT NOT NULL,
        status       TEXT NOT NULL DEFAULT 'ativa'
                                  CHECK (status IN ('ativa', 'cancelada')),
        criada_em    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        cancelada_em TEXT
);

-- Garantia 5: no máximo uma reserva ATIVA por área e data, validado pelo
-- próprio SQLite no momento da gravação.
CREATE UNIQUE INDEX IF NOT EXISTS idx_reserva_ativa_unica
        ON reservas (area, data) WHERE status = 'ativa';

CREATE TABLE IF NOT EXISTS visitantes (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        apartamento TEXT NOT NULL,
        nome        TEXT NOT NULL,
        data        TEXT NOT NULL,
        criado_em   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (apartamento, nome, data)
);

-- Dono de cada sessão, definido uma única vez na criação.
CREATE TABLE IF NOT EXISTS sessoes (
        session_id  TEXT PRIMARY KEY,
        apartamento TEXT NOT NULL,
        criada_em   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class DataInvalida(ValueError):
    pass


class AreaInvalida(ValueError):
    pass


@contextmanager
def conexao():
    config.DIR_ESTADO.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(config.DB_CONDOMINIO, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA busy_timeout = 30000")
    try:
        yield con
    finally:
        con.close()


def inicializar() -> None:
    """Cria o esquema e carrega os dados iniciais se o banco estiver vazio."""
    with conexao() as con:
        con.executescript(ESQUEMA)
        vazio = con.execute("SELECT COUNT(*) FROM areas").fetchone()[0] == 0
    if vazio:
        restaurar()


def _ler_json(nome: str):
    with open(config.DIR_DADOS_INICIAIS / nome, encoding="utf-8") as f:
        return json.load(f)


def restaurar() -> None:
    """Volta reservas e visitantes ao estado de dados/*.json.

    ``codigos_emitidos`` e ``sessoes`` são preservados: o histórico de códigos
    garante que nenhum código futuro repita um código já usado.
    """
    apartamentos = _ler_json("apartamentos.json")
    areas = _ler_json("areas.json")
    reservas = _ler_json("reservas.json")
    visitantes = _ler_json("visitantes.json")
    with conexao() as con:
        con.executescript(ESQUEMA)
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute("DELETE FROM reservas")
            con.execute("DELETE FROM visitantes")
            con.execute("DELETE FROM areas")
            con.execute("DELETE FROM apartamentos")
            con.executemany(
                    "INSERT INTO apartamentos (numero, morador) VALUES (?, ?)",
                    [(a["numero"], a["morador"]) for a in apartamentos],
            )
            con.executemany(
                    "INSERT INTO areas (id, nome, taxa) VALUES (?, ?, ?)",
                    [(a["id"], a["nome"], float(a["taxa"])) for a in areas],
            )
            for r in reservas:
                con.execute(
                        "INSERT OR IGNORE INTO codigos_emitidos (codigo) VALUES (?)",
                        (r["codigo"],),
                )
                con.execute(
                        "INSERT INTO reservas (codigo, apartamento, area, data)"
                        " VALUES (?, ?, ?, ?)",
                        (r["codigo"], r["apartamento"], r["area"], r["data"]),
                )
            con.executemany(
                    "INSERT INTO visitantes (apartamento, nome, data) VALUES (?, ?, ?)",
                    [(v["apartamento"], v["nome"], v["data"]) for v in visitantes],
            )
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise


# --------------------------------------------------------------------------
# Validação de entradas vindas do modelo
# --------------------------------------------------------------------------


def validar_data(data: str) -> str:
    try:
        return date.fromisoformat(str(data).strip()).isoformat()
    except ValueError as e:
        raise DataInvalida(
                f"Data inválida: {data!r}. Use o formato AAAA-MM-DD."
        ) from e


def _normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore")
    return sem_acento.decode().lower().strip().replace(" ", "-")


def resolver_area(area: str) -> sqlite3.Row:
    """Aceita o id ou o nome da área e devolve a linha da tabela areas."""
    alvo = _normalizar(area)
    with conexao() as con:
        linhas = con.execute("SELECT id, nome, taxa FROM areas").fetchall()
    for linha in linhas:
        if alvo in (linha["id"], _normalizar(linha["nome"])):
            return linha
    for linha in linhas:
        if alvo and (alvo in linha["id"] or linha["id"] in alvo):
            return linha
    ids = ", ".join(l["id"] for l in linhas)
    raise AreaInvalida(f"Área desconhecida: {area!r}. Áreas válidas: {ids}.")


def listar_areas() -> list[dict]:
    with conexao() as con:
        linhas = con.execute("SELECT id, nome, taxa FROM areas ORDER BY id")
        return [dict(l) for l in linhas]


def apartamento_existe(numero: str) -> bool:
    with conexao() as con:
        return (
                con.execute(
                        "SELECT 1 FROM apartamentos WHERE numero = ?", (numero,)
                ).fetchone()
                is not None
        )


# --------------------------------------------------------------------------
# Reservas
# --------------------------------------------------------------------------


def reservas_do_apartamento(apartamento: str) -> list[dict]:
    with conexao() as con:
        linhas = con.execute(
                "SELECT codigo, area, data FROM reservas"
                " WHERE apartamento = ? AND status = 'ativa' ORDER BY data, area",
                (apartamento,),
        )
        return [dict(l) for l in linhas]


def data_livre(area_id: str, data: str) -> bool:
    """Diz só se a data está livre. Nunca revela de quem é a reserva."""
    with conexao() as con:
        return (
                con.execute(
                        "SELECT 1 FROM reservas"
                        " WHERE area = ? AND data = ? AND status = 'ativa'",
                        (area_id, data),
                ).fetchone()
                is None
        )


def criar_reserva(apartamento: str, area_id: str, data: str) -> str | None:
    """Grava a reserva e devolve o código, ou None se a data já estiver ocupada.

    Não há "conferir e depois gravar": o INSERT é a própria conferência. Se
    outra reserva ativa para (area, data) já existe, inclusive uma gravada por
    outra requisição simultânea, o índice único parcial rejeita o INSERT e a
    transação é desfeita.
    """
    with conexao() as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            seq = con.execute(
                    "INSERT INTO codigos_emitidos (codigo) VALUES ('pendente')"
                    " RETURNING seq"
            ).fetchone()[0]
            codigo = f"AUR-{seq:06d}"
            con.execute(
                    "UPDATE codigos_emitidos SET codigo = ? WHERE seq = ?", (codigo, seq)
            )
            con.execute(
                    "INSERT INTO reservas (codigo, apartamento, area, data)"
                    " VALUES (?, ?, ?, ?)",
                    (codigo, apartamento, area_id, data),
            )
            con.execute("COMMIT")
            return codigo
        except sqlite3.IntegrityError:
            con.execute("ROLLBACK")
            return None
        except BaseException:
            con.execute("ROLLBACK")
            raise


def cancelar_reserva(apartamento: str, area_id: str, data: str) -> str | None:
    """Cancela a reserva ativa do apartamento. Devolve o código ou None.

    O filtro por apartamento está no próprio UPDATE: uma reserva de outro
    apartamento simplesmente não é encontrada.
    """
    with conexao() as con:
        linha = con.execute(
                "UPDATE reservas SET status = 'cancelada',"
                " cancelada_em = CURRENT_TIMESTAMP"
                " WHERE apartamento = ? AND area = ? AND data = ? AND status = 'ativa'"
                " RETURNING codigo",
                (apartamento, area_id, data),
        ).fetchone()
        return linha["codigo"] if linha else None


# --------------------------------------------------------------------------
# Visitantes
# --------------------------------------------------------------------------


def visitantes_do_apartamento(apartamento: str) -> list[dict]:
    with conexao() as con:
        linhas = con.execute(
                "SELECT nome, data FROM visitantes WHERE apartamento = ?"
                " ORDER BY data, nome",
                (apartamento,),
        )
        return [dict(l) for l in linhas]


def autorizar_visitante(apartamento: str, nome: str, data: str) -> bool:
    """Grava a autorização. Devolve False se ela já existia (idempotente)."""
    with conexao() as con:
        cur = con.execute(
                "INSERT OR IGNORE INTO visitantes (apartamento, nome, data)"
                " VALUES (?, ?, ?)",
                (apartamento, nome, data),
        )
        return cur.rowcount == 1


# --------------------------------------------------------------------------
# Sessões
# --------------------------------------------------------------------------


def registrar_sessao(session_id: str, apartamento: str) -> None:
    with conexao() as con:
        con.execute(
                "INSERT INTO sessoes (session_id, apartamento) VALUES (?, ?)",
                (session_id, apartamento),
        )


def apartamento_da_sessao(session_id: str) -> str | None:
    with conexao() as con:
        linha = con.execute(
                "SELECT apartamento FROM sessoes WHERE session_id = ?", (session_id,)
        ).fetchone()
        return linha["apartamento"] if linha else None
