-- Run after 01_roles.sh and 02_session_schema.sql in the isolated Docker DB.
GRANT CONNECT ON DATABASE agent_db TO agent_reader, agent_session_writer;
GRANT USAGE ON SCHEMA public TO agent_reader, agent_session_writer;
-- Explicitly authorized for the platform writer to create versioned import tables.
GRANT CREATE ON SCHEMA public TO agent_session_writer;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.analysis_sessions TO agent_session_writer;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.analysis_turns TO agent_session_writer;

-- No business-table SELECT grant is automatic. The optional seed step grants
-- agent_reader access only to public.agent_sales_sample.
