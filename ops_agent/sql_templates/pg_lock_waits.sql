select pid, usename, wait_event_type, wait_event, state, query
from pg_stat_activity
where wait_event_type = 'Lock'
order by query_start nulls last
