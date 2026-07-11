-- Schema and representative rows produced by the v0.7.3 SQLAlchemy models.
-- This fixture intentionally has no alembic_version or session_input table.
PRAGMA foreign_keys=ON;
BEGIN;

CREATE TABLE project (
    id VARCHAR NOT NULL,
    name VARCHAR NOT NULL,
    worktree VARCHAR NOT NULL,
    time_created DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    time_updated DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id)
);

CREATE TABLE scheduled_task (
    id VARCHAR NOT NULL,
    name VARCHAR NOT NULL,
    description VARCHAR NOT NULL,
    prompt VARCHAR NOT NULL,
    schedule_config JSON NOT NULL,
    agent VARCHAR NOT NULL,
    model VARCHAR,
    workspace VARCHAR,
    enabled BOOLEAN NOT NULL,
    template_id VARCHAR,
    last_run_at DATETIME,
    last_run_status VARCHAR,
    last_session_id VARCHAR,
    next_run_at DATETIME,
    run_count INTEGER NOT NULL,
    timeout_seconds INTEGER NOT NULL,
    loop_max_iterations INTEGER,
    loop_preset VARCHAR,
    loop_stop_marker VARCHAR,
    time_created DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    time_updated DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id)
);
CREATE INDEX ix_scheduled_task_next_run_at ON scheduled_task (next_run_at);

CREATE TABLE workspace_memory (
    id VARCHAR NOT NULL,
    workspace_path VARCHAR NOT NULL,
    content TEXT NOT NULL,
    time_created DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    time_updated DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (workspace_path)
);
CREATE UNIQUE INDEX ix_workspace_memory_path ON workspace_memory (workspace_path);

CREATE TABLE session (
    id VARCHAR NOT NULL,
    project_id VARCHAR,
    parent_id VARCHAR,
    slug VARCHAR NOT NULL,
    directory VARCHAR NOT NULL,
    title VARCHAR NOT NULL,
    version VARCHAR NOT NULL,
    model_id VARCHAR,
    provider_id VARCHAR,
    summary_additions INTEGER,
    summary_deletions INTEGER,
    summary_files INTEGER,
    summary_diffs JSON,
    is_pinned BOOLEAN DEFAULT '0' NOT NULL,
    permission JSON,
    time_compacting DATETIME,
    time_archived DATETIME,
    time_created DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    time_updated DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(project_id) REFERENCES project (id) ON DELETE CASCADE
);

CREATE TABLE message (
    id VARCHAR NOT NULL,
    session_id VARCHAR NOT NULL,
    data JSON NOT NULL,
    time_created DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    time_updated DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(session_id) REFERENCES session (id) ON DELETE CASCADE
);

CREATE TABLE part (
    id VARCHAR NOT NULL,
    message_id VARCHAR NOT NULL,
    session_id VARCHAR NOT NULL,
    data JSON NOT NULL,
    time_created DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    time_updated DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(message_id) REFERENCES message (id) ON DELETE CASCADE
);
CREATE INDEX ix_part_session_id ON part (session_id);

CREATE TABLE todo (
    id VARCHAR NOT NULL,
    session_id VARCHAR NOT NULL,
    content VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    active_form VARCHAR NOT NULL,
    position INTEGER NOT NULL,
    time_created DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    time_updated DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(session_id) REFERENCES session (id) ON DELETE CASCADE
);

CREATE TABLE session_file (
    id VARCHAR NOT NULL,
    session_id VARCHAR NOT NULL,
    file_path VARCHAR NOT NULL,
    file_name VARCHAR NOT NULL,
    tool_id VARCHAR NOT NULL,
    file_type VARCHAR NOT NULL,
    time_created DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    time_updated DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(session_id) REFERENCES session (id) ON DELETE CASCADE
);

CREATE TABLE task_run (
    id VARCHAR NOT NULL,
    task_id VARCHAR NOT NULL,
    session_id VARCHAR,
    status VARCHAR NOT NULL,
    error_message VARCHAR,
    started_at DATETIME,
    finished_at DATETIME,
    triggered_by VARCHAR NOT NULL,
    time_created DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    time_updated DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(task_id) REFERENCES scheduled_task (id) ON DELETE CASCADE
);
CREATE INDEX ix_task_run_task_id ON task_run (task_id);

INSERT INTO project (id, name, worktree)
VALUES ('project-v073', '采访项目', 'C:\\Users\\tester\\Documents\\采访');

INSERT INTO session (
    id, project_id, slug, directory, title, version, model_id, provider_id,
    summary_additions, summary_deletions, summary_files, summary_diffs,
    is_pinned, permission
) VALUES (
    'session-v073', 'project-v073', 'legacy-chat',
    'C:\\Users\\tester\\Documents\\采访', '需要保留的 v0.7.3 对话', '0.7.3',
    'deepseek-chat', 'deepseek', 12, 3, 2,
    '{"report.md":{"additions":12,"deletions":3}}', 1,
    '[{"permission":"read","pattern":"*","action":"allow"}]'
);

INSERT INTO message (id, session_id, data)
VALUES ('message-v073', 'session-v073', '{"role":"user","agent":"build"}');

INSERT INTO part (id, message_id, session_id, data)
VALUES (
    'part-v073', 'message-v073', 'session-v073',
    '{"type":"text","text":"这条历史消息不能在升级时丢失"}'
);

INSERT INTO todo (id, session_id, content, status, active_form, position)
VALUES ('todo-v073', 'session-v073', '生成会议纪要', 'completed', '正在生成会议纪要', 0);

INSERT INTO session_file (
    id, session_id, file_path, file_name, tool_id, file_type
) VALUES (
    'file-v073', 'session-v073',
    'C:\\Users\\tester\\Documents\\采访\\report.md', 'report.md',
    'write-v073', 'generated'
);

INSERT INTO scheduled_task (
    id, name, description, prompt, schedule_config, agent, model, workspace,
    enabled, template_id, last_run_at, last_run_status, last_session_id,
    next_run_at, run_count, timeout_seconds, loop_max_iterations,
    loop_preset, loop_stop_marker
) VALUES (
    'task-v073', '每日简报', '保留自动化', '生成简报',
    '{"type":"cron","cron":"0 8 * * *"}', 'build', 'deepseek-chat',
    'C:\\Users\\tester\\Documents', 1, NULL,
    '2026-07-10 08:00:00', 'success', 'session-v073',
    '2026-07-12 08:00:00', 7, 1800, NULL, NULL, '[LOOP_DONE]'
);

INSERT INTO task_run (
    id, task_id, session_id, status, started_at, finished_at, triggered_by
) VALUES (
    'run-v073', 'task-v073', 'session-v073', 'success',
    '2026-07-10 08:00:00', '2026-07-10 08:03:00', 'schedule'
);

INSERT INTO workspace_memory (id, workspace_path, content)
VALUES (
    'memory-v073', 'C:\\Users\\tester\\Documents\\采访',
    '# 项目记忆\n\n这些内容必须保留。'
);

COMMIT;
