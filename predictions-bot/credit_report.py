"""Print today's and this month's credit spend per provider."""
from predbot.config import load_config
from predbot.storage.db import Database
from predbot.util.timeutil import utcnow

cfg = load_config()
db = Database(cfg.db_path)
day = utcnow().strftime("%Y-%m-%d")
month = utcnow().strftime("%Y-%m")
for r in db.query("SELECT provider, bucket, used FROM api_budget "
                  "WHERE bucket LIKE ? AND provider IN ('oddsapi','api_football','sharpapi','sportsgameodds') "
                  "ORDER BY provider, bucket", (month + "%",)):
    print(dict(r))
