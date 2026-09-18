"""Search for hardcoded 3.5 and default-under patterns."""
import pathlib

root = pathlib.Path("c:/Projects/Sureodds/predictions-bot/predbot")

print("=" * 60)
print("FILES CONTAINING '3.5'")
print("=" * 60)
for p in sorted(root.rglob("*.py")):
    try:
        text = p.read_text(errors="replace")
    except OSError:
        continue
    for i, line in enumerate(text.splitlines(), 1):
        if "3.5" in line and not line.strip().startswith("#"):
            print(f"{p.relative_to(root)}:{i}: {line.strip()}")

print()
print("=" * 60)
print("FILES CONTAINING 'under' near 'default/fallback/unknown'")
print("=" * 60)
for p in sorted(root.rglob("*.py")):
    try:
        text = p.read_text(errors="replace")
    except OSError:
        continue
    for i, line in enumerate(text.splitlines(), 1):
        ll = line.lower()
        if "under" in ll and any(kw in ll for kw in ["default", "fallback", "unknown", "else:", "except", "missing"]):
            print(f"{p.relative_to(root)}:{i}: {line.strip()}")
