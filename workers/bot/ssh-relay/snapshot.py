"""Read a consistent, read-only Oracle snapshot as SQL; never log its contents."""
import sqlite3
import sys


def export(path, output):
    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    snapshot = sqlite3.connect(":memory:")
    try:
        source.backup(snapshot)
        if snapshot.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("snapshot integrity check failed")
        for line in snapshot.iterdump():
            output.write(line + "\n")
    finally:
        snapshot.close()
        source.close()


if __name__ == "__main__":
    export(sys.argv[1], sys.stdout)
