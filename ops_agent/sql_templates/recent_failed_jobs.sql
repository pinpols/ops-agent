select id, status, updated_at, error_message
from batch.job_instance
where status in ('FAILED', 'ERROR')
order by updated_at desc
