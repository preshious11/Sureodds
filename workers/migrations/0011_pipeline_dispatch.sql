-- WHY: keep generation retries durable across Worker restarts and overlapping cron fires.
-- due_at stores the Lagos dispatch date; attempts is that day's reserved dispatch count.
INSERT OR IGNORE INTO job_state (name) VALUES ('generation_dispatch'), ('settlement_dispatch');
