select status, count(*) as count
from batch.job_instance
group by status
order by count desc
