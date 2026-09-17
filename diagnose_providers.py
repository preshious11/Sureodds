    for f in root.rglob("*.py"):
        try:
            t = f.read_text(errors="ignore")
        except Exception as e:
            print("ERR", f, e, file=sys.stderr)
            continue
        if PAT.search(t):
            hits.append(str(f))
    out = "\n".join(hits) if hits else "NO_HITS"
    print(out)
    sys.exit(0)
