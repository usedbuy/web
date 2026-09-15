#!/usr/bin/env python3
"""
upload_to_github_pages.py

Uploads all files from a local folder to a GitHub repository (e.g. a
GitHub Pages site) using the GitHub REST API and a personal access token.

For each local file, the script:
  1. Computes its path relative to the source folder (this becomes the
     path in the repo).
  2. Checks whether a file already exists at that path in the target
     branch (needed because the GitHub API requires the existing file's
     SHA to update it).
  3. Creates the file, or updates it if the content has changed.

--------------------------------------------------------------------
SETUP
--------------------------------------------------------------------
1. Install the only dependency:
       pip install requests

2. Create a GitHub Personal Access Token (PAT):
   - Fine-grained token (recommended): Settings -> Developer settings ->
     Personal access tokens -> Fine-grained tokens. Grant it
     "Contents: Read and write" access on the target repo.
   - Classic token: grant it the "repo" scope.

3. Provide the token to the script via the GITHUB_TOKEN environment
   variable (recommended, keeps it out of shell history) or --token.

       export GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx

4. Make sure the target repo exists and, if you want GitHub Pages to
   serve it, that Pages is enabled for the branch you push to
   (Settings -> Pages in the repo).

--------------------------------------------------------------------
USAGE EXAMPLES
--------------------------------------------------------------------
# Upload everything in ./site to the gh-pages branch of you/your-repo
python upload_to_github_pages.py \\
    --folder ./site \\
    --owner your-github-username \\
    --repo your-repo \\
    --branch gh-pages

# Dry run first, to see what would happen without changing anything
python upload_to_github_pages.py --folder ./site --owner me --repo my-site --dry-run

# Only upload .html/.css/.js/image files, skip everything else
python upload_to_github_pages.py --folder ./site --owner me --repo my-site \\
    --extensions .html .css .js .png .jpg .svg
"""

import argparse
import base64
import hashlib
import os
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit(
        "The 'requests' package is required. Install it with:\n"
        "    pip install requests"
    )

GITHUB_API = "https://api.github.com"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Upload a local folder of files to a GitHub repo via the API."
    )
    parser.add_argument("--folder", required=True, help="Local folder to upload (e.g. ./site)")
    parser.add_argument("--owner", required=True, help="GitHub username or org that owns the repo")
    parser.add_argument("--repo", required=True, help="Repository name")
    parser.add_argument(
        "--branch",
        default="gh-pages",
        help="Branch to push to (default: gh-pages). Use 'main' if your Pages "
        "site is configured to serve from main.",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("GITHUB_TOKEN"),
        help="GitHub personal access token. Defaults to the GITHUB_TOKEN "
        "environment variable if not given.",
    )
    parser.add_argument(
        "--commit-message",
        default="Update site content",
        help="Commit message to use for each changed file.",
    )
    parser.add_argument(
        "--extensions",
        nargs="*",
        default=None,
        help="Optional list of file extensions to include (e.g. .html .css .js). "
        "If omitted, all files are uploaded.",
    )
    parser.add_argument(
        "--exclude",
        nargs="*",
        default=[".git", ".DS_Store", "__pycache__"],
        help="Names of files/folders to skip.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be uploaded without making any API calls that change data.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="Seconds to wait between API requests (helps avoid rate limits on large sites).",
    )
    parser.add_argument(
        "--target-dir",
        default="",
        help="Optional folder prefix inside the repo to upload everything under, "
        "e.g. 'landing-pages' -> files end up at landing-pages/... instead of the repo root.",
    )
    parser.add_argument(
        "--page-per-file",
        action="store_true",
        help="Put every HTML file into its own folder named after the file, renamed to "
        "index.html. E.g. promo.html -> promo/index.html, and "
        "specials/summer-sale.html -> specials/summer-sale/index.html. "
        "Non-HTML files (shared css/js/images) keep their normal relative path. "
        "Use this when you have a flat pile of landing pages that each need to be "
        "served at their own URL (GitHub Pages requires each page to be named index.html "
        "inside its own folder to get a clean URL like yoursite.com/promo/).",
    )
    return parser.parse_args()


def should_skip(path: Path, exclude_names) -> bool:
    return any(part in exclude_names for part in path.parts)


