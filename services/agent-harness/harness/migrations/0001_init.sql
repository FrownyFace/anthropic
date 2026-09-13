-- Faultline harness store, migration 1 (ARCHITECTURE.md §4.4).
-- All timestamps ISO-8601 UTC ("…Z"). JSON columns hold pydantic dumps from faultline_common.schemas.
-- Applied by harness/sqlite_store.py inside one transaction; `PRAGMA user_version` gates it.
--
-- Deviation from the doc, deliberate: `runs.status` CHECK also allows 'unevaluated' and
-- 'interrupted', which schemas.RunStatus gained with PLAN.md §2.11. The doc's five-value CHECK
-- would reject a run that ends interrupted and lose the whole transcript.

CREATE TABLE users (
  id            TEXT PRIMARY KEY,                     -- u_<uuid4>, minted by the browser (§6)
  created_at    TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,
  user_agent    TEXT
);

CREATE TABLE conversations (
  id            TEXT PRIMARY KEY,                     -- c_…
  user_id       TEXT NOT NULL REFERENCES users(id),
  scenario_id   TEXT NOT NULL,
  title         TEXT NOT NULL,                        -- defaults to the scenario title; PATCHable
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,                        -- bumped on every run event (drives list order)
  archived_at   TEXT                                  -- soft delete
);
CREATE INDEX ix_conversations_user ON conversations(user_id, updated_at DESC);

CREATE TABLE runs (
  id              TEXT PRIMARY KEY,                   -- r_…
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  user_id         TEXT NOT NULL REFERENCES users(id), -- denormalised for ownership checks
  scenario_id     TEXT NOT NULL,
  model           TEXT NOT NULL,
  seed            INTEGER,
  max_steps       INTEGER NOT NULL,
  task_prompt     TEXT NOT NULL,                      -- what the user turn actually contained
  episode_id      TEXT,                               -- sandbox-env episode (set after reset)
  status          TEXT NOT NULL CHECK (status IN
                    ('queued','running','ok','error','truncated','unevaluated','interrupted')),
  score           REAL,                               -- 0..100 from evaluate
  evaluation_json TEXT,                               -- EvaluateResponse (checks, tests, ledger)
  input_tokens    INTEGER NOT NULL DEFAULT 0,
  output_tokens   INTEGER NOT NULL DEFAULT 0,
  error           TEXT,
  created_at      TEXT NOT NULL,
  started_at      TEXT,
  finished_at     TEXT,
  steps           INTEGER,                            -- final step count (RunRecord.steps)
  summary         TEXT,                               -- the agent's submit summary
  extra_json      TEXT                                -- additive RunRecord fields (usage detail, …)
);
CREATE INDEX ix_runs_conversation ON runs(conversation_id, created_at);
CREATE INDEX ix_runs_user ON runs(user_id, created_at DESC);
CREATE INDEX ix_runs_live ON runs(status) WHERE status IN ('queued','running');

-- Append-only event log: the source of truth for replay, SSE resume and evidence export.
CREATE TABLE events (
  run_id  TEXT    NOT NULL REFERENCES runs(id),
  seq     INTEGER NOT NULL,                           -- 0-based; == Event.id == SSE id / Last-Event-ID
  ts      TEXT    NOT NULL,
  type    TEXT    NOT NULL,                           -- EventType (run.started … llm.call, turn.thinking)
  step    INTEGER,
  data    TEXT    NOT NULL,                           -- JSON: Event.data
  PRIMARY KEY (run_id, seq)
) WITHOUT ROWID;

-- Transcript projection in Anthropic message shape: what the UI renders and what the model saw.
CREATE TABLE messages (
  id              TEXT PRIMARY KEY,                   -- m_…
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  run_id          TEXT NOT NULL REFERENCES runs(id),
  seq             INTEGER NOT NULL,                   -- order within the conversation
  role            TEXT NOT NULL CHECK (role IN ('user','assistant')),
  step            INTEGER,                            -- harness step (NULL for the task prompt)
  created_at      TEXT NOT NULL,
  UNIQUE (conversation_id, seq)
);
CREATE INDEX ix_messages_run ON messages(run_id, seq);

CREATE TABLE blocks (
  id           TEXT PRIMARY KEY,                      -- b_…
  message_id   TEXT NOT NULL REFERENCES messages(id),
  seq          INTEGER NOT NULL,                      -- order within the message
  type         TEXT NOT NULL CHECK (type IN ('text','thinking','tool_use','tool_result')),
  text         TEXT,                                  -- text / thinking / tool_result output
  tool_name    TEXT,                                  -- tool_use
  tool_use_id  TEXT,                                  -- tool_use and tool_result (join key)
  input_json   TEXT,                                  -- tool_use input
  is_error     INTEGER,                               -- tool_result
  exit_code    INTEGER,                               -- tool_result (run_command)
  duration_ms  INTEGER,                               -- tool_result
  fault_json   TEXT,                                  -- FaultFired echoed on the result, if any
  truncated    INTEGER NOT NULL DEFAULT 0,
  UNIQUE (message_id, seq)
);
CREATE INDEX ix_blocks_tool_use ON blocks(tool_use_id);

-- One row per messages.create: the "LLM turn". Retries on 429/5xx get attempt > 1.
CREATE TABLE llm_calls (
  id                 TEXT PRIMARY KEY,                -- l_…
  run_id             TEXT NOT NULL REFERENCES runs(id),
  step               INTEGER NOT NULL,
  attempt            INTEGER NOT NULL DEFAULT 1,
  model              TEXT NOT NULL,
  request_id         TEXT,                            -- anthropic `request-id` response header
  stop_reason        TEXT,                            -- end_turn | tool_use | max_tokens | …
  input_tokens       INTEGER,
  output_tokens      INTEGER,
  cache_read_tokens  INTEGER,
  cache_write_tokens INTEGER,
  started_at         TEXT NOT NULL,
  duration_ms        INTEGER,
  error              TEXT,
  UNIQUE (run_id, step, attempt)
);
