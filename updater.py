"""Update check against GitHub.

Two channels, because the two audiences install differently:

* **stable** (``main``) — compared against the latest published release, so it
  works for the majority who install by downloading a zip.
* **dev** — compared against the branch tip, which needs the local commit id and
  therefore a real ``git clone``.  A dev build unpacked from a zip has no ``.git``
  and reports ``unavailable``; there is nothing to compare it to.

Nothing here is specific to EDFCA — ``owner``, ``repo``, ``plugin_dir`` and
``current_version`` are all parameters — so the module can be lifted into another
plugin by copying the file.  It is kept inside this repo rather than shared as a
git submodule because GitHub's "Download ZIP" omits submodule contents, which
would leave zip installs with an empty directory and a broken import.

Every entry point is non-raising: a failed check degrades to ``unavailable``.
"""

import os
import re
from dataclasses import dataclass
from typing import Optional

import requests

from _logger import logger

_API = "https://api.github.com/repos/{owner}/{repo}"
_WEB = "https://github.com/{owner}/{repo}"
_TIMEOUT = 10
_DEFAULT_UA = "edmc-plugin-update-check"

_UNAVAILABLE_TEXT = "Update check unavailable"


@dataclass(frozen=True)
class UpdateStatus:
    """Outcome of a check.

    ``text`` is a short, glyph-free line for the caller to render — choosing the
    icon is the UI's business, not ours.  ``detail`` is the longer reason, meant
    for the log.  ``url`` is what to open when the user clicks.
    """

    state: str          # "up_to_date" | "update_available" | "unavailable"
    text: str
    url: str = ""
    detail: str = ""

    @property
    def update_available(self) -> bool:
        return self.state == "update_available"


# ── local git inspection ─────────────────────────────────────────────────────