def iter_local_files(folder: Path, extensions, exclude_names):
    for path in sorted(folder.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(folder)
        if should_skip(rel, exclude_names):
            continue
        if extensions and path.suffix.lower() not in extensions:
            continue
        yield path, rel


def compute_repo_path(rel_path: Path, target_dir: str, page_per_file: bool) -> str:
    """Work out where a local file should live in the repo, applying
    --target-dir and --page-per-file transformations."""
    if page_per_file and rel_path.suffix.lower() in (".html", ".htm") and rel_path.name.lower() != "index.html":
        # e.g. "specials/summer-sale.html" -> "specials/summer-sale/index.html"
        # e.g. "promo.html"                -> "promo/index.html"
        new_rel = rel_path.parent / rel_path.stem / "index.html"
    else:
        new_rel = rel_path

    if target_dir:
        new_rel = Path(target_dir) / new_rel

    return new_rel.as_posix()


def get_remote_file_sha(session, owner, repo, branch, repo_path):
    """Return the SHA of the existing file at repo_path on branch, or None if it doesn't exist."""
    url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{repo_path}"
    resp = session.get(url, params={"ref": branch})
    if resp.status_code == 200:
        return resp.json().get("sha")
    if resp.status_code == 404:
        return None
    resp.raise_for_status()


def local_sha_matches_remote(local_bytes: bytes, remote_sha: str) -> bool:
    """GitHub's blob SHA is git's blob hash: sha1('blob <len>\\0' + content)."""
    header = f"blob {len(local_bytes)}\0".encode()
    computed = hashlib.sha1(header + local_bytes).hexdigest()
    return computed == remote_sha


def upload_file(session, owner, repo, branch, repo_path, local_path, commit_message, dry_run):
    content_bytes = local_path.read_bytes()
    remote_sha = get_remote_file_sha(session, owner, repo, branch, repo_path)

    if remote_sha and local_sha_matches_remote(content_bytes, remote_sha):
        print(f"  [unchanged] {repo_path}")
        return "unchanged"

    action = "update" if remote_sha else "create"
    if dry_run:
        print(f"  [dry-run:{action}] {repo_path}")
        return action

    url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{repo_path}"
    payload = {
        "message": f"{commit_message}: {repo_path}",
        "content": base64.b64encode(content_bytes).decode("utf-8"),
        "branch": branch,
    }
    if remote_sha:
        payload["sha"] = remote_sha

    resp = session.put(url, json=payload)
    if resp.status_code in (200, 201):
        print(f"  [{action}] {repo_path}")
        return action
    else:
        print(f"  [FAILED] {repo_path}: {resp.status_code} {resp.text}")
        return "failed"


def main():
    args = parse_args()

    if not args.token:
        sys.exit(
            "No GitHub token found. Set the GITHUB_TOKEN environment variable "
            "or pass --token YOUR_TOKEN."
        )

    folder = Path(args.folder).expanduser().resolve()
    if not folder.is_dir():
        sys.exit(f"Folder not found: {folder}")

    extensions = None
    if args.extensions:
        extensions = {e if e.startswith(".") else f".{e}" for e in args.extensions}

    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {args.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
    )

    # Sanity check: confirm repo + branch are reachable before doing any work.
    check_url = f"{GITHUB_API}/repos/{args.owner}/{args.repo}"
    check = session.get(check_url)
    if check.status_code == 404:
        sys.exit(f"Repository not found or token lacks access: {args.owner}/{args.repo}")
    check.raise_for_status()

    files = list(iter_local_files(folder, extensions, set(args.exclude)))
    if not files:
        sys.exit(f"No matching files found in {folder}")

    print(f"Found {len(files)} file(s) to process in {folder}")
    print(f"Target: {args.owner}/{args.repo}@{args.branch}")
    if args.target_dir:
        print(f"Target directory prefix: {args.target_dir}/")
    if args.page_per_file:
        print("Mode: each HTML file will be placed in its own folder as index.html")
    if args.dry_run:
        print("Running in --dry-run mode: no changes will be made.\n")
    else:
        print()

    results = {"create": 0, "update": 0, "unchanged": 0, "failed": 0, "dry-run:create": 0, "dry-run:update": 0}
    for local_path, rel_path in files:
        repo_path = compute_repo_path(rel_path, args.target_dir, args.page_per_file)
        result = upload_file(
            session, args.owner, args.repo, args.branch, repo_path, local_path,
            args.commit_message, args.dry_run,
        )
        results[result] = results.get(result, 0) + 1
        if args.delay:
            time.sleep(args.delay)

    print("\nSummary:")
    for key, count in results.items():
        if count:
            print(f"  {key}: {count}")

    if results.get("failed"):
        sys.exit(1)


if __name__ == "__main__":
    main()
