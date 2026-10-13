"""Generate a read-only release manifest from an exact local Git commit.

Run from any directory. Write the manifest outside tracked source (for example
data/release_candidate_inventory.json) to avoid a self-referential commit hash.
No provider, Cloudflare, or GitHub requests are made.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
BASE = "e11a42a445af36907e6c2dbb9b7a4a5e81fd8fa9"


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    output = Path(args.out).resolve()
    tracked = git("ls-files", "-z").decode().split("\0")[:-1]
    if output in {(ROOT / name).resolve() for name in tracked}:
        parser.error("output must not replace a tracked file")
    sha = git("rev-parse", "HEAD").decode().strip()
    subprocess.run(["git", "merge-base", "--is-ancestor", BASE, sha], cwd=ROOT, check=True)
    files = {}
    for name in tracked:
        committed = git("show", f"{sha}:{name}")
        local = (ROOT / name).read_bytes()
        files[name] = {
            "commit_sha256": hashlib.sha256(committed).hexdigest(),
            "working_file_sha256": hashlib.sha256(local).hexdigest(),
            "equal_after_line_ending_normalization": (
                committed.replace(b"\r\n", b"\n") == local.replace(b"\r\n", b"\n")),
        }
    status = subprocess.run(["git", "status", "--porcelain=v1", "--untracked-files=all"],
                            cwd=ROOT, capture_output=True, text=True, check=True)
    report = {
        "release_commit": sha,
        "branch": git("branch", "--show-current").decode().strip(),
        "required_base": BASE,
        "tree": git("rev-parse", "HEAD^{tree}").decode().strip(),
        "release_commits": git("log", "--format=%H %s", f"{BASE}..HEAD").decode().splitlines(),
        "changed_files": git("diff", "--name-status", BASE, sha).decode().splitlines(),
        "files": files,
        "working_tree_status": status.stdout.splitlines(),
        "inventory_warnings": status.stderr.splitlines(),
        "manifest_is_untracked_evidence": True,
        "ci_results": "Not run; no GitHub Actions run or clean Python 3.11/pytest 8 result claimed.",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Release {sha}; {len(files)} files; manifest: {output}")


if __name__ == "__main__":
    main()
