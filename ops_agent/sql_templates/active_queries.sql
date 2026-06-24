select pid, usename, state, wait_event_type, wait_event, query
from pg_stat_activity
where state <> 'idle'
order by query_start nulls last
