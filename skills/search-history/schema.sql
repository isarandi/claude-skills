-- Claude conversation history schema

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,  -- dir name like -home-user-project
    path TEXT                    -- filesystem path like /home/user/project
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    started_at TEXT,
    ended_at TEXT,
    jsonl_path TEXT,
    jsonl_mtime REAL
);

CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES sessions(id),
    jsonl_path TEXT,
    jsonl_mtime REAL
);

CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    agent_id TEXT,
    uuid TEXT,
    type TEXT NOT NULL,
    tool_name TEXT,
    content TEXT,
    timestamp TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id),
    FOREIGN KEY (agent_id) REFERENCES agents(id)
);

CREATE INDEX IF NOT EXISTS idx_records_session ON records(session_id);
CREATE INDEX IF NOT EXISTS idx_records_agent ON records(agent_id);
CREATE INDEX IF NOT EXISTS idx_records_type ON records(type);
CREATE INDEX IF NOT EXISTS idx_records_timestamp ON records(timestamp);
CREATE INDEX IF NOT EXISTS idx_records_tool ON records(tool_name);

-- Full-text search on content
CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(
    content,
    content='records',
    content_rowid='id'
);

-- Triggers to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS records_ai AFTER INSERT ON records BEGIN
    INSERT INTO records_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS records_ad AFTER DELETE ON records BEGIN
    INSERT INTO records_fts(records_fts, rowid, content) VALUES('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS records_au AFTER UPDATE ON records BEGIN
    INSERT INTO records_fts(records_fts, rowid, content) VALUES('delete', old.id, old.content);
    INSERT INTO records_fts(rowid, content) VALUES (new.id, new.content);
END;
