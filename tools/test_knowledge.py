import sys
sys.path.insert(0, r"C:\jarvis-trading")
import hub
for q in ["How many exercises are in the YARBRO Physique Pro movement library, and how is it organised?",
          "How does the body scan in the FBF app measure body fat?"]:
    d = hub.coach_ask(q, [])
    print("==", q, "\n", d.get("answer", d)[:500], "\n sources:", d.get("sources", [])[:4], d.get("seconds"), "s\n")
