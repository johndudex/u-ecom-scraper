import django
django.setup()
from scraper.models import ScrapeJob, ToolCallLog
import json

j = ScrapeJob.objects.get(id=131)
print(f"Job {j.id}: status={j.status} site={j.site_name} platform={j.platform} method={j.scraping_method} error={j.error_message[:100] if j.error_message else None}")

logs = ToolCallLog.objects.filter(job_id=j.id).order_by("created_at")
agents = {}
for l in logs:
    agent = l.agent or "unknown"
    if agent not in agents:
        agents[agent] = {"calls": 0, "first": l.created_at, "last": l.created_at}
    agents[agent]["calls"] += 1
    agents[agent]["last"] = l.created_at

if agents:
    print(f"\nAgent tool calls ({len(logs)} total):")
    for agent, info in sorted(agents.items(), key=lambda x: x[1]["first"]):
        print(f"  {agent}: {info['calls']} calls")
else:
    print("  No tool calls yet")
