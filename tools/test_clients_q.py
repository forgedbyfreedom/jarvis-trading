import sys
sys.path.insert(0, r"C:\jarvis-trading")
import hub
d = hub.coach_ask("Who's slipping on adherence this week?", [])
print(d.get("seconds"), d.get("sources"))
print(d.get("answer", d)[:500])
