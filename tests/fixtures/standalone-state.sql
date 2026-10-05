-- Snapshot of skill-learning-service's learning schema. Kept locally on purpose.
CREATE TABLE submissions (
    id TEXT PRIMARY KEY, harness TEXT NOT NULL, session_id TEXT NOT NULL,
    watermark TEXT NOT NULL, trigger_name TEXT, delegate_depth INTEGER NOT NULL,
    model TEXT, reasoning TEXT, system_prompt TEXT, tools_json TEXT,
    messages_json TEXT NOT NULL, received_at TEXT NOT NULL,
    context_window INTEGER, capture_json TEXT, temperature REAL, session_name TEXT,
    UNIQUE(harness, session_id, watermark)
);
CREATE TABLE reviews (
    id TEXT PRIMARY KEY, submission_id TEXT NOT NULL, harness TEXT NOT NULL,
    session_id TEXT NOT NULL, status TEXT NOT NULL,
    cancel_requested INTEGER NOT NULL DEFAULT 0, called_model TEXT,
    outcome TEXT, error TEXT, started_at TEXT, finished_at TEXT,
    context_mode TEXT, tools_extended INTEGER, cache_read_tokens INTEGER,
    input_tokens INTEGER, decision_json TEXT, usage_totals_json TEXT
);
CREATE TABLE evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT, review_id TEXT NOT NULL,
    kind TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE proposals (
    id TEXT PRIMARY KEY, review_id TEXT NOT NULL, skill_name TEXT NOT NULL,
    action TEXT NOT NULL, gist TEXT NOT NULL, payload_json TEXT NOT NULL,
    base_hash TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL, decided_at TEXT
);
CREATE TABLE skill_registry (
    name TEXT PRIMARY KEY, origin TEXT NOT NULL, pinned INTEGER NOT NULL DEFAULT 0,
    protection TEXT, created_at TEXT NOT NULL
);
CREATE TABLE model_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, review_id TEXT NOT NULL, harness TEXT,
    session_id TEXT NOT NULL, call_index INTEGER NOT NULL, model TEXT,
    payload_json TEXT NOT NULL, response_json TEXT, created_at TEXT NOT NULL,
    evidence_kind TEXT NOT NULL DEFAULT 'legacy_pre_serialization', call_status TEXT,
    wire_body TEXT, endpoint TEXT, account_scope TEXT, request_headers_json TEXT,
    response_headers_json TEXT, http_status INTEGER, raw_response TEXT,
    response_id TEXT, duration_seconds REAL, finished_at TEXT
);
