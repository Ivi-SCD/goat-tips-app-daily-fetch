# Goat Tips — Sincronização Diária de Partidas

Azure Function App (Python v2) responsável por buscar os resultados encerrados da Premier League na **BetsAPI** e persistir os dados no **Supabase** (PostgreSQL).

---

## Visão Geral

Este módulo mantém o banco de dados atualizado diariamente com:

- Resultados de partidas encerradas
- Estatísticas por partida (incluindo xG)
- Odds finais (mercados múltiplos)
- Log de execução de cada sincronização

Possui dois pontos de entrada:

| Trigger | Rota / Agendamento | Descrição |
|---|---|---|
| Timer | `0 0 3 * * *` (03:00 UTC) | Sincronização automática diária |
| HTTP POST | `/api/refresh` | Reprocessamento manual com offset de dias |

---

## Arquitetura

```
BetsAPI
  │
  ├── GET /v1/events/ended    → lista de partidas encerradas do dia
  ├── GET /v1/event/view      → detalhes (árbitro, estádio, rodada)
  ├── GET /v1/event/stats     → estatísticas da partida (xG, chutes, etc.)
  └── GET /v2/event/odds/summary → odds finais por mercado
          │
          ▼
     run_sync()
          │
          ▼
     Supabase (PostgreSQL)
     ├── teams
     ├── events
     ├── match_stats
     ├── odds_snapshots
     └── sync_log
```

---

## Estrutura do Repositório

```
goat-tips-azr-func-daily/
├── function_app.py       # Registro dos triggers Azure Functions (v2)
├── sync_logic.py         # Lógica de negócio pura (sem dependência Azure)
├── host.json             # Configuração do runtime Azure Functions
├── local.settings.json   # Variáveis de ambiente locais (não versionado)
├── requirements.txt      # Dependências Python
├── .funcignore           # Arquivos ignorados no deploy
└── daily_sync/           # Módulo auxiliar
    └── __init__.py
```

---

## Variáveis de Ambiente

| Variável | Obrigatório | Padrão | Descrição |
|---|---|---|---|
| `BETSAPI_TOKEN` | ✅ | — | Token de autenticação da BetsAPI |
| `SUPABASE_DB_URL` | ✅ | — | Connection string PostgreSQL do Supabase |
| `PREMIER_LEAGUE_ID` | ❌ | `94` | ID da Premier League na BetsAPI |

Configure em `local.settings.json` para desenvolvimento local, ou nas **Application Settings** do Azure para produção.

---

## Triggers

### Timer — Sincronização Automática

Executa todos os dias às **03:00 UTC**, sincronizando as partidas do dia anterior.

```
Cron: 0 0 3 * * *
```

### HTTP — Reprocessamento Manual

```http
POST /api/refresh
Content-Type: application/json

{
  "day_offset": 0
}
```

**Parâmetros:**

| Campo | Tipo | Padrão | Descrição |
|---|---|---|---|
| `day_offset` | `int` | `0` | `0` = ontem, `1` = dois dias atrás, etc. |

**Resposta:**

```json
{
  "trigger": "http",
  "fetched": 10,
  "upserted": 10,
  "errors": 0,
  "duration_ms": 4821
}
```

---

## Tabelas do Supabase

### `teams`
Equipes upsertadas a partir dos campos `home`/`away` de cada evento.

| Coluna | Tipo | Descrição |
|---|---|---|
| `id` | `int` | ID da equipe na BetsAPI |
| `name` | `text` | Nome da equipe |
| `image_id` | `text` | ID do escudo (BetsAPI) |

### `events`
Uma linha por partida encerrada.

| Coluna | Tipo | Descrição |
|---|---|---|
| `id` | `int` | ID do evento |
| `time_utc` | `timestamptz` | Data/hora UTC da partida |
| `time_status` | `int` | `3` = encerrado |
| `home_score` / `away_score` | `int` | Placar final |
| `round` | `text` | Rodada |
| `referee_name` | `text` | Nome do árbitro |
| `stadium_name` | `text` | Nome do estádio |

### `match_stats`
Estatísticas por partida e período. Cada métrica (ex: `xg`, `shots`, `corners`) gera uma linha.

| Coluna | Tipo | Descrição |
|---|---|---|
| `event_id` | `int` | FK para `events` |
| `metric` | `text` | Nome da métrica |
| `home_value` / `away_value` | `float` | Valor por equipe |
| `period` | `text` | `full`, `1st`, `2nd` |

### `odds_snapshots`
Odds finais por mercado.

| Coluna | Tipo | Descrição |
|---|---|---|
| `event_id` | `int` | FK para `events` |
| `market_key` | `text` | Identificador do mercado |
| `home_od` / `draw_od` / `away_od` | `float` | Odds 1X2 |
| `over_od` / `under_od` | `float` | Odds Over/Under |

### `sync_log`
Registro de cada execução da sincronização.

| Coluna | Tipo | Descrição |
|---|---|---|
| `run_at` | `timestamptz` | Momento da execução |
| `trigger` | `text` | `daily_timer` ou `http` |
| `events_fetched` | `int` | Partidas encontradas na API |
| `events_upserted` | `int` | Partidas gravadas com sucesso |
| `errors` | `int` | Número de falhas individuais |
| `duration_ms` | `int` | Duração total em milissegundos |
| `notes` | `text` | Mensagens de erro (até 5) |

---

## Execução Local

```bash
# Instalar dependências
pip install -r requirements.txt

# Configurar variáveis de ambiente
# Editar local.settings.json com BETSAPI_TOKEN e SUPABASE_DB_URL

# Iniciar o runtime local do Azure Functions
func start
```

---

## Deploy no Azure

```bash
# Deploy via Azure Functions Core Tools
func azure functionapp publish <NOME_DO_APP>
```

Ou via GitHub Actions / Azure DevOps pipeline apontando para este repositório.

---

## Dependências

| Pacote | Versão mínima | Uso |
|---|---|---|
| `azure-functions` | 1.21 | Runtime Azure Functions v2 |
| `httpx` | 0.27 | Requisições HTTP para a BetsAPI |
| `psycopg2-binary` | 2.9 | Conexão com Supabase (PostgreSQL) |
| `python-dotenv` | 1.0 | Carregamento de `.env` local |

---

## Comportamento em Falhas

- Falhas individuais por evento são registradas em `sync_log.notes` e não interrompem o processamento dos demais eventos
- Se nenhuma partida for encontrada na BetsAPI para o dia consultado, a função retorna `{"fetched": 0, ...}` sem erro
- Falhas de conexão com a BetsAPI ou com o Supabase propagam exceção e são capturadas pelo runtime do Azure Functions
