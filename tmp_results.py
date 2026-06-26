import django
django.setup()
from scraper.models import AgentPlayground
import os

agents = [
    (85, "site_analyzer", "round4/site_analyzer/site_analysis.json"),
    (87, "nav_explore", "round4/nav_explore/navigation_findings.json"),
    (86, "product_analyzer", "round4/product_analyzer/product_analysis.json"),
    (90, "nav_synthesize", "round4-nav-synth-v2/navigation_analysis.json"),
    (89, "scraper_analyzer", "round4/scraper_analyzer/scraper_analysis.json"),
    (91, "code_writer", "calvinklein-co-uk/scraper_draft.py"),
    (93, "code_tester", "round4-code-tester/test_report.json"),
    (92, "nav_skill_review", "round4-nav-skill-review/nav_learning_report.json"),
    (94, "cleanup", "calvinklein-co-uk/cleanup_report.json"),
    (95, "skill_learner", "calvinklein-co-uk/learning_report.json"),
]

header = f"{'ID':>3} {'Agent':<20} {'Calls':>5} {'OK?':>4} {'Path'}"
print(header)
print("-" * len(header))
for pid, name, path in agents:
    p = AgentPlayground.objects.get(id=pid)
    exists = os.path.isfile(f"/app/workspace/{path}")
    tag = "YES" if exists else "NO"
    print(f"{pid:>3} {name:<20} {p.tool_call_count:>5} {tag:>4} {path}")