def _git_dir(plugin_dir: str) -> Optional[str]:
    """Locate the ``.git`` directory for ``plugin_dir``, or None if unpacked.

    ``.git`` is a plain file holding a ``gitdir: …`` pointer when the plugin sits
    in a linked worktree or a submodule.
    """
    dot_git = os.path.join(plugin_dir, ".git")
    if os.path.isdir(dot_git):
        return dot_git
    if os.path.isfile(dot_git):
        try:
            with open(dot_git, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read().strip()
        except OSError:
            logger.exception("Could not read %s", dot_git)
            return None
        if content.startswith("gitdir:"):
            target = content[len("gitdir:"):].strip()
            if not os.path.isabs(target):
                target = os.path.join(plugin_dir, target)
            target = os.path.normpath(target)
            if os.path.isdir(target):
                return target
    return None


def _refs_dir(git_dir: str) -> str:
    """Directory the refs live in — not ``git_dir`` inside a linked worktree,
    where a ``commondir`` file points back at the main repository."""
    commondir = os.path.join(git_dir, "commondir")
    if os.path.isfile(commondir):
        try:
            with open(commondir, "r", encoding="utf-8", errors="replace") as fh:
                rel = fh.read().strip()
        except OSError:
            logger.exception("Could not read %s", commondir)
            return git_dir
        if rel:
            return os.path.normpath(os.path.join(git_dir, rel))
    return git_dir


def _read_head(git_dir: str) -> str:
    try:
        with open(os.path.join(git_dir, "HEAD"), "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        logger.exception("Could not read HEAD in %s", git_dir)
        return ""


def detect_branch(
    plugin_dir: str,
    fallback_version: str,
    dev_branch: str = "dev",
    stable_branch: str = "main",
) -> Optional[str]:
    """Branch the plugin was installed from.

    A checkout is authoritative — checking out a branch swaps the version file
    too, so the two always agree.  Without ``.git`` the baked-in version string is
    all we have: a ``-dev`` suffix means the dev channel.

    Returns None for a detached HEAD, which belongs to no channel.
    """
    git_dir = _git_dir(plugin_dir)
    if git_dir is None:
        return dev_branch if fallback_version.strip().endswith("-dev") else stable_branch

    head = _read_head(git_dir)
    if head.startswith("ref: refs/heads/"):
        return head[len("ref: refs/heads/"):].strip() or None
    return None


def local_head_sha(plugin_dir: str) -> Optional[str]:
    """Commit id of the installed checkout, or None if it isn't one."""
    git_dir = _git_dir(plugin_dir)
    if git_dir is None:
        return None

    head = _read_head(git_dir)
    if not head:
        return None
    if not head.startswith("ref: "):
        # Detached HEAD stores the commit id directly.
        return head if re.fullmatch(r"[0-9a-f]{40}", head) else None

    ref = head[len("ref: "):].strip()
    refs_dir = _refs_dir(git_dir)

    loose = os.path.join(refs_dir, *ref.split("/"))
    if os.path.isfile(loose):
        try:
            with open(loose, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read().strip() or None
        except OSError:
            logger.exception("Could not read ref %s", loose)

    # A freshly cloned repo keeps its refs packed, with no loose file per branch.
    packed = os.path.join(refs_dir, "packed-refs")
    try:
        with open(packed, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith(("#", "^")):
                    continue
                parts = line.split()
                if len(parts) == 2 and parts[1] == ref:
                    return parts[0]
    except OSError:
        pass

    logger.warning("Could not resolve %s to a commit id", ref)
    return None


# ── version comparison ───────────────────────────────────────────────────────

def _version_tuple(value: str) -> Optional[tuple]:
    """``"v1.2.3-dev"`` → ``(1, 2, 3)``, or None if there is no version in it.

    The first dotted run of digits *anywhere* in the string wins, so this copes
    with whatever tag style a repo uses — ``v1.2.3``, ``1.2.3-rc1``,
    ``Release/1.2.3``.  Missing components are zero, so ``"1.1"`` and ``"1.1.0"``
    compare equal.  Returning None rather than a zero tuple matters: a tag we
    can't read must be reported, not silently treated as older than everything.
    """
    match = re.search(r"\d+(?:\.\d+)*", value)
    if match is None:
        return None
    parts = [int(p) for p in match.group(0).split(".")]
    return tuple((parts + [0, 0, 0])[:3])


# ── GitHub queries ───────────────────────────────────────────────────────────

def _get(url: str, user_agent: str) -> requests.Response:
    return requests.get(
        url,
        headers={
            # GitHub rejects API requests that send no User-Agent.
            "User-Agent": user_agent or _DEFAULT_UA,
            "Accept": "application/vnd.github+json",
        },
        timeout=_TIMEOUT,
    )


def _unavailable(detail: str, url: str = "") -> UpdateStatus:
    logger.info("Update check unavailable: %s", detail)
    return UpdateStatus("unavailable", _UNAVAILABLE_TEXT, url, detail)


def _check_release(api: str, web: str, current_version: str, user_agent: str) -> UpdateStatus:
    """Compare against the latest published release."""
    releases_page = f"{web}/releases"
    resp = _get(f"{api}/releases/latest", user_agent)

    if resp.status_code == 404:
        return _unavailable("no releases published yet", releases_page)
    if resp.status_code >= 400:
        return _unavailable(f"releases API returned HTTP {resp.status_code}", releases_page)

    data = resp.json()
    tag = str(data.get("tag_name") or "").strip()
    if not tag:
        return _unavailable("latest release has no tag name", releases_page)

    latest = _version_tuple(tag)
    running = _version_tuple(current_version)
    if latest is None:
        return _unavailable(f"could not read a version from release tag {tag!r}", releases_page)
    if running is None:
        return _unavailable(f"could not read a version from {current_version!r}", releases_page)

    if latest > running:
        shown = ".".join(str(p) for p in latest)
        url = str(data.get("html_url") or releases_page)
        logger.info("Update available: v%s (running v%s)", shown, current_version)
        return UpdateStatus("update_available", f"Update available: v{shown}", url)

    logger.info("Up to date: v%s (latest release %s)", current_version, tag)
    return UpdateStatus("up_to_date", f"Up to date (v{current_version})")


def _check_dev_branch(
    api: str, web: str, plugin_dir: str, dev_branch: str, user_agent: str,
) -> UpdateStatus:
    """Compare the local commit against the tip of the dev branch."""
    branch_page = f"{web}/commits/{dev_branch}"

    sha = local_head_sha(plugin_dir)
    if sha is None:
        return _unavailable("could not read the local commit id", branch_page)

    resp = _get(f"{api}/compare/{sha}...{dev_branch}", user_agent)
    if resp.status_code == 404:
        # Our commit isn't on GitHub — unpushed local work, or a rewritten
        # history.  There is no meaningful "newer" to report.
        return _unavailable(f"local commit {sha[:7]} is not on GitHub", branch_page)

    if resp.status_code < 400:
        ahead = int(resp.json().get("ahead_by") or 0)
        if ahead > 0:
            logger.info("Update available: %d new commit(s) on %s", ahead, dev_branch)
            return UpdateStatus(
                "update_available",
                f"Update available: {ahead} new commit{'' if ahead == 1 else 's'} on {dev_branch}",
                branch_page,
            )
        logger.info("Up to date with %s (%s)", dev_branch, sha[:7])
        return UpdateStatus("up_to_date", f"Up to date ({dev_branch} {sha[:7]})")

    # Compare is the informative endpoint; a bare tip lookup is the fallback.
    logger.info("Compare API returned HTTP %s — falling back to a commit id check",
                resp.status_code)
    resp = _get(f"{api}/commits/{dev_branch}", user_agent)
    if resp.status_code >= 400:
        return _unavailable(f"commits API returned HTTP {resp.status_code}", branch_page)

    remote = str(resp.json().get("sha") or "")
    if not remote:
        return _unavailable(f"{dev_branch} tip has no commit id", branch_page)
    if remote != sha:
        return UpdateStatus("update_available", f"Update available on {dev_branch}", branch_page)
    return UpdateStatus("up_to_date", f"Up to date ({dev_branch} {sha[:7]})")


def check_for_update(
    plugin_dir: str,
    current_version: str,
    owner: str,
    repo: str,
    dev_branch: str = "dev",
    stable_branch: str = "main",
    user_agent: str = "",
) -> UpdateStatus:
    """Check for a newer version and describe the result.

    Safe to call from a worker thread; performs network I/O and never raises.
    """
    api = _API.format(owner=owner, repo=repo)
    web = _WEB.format(owner=owner, repo=repo)

    try:
        branch = detect_branch(plugin_dir, current_version, dev_branch, stable_branch)

        if branch == stable_branch:
            return _check_release(api, web, current_version, user_agent)

        if branch == dev_branch:
            if _git_dir(plugin_dir) is None:
                return _unavailable(
                    "dev builds installed from a zip cannot be checked — "
                    "install with git clone to enable update checks",
                    f"{web}/commits/{dev_branch}",
                )
            return _check_dev_branch(api, web, plugin_dir, dev_branch, user_agent)

        return _unavailable(f"no update channel for branch {branch!r}", web)

    except requests.RequestException as exc:
        return _unavailable(f"could not reach GitHub: {exc}", web)
    except Exception:
        logger.exception("Update check failed")
        return UpdateStatus("unavailable", _UNAVAILABLE_TEXT, web, "update check failed")
