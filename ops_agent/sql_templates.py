"""Approved read-only SQL templates."""

SQL_TEMPLATES: dict[str, str] = {
    "pg_lock_waits": """
        select pid, usename, wait_event_type, wait_event, state, query
        from pg_stat_activity
        where wait_event_type = 'Lock'
        order by query_start nulls last
    """,
    "active_queries": """
        select pid, usename, state, wait_event_type, wait_event, query
        from pg_stat_activity
        where state <> 'idle'
        order by query_start nulls last
    """,
    "job_status_counts": """
        select status, count(*) as count
        from batch.job_instance
        group by status
        order by count desc
    """,
    "recent_failed_jobs": """
        select id, status, updated_at, error_message
        from batch.job_instance
        where status in ('FAILED', 'ERROR')
        order by updated_at desc
    """,
}


def list_sql_templates() -> str:
    return "\n".join(f"- {name}" for name in sorted(SQL_TEMPLATES))
