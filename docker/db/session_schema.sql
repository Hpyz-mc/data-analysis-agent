-- 在 agent_db 中以有建表权限的管理员账号执行一次。
-- 只创建 Agent 会话表，不修改销售或其他业务表。

CREATE TABLE IF NOT EXISTS public.analysis_sessions (
    id uuid PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.analysis_turns (
    turn_no bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES public.analysis_sessions(id),
    question text NOT NULL CHECK (char_length(question) BETWEEN 2 AND 2000),
    answer text,
    status text NOT NULL CHECK (
        status IN ('running', 'completed', 'incomplete', 'failed', 'interrupted')
    ),
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);

CREATE INDEX IF NOT EXISTS analysis_turns_session_order_idx
    ON public.analysis_turns (session_id, turn_no DESC);

-- Incremental upgrades preserve existing sessions and their turns.
ALTER TABLE public.analysis_sessions
    ADD COLUMN IF NOT EXISTS analysis_context jsonb;
ALTER TABLE public.analysis_turns
    ADD COLUMN IF NOT EXISTS artifacts jsonb NOT NULL DEFAULT '{}'::jsonb;

-- 另用管理员创建专用会话账号后，将下面的角色名换成实际账号再授权：
-- GRANT CONNECT ON DATABASE agent_db TO agent_session_writer;
-- GRANT USAGE ON SCHEMA public TO agent_session_writer;
-- GRANT SELECT, INSERT, UPDATE ON public.analysis_sessions TO agent_session_writer;
-- GRANT SELECT, INSERT, UPDATE ON public.analysis_turns TO agent_session_writer;
