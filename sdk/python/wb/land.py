"""wb land: move projects' lines in the Set, the only route to a set-named branch.

    wb land                          HEAD of the worktree you stand in (or of every member of your Work)
    wb land cubature=<ref> ...       named projects at refs of their own repositories
    wb land --add cubature=<url>     a project's first line, with the url a public repository carries
    wb land ... --tag v1.2           also tag each landed commit on its remote, as part of the publish
    wb land a=<ref> b=<ref> --only a land only a of what was asked
    wb land --dry                    validate and compose only: nothing is published

What happens, in order:

1. Any unfinished landing is finished (its Set commit exists: its branches are fast-forwarded, no check runs) or
   cleaned up (it stopped before the Set moved: its landing refs are deleted).
2. Each project's remote is fetched; a commit that does not descend from its line is refused with the git command
   that fixes it. The head of the Set is exported for the job.
3. Each commit's gate checks run with ci.run (a pass already recorded stands), outside the lock, producers first: a
   project that uses another one landing with it runs against the would-be set holding that one's new line.
4. set.reach composes the would-be set and names every consumer the landing reaches (its [uses] names a project
   whose files changed, its tools read a moved project's tree, it uses a moved third entry, or it builds with
   {gradle} and the platform moved); each one with no pass under its new key runs its gate checks against that set.
5. set.land takes lock set, checks every pass again at the head, applies the Set gate (no private URL, no launch with
   two builds of one mod id), composes set.json and its commit body, scrubs them with every ref name, and writes the
   landing record.
6. Here, with the caller's own git: each commit is pushed to refs/landed/<id> on its remote; the Set commit is made
   with plumbing in the Set's bare home and becomes the head (pushed fast-forward to the Set's remote when it has
   one; this is the commit point); each set-named branch is fast-forwarded and tagged when --tag names a tag; the
   landing refs are deleted.

A failure before the Set moves leaves every branch as it was. A failure after it leaves 'landed, branch behind',
which the next wb land (or wb work sync) finishes from the record."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .workspace import GIT_ENV, Workspace, WorkspaceError, git


SET = "set"  # the Set's own repository, as a ci project


def _say(text: str):
    print(f"wb land: {text}", file=sys.stderr)


class Lander:
    def __init__(self, workspace: Workspace, client, say=_say):
        self.workspace = workspace
        self.client = client
        self.say = say
        configured = str((workspace.values.get("workspace") or {}).get("set") or "")
        if not configured:
            raise WorkspaceError("no Set on this machine: set [workspace] set to the Set's bare home")
        self.home = Path(configured)
        if not (self.home / "HEAD").is_file():
            raise WorkspaceError(f"{self.home} is not a bare repository: [workspace] set names the Set's bare home")
        store = workspace.store_root()
        if store is None:
            raise WorkspaceError("no Store: set [paths] out (the Store is <out>/store)")
        self.store = store
        self.sets = self.store.parent / "sets"

    # -- the Set ------------------------------------------------------------------------------------------------
    def fetch(self):
        """Brings the bare home's main to the Set's remote: a landing made elsewhere is built on, never raced. A
        home that has commits the remote lacks, or that diverged from it, is refused with what to do."""
        if not self._remote():
            return
        done = subprocess.run(["git", "fetch", "--quiet", "origin", "+refs/heads/main:refs/remotes/origin/main"],
                              cwd=self.home, capture_output=True, text=True)
        if done.returncode != 0:
            raise WorkspaceError(f"cannot fetch the Set's remote, so nothing can land: {done.stderr.strip()[-300:]}")
        local = git(self.home, "rev-parse", "--verify", "refs/heads/main")
        remote = git(self.home, "rev-parse", "--verify", "refs/remotes/origin/main")
        if local == remote:
            return
        def ancestor(a, b):
            return subprocess.run(["git", "merge-base", "--is-ancestor", a, b], cwd=self.home).returncode == 0
        if ancestor(local, remote):
            git(self.home, "update-ref", "refs/heads/main", remote, local)
            self.say(f"the Set moved elsewhere: {local[:12]} -> {remote[:12]}")
        elif ancestor(remote, local):
            raise WorkspaceError(f"the Set's home has commits its remote lacks ({remote[:12]}..{local[:12]}): a "
                                 "landing stopped before its push; push them with git -C <home> push origin main")
        else:
            raise WorkspaceError(f"the Set's home ({local[:12]}) and its remote ({remote[:12]}) have diverged; "
                                 "nothing lands until one of them is chosen")

    def head(self) -> tuple[str, str]:
        from wb_core import setfile
        revision = git(self.home, "rev-parse", "--verify", "refs/heads/main")
        text = git(self.home, "show", f"{revision}:set.json") + "\n"
        setfile.parse(text)
        return revision, text

    def export(self, revision: str, text: str, head: bool = False) -> Path:
        from wb_core import setfile
        path = setfile.export(self.sets, revision, text)
        if head:
            setfile.write_head(self.sets, revision)
        return path

    def _remote(self) -> bool:
        return "origin" in git(self.home, "remote").split()

    # -- reconcile ----------------------------------------------------------------------------------------------
    def reconcile(self) -> list[str]:
        """Finishes or cleans up each landing a publish left open whose job has ended."""
        from wb_core import landing as landings
        done = []
        for record in landings.unfinished(self.store):
            if not self._ended(record):
                continue
            if record["state"] == "set-pushed":
                self.say(f"finishing landing {record['id']}: its Set commit exists, so its branches move now")
                self.finish(record)
                done.append(f"{record['id']} finished")
            else:
                self.say(f"cleaning up landing {record['id']}: it stopped before the Set moved")
                self._drop_refs(record)
                landings.move(self.store, record["id"], "abandoned", why=record.get("why") or "its publish stopped",
                              refs_dropped=True)
                done.append(f"{record['id']} cleaned up")
        for record in landings.with_refs(self.store):
            self._drop_refs(record)
            landings.move(self.store, record["id"], "abandoned", refs_dropped=True)
            done.append(f"{record['id']} landing refs removed")
        return done

    def _ended(self, record: dict) -> bool:
        if time.time() > record.get("deadline", 0):
            return True
        try:
            return self.client.job(record["job"]).refresh().state in ("settled", "cancelled", "interrupted")
        except Exception:  # an unknown job cannot be holding the publish
            return True

    def _repo(self, name: str) -> Path:
        project = self.workspace.projects().get(name)
        if project is None or not project.repo.is_dir():
            raise WorkspaceError(f"{name} is not a project here: add it under [modules.ci.projects]")
        return project.repo

    def _drop_refs(self, record: dict):
        for name in record.get("projects") or {}:
            subprocess.run(["git", "push", "--quiet", "origin", f":refs/landed/{record['id']}"], cwd=self._repo(name),
                           capture_output=True, env={**os.environ, **GIT_ENV})

    # -- landing ------------------------------------------------------------------------------------------------
    def land(self, targets: dict[str, str], *, urls: dict[str, str] | None = None, branch: str | None = None,
             deadline: float = 600, dry: bool = False, stop_after: str | None = None, holds: dict | None = None,
             drop_holds: list | None = None, third: dict | None = None, platform: dict | None = None,
             tag: str | None = None, only: list[str] | None = None, doctor_exempt: dict | None = None,
             doctor_unexempt: list | None = None) -> dict:
        from wb_core import setfile
        doctor_exempt, doctor_unexempt = dict(doctor_exempt or {}), list(doctor_unexempt or [])
        for flag, keys in (("--doctor-exempt", list(doctor_exempt)), ("--doctor-unexempt", doctor_unexempt)):
            for key in keys:
                try:
                    setfile.exempt_key(key)
                except setfile.SetError as error:
                    raise WorkspaceError(f"{flag} {error}") from None
        if only:
            unknown = sorted(set(only) - set(targets))
            if unknown:
                raise WorkspaceError(f"--only {', '.join(unknown)}: not in this landing (it lands "
                                     f"{', '.join(sorted(targets)) or 'no project'})")
            left = sorted(set(targets) - set(only))
            if left:
                self.say(f"--only: {', '.join(left)} left out")
            targets = {name: ref for name, ref in targets.items() if name in only}
        for name, url in (urls or {}).items():
            problem = setfile.url_problem(url)
            if problem:
                raise WorkspaceError(f"--add {name}: not a URL a public set may carry ({problem}); a repository that "
                                     "is not public lands with no url and is found by name under the workspace root")
        if tag:
            checked = subprocess.run(["git", "check-ref-format", f"refs/tags/{tag}"], capture_output=True)
            if checked.returncode != 0 or tag.startswith("-"):
                raise WorkspaceError(f"--tag {tag!r} is not a tag name git takes")
        self.fetch()
        self.reconcile()
        revision, text = self.head()
        current = setfile.parse(text)
        wanted, skipped = {}, []
        for name, ref in targets.items():
            repo = self._repo(name)
            if "origin" in git(repo, "remote").split():
                git(repo, "fetch", "--quiet", "origin", check=False)
            commit = git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
            if name == SET:  # the Set's own files: the landing's Set commit goes on top of this one
                if commit == revision:
                    skipped.append(name)
                    continue
                if subprocess.run(["git", "merge-base", "--is-ancestor", revision, commit], cwd=repo,
                                  capture_output=True).returncode != 0:
                    raise WorkspaceError(f"the Set change {commit[:12]} does not descend from the head "
                                         f"{revision[:12]}: git rebase {revision[:12]}, then wb land set=<commit>")
                wanted[name] = {"commit": commit}
                continue
            line = setfile.line(current, name)
            if line and line["commit"] == commit:
                skipped.append(name)
                continue
            if line and subprocess.run(["git", "merge-base", "--is-ancestor", line["commit"], commit], cwd=repo,
                                       capture_output=True).returncode != 0:
                raise WorkspaceError(f"{name} {commit[:12]} does not descend from its line {line['commit'][:12]}: "
                                     f"git rebase {line['commit'][:12]} (or git revert <range> for a rollback), "
                                     "then wb land")
            if tag:  # a tag the remote already has on another commit would leave the publish unfinishable
                there = git(repo, "ls-remote", "origin", f"refs/tags/{tag}", check=False).split()
                if there and there[0] != commit and git(repo, "rev-parse", "--verify", "-q", f"{there[0]}^{{commit}}",
                                                       check=False) != commit:
                    raise WorkspaceError(f"{name}'s remote has tag {tag} on {there[0][:12]} already, not on "
                                         f"{commit[:12]}: choose another --tag")
            item = {"commit": commit, "branch": branch or (line or {}).get("branch") or self._default_branch(repo)}
            if urls and name in urls:
                item["url"] = urls[name]
            wanted[name] = item
        self.export(revision, text, head=True)
        holds, drop_holds, third, platform = holds or {}, list(drop_holds or []), third or {}, platform or {}
        if not (wanted or holds or drop_holds or third or platform or doctor_exempt or doctor_unexempt):
            self.say(f"nothing moves: {', '.join(skipped) or 'no project'} already at its line")
            return {"state": "unchanged", "revision": revision}
        if tag and not [n for n in wanted if n != SET]:
            raise WorkspaceError("--tag tags the landed commit of a project, and this landing moves no project's line")
        for name, hold in holds.items():
            hold["artifacts"] = self._seed(name, hold)
        values = {"base": revision, "holds": holds, "drop_holds": drop_holds, "third": third, "platform": platform,
                  "doctor_exempt": doctor_exempt, "doctor_unexempt": doctor_unexempt}
        # Producers first: the Set's own change against the head; then each wave of landed projects, a project that
        # uses (or reads the files of) another one landing here running after it, against a would-be set that holds
        # the waves before it (and any hold, third entry or platform value this landing moves).
        if SET in wanted:
            self._check(SET, wanted[SET]["commit"], "", "its gate checks against the head")
        moves_set = bool(holds or drop_holds or third or platform or doctor_exempt or doctor_unexempt or SET in wanted)
        built = {SET: wanted[SET]} if SET in wanted else {}
        for index, wave in enumerate(self._waves({n: i for n, i in wanted.items() if n != SET})):
            candidate = self._reach(built, values)["candidate"] if index or moves_set else ""
            for name in wave:
                self._check(name, wanted[name]["commit"], candidate,
                            "its gate checks" + (f" against the would-be set {candidate}" if candidate else ""))
                built[name] = wanted[name]
        reach = self._reach(wanted, values)
        reached = reach.get("reached") or {}
        for note in reach.get("notes") or []:
            self.say(note)
        for warning in reach.get("warnings") or []:
            self.say(f"warning: {warning}")
        self._check_reached(reached, reach["candidate"])
        landing = time.strftime("l%Y%m%d%H%M%S") + secrets.token_hex(2)
        job = self.client.submit("set.land", {"landing": landing, "base": revision, "projects": wanted,
                                             "deadline": deadline, "caller": os.environ.get("WB_CALLER", ""),
                                             "holds": holds, "drop_holds": drop_holds, "third": third,
                                             "platform": platform, "tag": tag or "",
                                             "doctor_exempt": doctor_exempt, "doctor_unexempt": doctor_unexempt})
        self.say(f"{job.id}  set.land {landing}  (wb show {job.id})")
        record = self._prepared(job, landing)
        if dry:  # nothing was pushed, so nothing is deleted from any remote either
            from wb_core import landing as landings
            landings.move(self.store, landing, "abandoned", why="a dry landing publishes nothing", refs_dropped=True,
                          dry=True)
            return {"state": "dry", "landing": landing, "set_json": record["set_json"], "message": record["message"],
                    "reached": self._reached(record, reached)}
        result = self.publish(record, stop_after=stop_after)
        return {**result, "reached": self._reached(record, reached)}

    @staticmethod
    def _reached(record: dict, asked: dict) -> dict:
        """What the landing reached, as set.land judged it under the lock, with whether each pass stood beforehand."""
        return {name: {**item, **({"stands": asked[name]["stands"]} if name in asked else {})}
                for name, item in (record.get("reached") or {}).items()}

    def _check(self, name: str, commit: str, candidate: str, what: str):
        self.say(f"checking {name} at {commit[:12]} ({what}; a recorded pass stands)")
        envelope = self.client.submit("ci.run", {"project": name, "commit": commit,
                                                 **({"set": candidate} if candidate else {})}).wait(None)
        if envelope.get("outcome") != "ok":
            raise WorkspaceError(f"{name} {commit[:12]} did not pass its gate checks "
                                 f"({envelope.get('summary')}); nothing was landed: wb show {envelope.get('job')}")

    def _reach(self, projects: dict, values: dict) -> dict:
        """set.reach for these projects and values: the would-be set's candidate name and what it reaches."""
        envelope = self.client.submit("set.reach", {**values, "projects": projects}).wait(None)
        if envelope.get("outcome") != "ok":
            raise WorkspaceError(f"the landing cannot be composed: {envelope.get('summary')}; nothing was landed: "
                                 f"wb show {envelope.get('job')}")
        return dict(envelope.get("data") or {})

    def _check_reached(self, reached: dict, candidate: str):
        """Runs the gate checks of every reached consumer with no pass under its new key, against the would-be set,
        all submitted at once; a consumer whose pass stands is not run again."""
        if not reached:
            return
        jobs = {}
        for name, item in sorted(reached.items()):
            why = ", ".join(item.get("why") or [])
            if item.get("stands"):
                self.say(f"reached {name} at {item['commit'][:12]} ({why}): its pass stands")
                continue
            self.say(f"reached {name} at {item['commit'][:12]} ({why}): checking it against the would-be set")
            jobs[name] = self.client.submit("ci.run", {"project": name, "commit": item["commit"], "set": candidate})
        failed = []
        for name, job in jobs.items():
            envelope = job.wait(None)
            if envelope.get("outcome") != "ok":
                failed.append(f"{name} {reached[name]['commit'][:12]} ({', '.join(reached[name].get('why') or [])}): "
                              f"{envelope.get('summary')} (wb show {job.id})")
        if failed:
            raise WorkspaceError("the landing would break what it reaches, so nothing was landed: " + "; ".join(failed))

    def _waves(self, wanted: dict) -> list[list[str]]:
        """The landed projects in the order they build: a project whose [uses] names, or whose checks' tools read,
        another one landing here comes after it. A cycle is refused."""
        import tomllib
        edges = {}
        for name, item in wanted.items():
            text = git(self._repo(name), "show", f"{item['commit']}:project.toml", check=False)
            try:
                data = tomllib.loads(text) if text else {}
            except tomllib.TOMLDecodeError:
                data = {}
            needs = set(data.get("uses") or {})
            for check in (data.get("checks") or {}).values():
                needs |= {str(t).split(":")[0] for t in (check or {}).get("tools") or []}
            edges[name] = (needs & set(wanted)) - {name}
        waves, done = [], set()
        while len(done) < len(edges):
            wave = sorted(n for n, needs in edges.items() if n not in done and needs <= done)
            if not wave:
                raise WorkspaceError("these projects use each other, so none of them can build first: "
                                     + ", ".join(sorted(set(edges) - done)))
            waves.append(wave)
            done |= set(wave)
        return waves

    def _seed(self, name: str, hold: dict) -> dict:
        """Seeds a hold's file into the Store with its source and reason; returns its artifacts entry."""
        from wb_core import artifacts
        project, _, rest = name.partition(":")
        artifact = rest.split("@")[0] or project
        path = Path(hold.pop("file"))
        if not path.is_file():
            raise WorkspaceError(f"hold {name}: no file at {path}")
        note = artifacts.seed(self.store, path, f"{project}:{artifact}@{hold['commit']}", hold.get("source") or "",
                              hold["reason"], self.workspace.folder("pin_cache"))
        return {artifact: {"sha256": note["sha256"], "file": note["name"]}}

    def _default_branch(self, repo: Path) -> str:
        ref = git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False)
        return ref.split("/", 1)[1] if "/" in ref else "main"

    def _prepared(self, job, landing: str) -> dict:
        from wb_core import landing as landings
        told = None
        while True:
            record = landings.read(self.store, landing)
            if record and record.get("state") == "prepared":
                return record
            envelope = job.refresh()
            if envelope.state in ("settled", "cancelled", "interrupted"):
                raise WorkspaceError(f"set.land ended before publishing: {envelope.get('summary')} "
                                     f"(wb show {job.id})")
            reason = (envelope.get("waiting") or {}).get("reason") if isinstance(envelope.get("waiting"), dict) \
                else envelope.get("waiting")
            if reason and reason != told:
                self.say(f"waiting: {reason}")
                told = reason
            time.sleep(0.5)

    def _abandon(self, record: dict, why: str):
        from wb_core import landing as landings
        self._drop_refs(record)
        landings.move(self.store, record["id"], "abandoned", why=why, refs_dropped=True)

    # -- publishing ---------------------------------------------------------------------------------------------
    def publish(self, record: dict, stop_after: str | None = None) -> dict:
        from wb_core import landing as landings
        landing = record["id"]
        for name, member in record["projects"].items():
            pushed = subprocess.run(["git", "push", "--quiet", "origin", f"{member['commit']}:refs/landed/{landing}"],
                                    cwd=self._repo(name), capture_output=True, text=True,
                                    env={**os.environ, **GIT_ENV})
            if pushed.returncode != 0:
                self._abandon(record, f"pushing {name}'s landing ref failed")
                raise WorkspaceError(f"could not push {name}'s commit to its remote (nothing moved): "
                                     f"{(pushed.stderr or pushed.stdout).strip()[-400:]}")
        record = landings.move(self.store, landing, "refs-pushed")
        if stop_after == "refs":
            return {"state": "refs-pushed", "landing": landing}
        new = self._commit(record)
        if self._remote():
            pushed = subprocess.run(["git", "push", "--quiet", "origin", f"{new}:refs/heads/main"], cwd=self.home,
                                    capture_output=True, text=True)
            if pushed.returncode != 0:
                self._abandon(record, "the Set's remote moved")
                raise WorkspaceError("the Set's remote has moved (another machine landed): fetch it, then wb land "
                                     f"again: {(pushed.stderr or pushed.stdout).strip()[-300:]}")
        moved = subprocess.run(["git", "update-ref", "refs/heads/main", new, record["base"]], cwd=self.home,
                               capture_output=True, text=True)
        if moved.returncode != 0:
            self._abandon(record, "the Set moved under the landing")
            raise WorkspaceError("the Set moved while this landing was publishing; wb land again")
        self.export(new, record["set_json"], head=True)
        record = landings.move(self.store, landing, "set-pushed", revision=new)
        self.say(f"set {new[:12]}: {record['message'].splitlines()[0]}")
        if stop_after == "set":
            return {"state": "set-pushed", "landing": landing, "revision": new}
        return self.finish(record)

    def _commit(self, record: dict) -> str:
        """The Set commit, made with plumbing in the bare home: the base's tree with set.json replaced, or, for a
        change to the Set's own files, that change's tree with set.json replaced, on top of the change."""
        parent = record.get("change") or record["base"]
        if record.get("change"):  # bring the change's objects into the bare home from the Set's checkout
            pushed = subprocess.run(["git", "push", "--quiet", "--no-verify", str(self.home),
                                     f"{record['change']}:refs/landing/{record['id']}"],
                                    cwd=self._repo(SET), capture_output=True, text=True)
            if pushed.returncode != 0:
                raise WorkspaceError(f"the Set change could not be brought into the Set's home: {pushed.stderr.strip()}")
        blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=self.home, input=record["set_json"],
                              capture_output=True, text=True, check=True).stdout.strip()
        names = (list(record["projects"]) or [h.split(":")[0].split("@")[0] for h in record.get("holds") or []]
                 or ([SET] if record.get("change") or record.get("third") or record.get("platform")
                     or record.get("doctor") else []))
        if not names:
            raise WorkspaceError("a landing that names no project or hold has no identity to commit with")
        first = self._repo(names[0])
        identity = {}
        for key, variable in (("user.name", "NAME"), ("user.email", "EMAIL")):
            value = git(first, "config", key, check=False)
            if value:
                identity[f"GIT_AUTHOR_{variable}"] = identity[f"GIT_COMMITTER_{variable}"] = value
        with tempfile.TemporaryDirectory(prefix="wb-land-") as scratch:
            env = {**os.environ, **identity, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
            for args in (["read-tree", parent],
                         ["update-index", "--add", "--cacheinfo", f"100644,{blob},set.json"]):
                subprocess.run(["git", *args], cwd=self.home, env=env, check=True, capture_output=True)
            tree = subprocess.run(["git", "write-tree"], cwd=self.home, env=env, check=True, capture_output=True,
                                  text=True).stdout.strip()
            message = Path(scratch) / "message"
            message.write_text(record["message"], encoding="utf-8")
            made = subprocess.run(["git", "commit-tree", tree, "-p", parent, "-F", str(message)],
                                  cwd=self.home, env=env, capture_output=True, text=True)
        if record.get("change"):
            subprocess.run(["git", "update-ref", "-d", f"refs/landing/{record['id']}"], cwd=self.home,
                           capture_output=True)
        if made.returncode != 0:
            raise WorkspaceError(f"the Set commit could not be made: {made.stderr.strip()} (set user.name and "
                                 "user.email in the landed repository)")
        return made.stdout.strip()

    def finish(self, record: dict) -> dict:
        """Fast-forwards each set-named branch to its line, puts the landing's tag (when it has one) on each landed
        commit, and deletes the landing refs."""
        from wb_core import landing as landings
        landing = record["id"]
        tag = record.get("tag")
        behind = []
        for name, member in record["projects"].items():
            repo = self._repo(name)
            pushed = subprocess.run(["git", "push", "--quiet", "origin",
                                     f"{member['commit']}:refs/heads/{member['branch']}"],
                                    cwd=repo, capture_output=True, text=True, env={**os.environ, **GIT_ENV})
            if pushed.returncode != 0:
                behind.append(f"{name} {member['branch']}: {(pushed.stderr or pushed.stdout).strip()[-300:]}")
                continue
            if tag:  # a tag already there on this commit is fine; on another commit the push is refused
                pushed = subprocess.run(["git", "push", "--quiet", "origin", f"{member['commit']}:refs/tags/{tag}"],
                                        cwd=repo, capture_output=True, text=True, env={**os.environ, **GIT_ENV})
                if pushed.returncode != 0:
                    behind.append(f"{name} tag {tag}: {(pushed.stderr or pushed.stdout).strip()[-300:]}")
                    continue
            subprocess.run(["git", "push", "--quiet", "origin", f":refs/landed/{landing}"], cwd=repo,
                           capture_output=True, env={**os.environ, **GIT_ENV})
        if behind:
            self.say("landed, branch behind (the next wb land finishes it): " + "; ".join(behind))
            return {"state": "set-pushed", "landing": landing, "revision": record.get("revision"), "behind": behind}
        landings.move(self.store, landing, "landed")
        for name, member in record["projects"].items():
            self.say(f"{name}: {member['branch']} at {member['commit'][:12]}")
        return {"state": "landed", "landing": landing, "revision": record.get("revision")}


def _targets(workspace: Workspace, given: list[str], here: Path | None = None) -> dict[str, str]:
    """Each <project>=<ref>, the ref read in the worktree you stand in when it belongs to that project (HEAD is your
    HEAD, not its home checkout's); with none given, HEAD of the worktree you stand in."""
    here = here or Path.cwd()
    try:
        mine, top = workspace.project_for(here)
    except WorkspaceError:
        mine, top = None, None
    if given:
        targets = {}
        for item in given:
            name, sep, ref = item.partition("=")
            if not sep or not name or not ref:
                raise WorkspaceError(f"{item!r}: name a project and a ref as <project>=<ref>")
            targets[name] = git(top, "rev-parse", "--verify", f"{ref}^{{commit}}") if mine and mine.name == name                 else ref
        return targets
    if mine is None:
        raise WorkspaceError("stand in a worktree to land its HEAD, or name what lands: <project>=<ref>")
    return {mine.name: git(top, "rev-parse", "HEAD")}


def main(args) -> int:
    from .cli import _client
    try:
        workspace = Workspace()
        lander = Lander(workspace, _client())
        urls = {}
        for item in args.add or []:
            name, _, url = item.partition("=")
            urls[name] = url
        holds = {}
        if args.hold:
            if not (args.file and args.commit and args.reason):
                raise WorkspaceError("--hold needs --file, --commit and --reason (and --source: where the bytes "
                                     "came from)")
            holds[args.hold] = {"file": args.file, "commit": args.commit, "reason": args.reason,
                                "source": args.source or ""}
        third = {}
        if args.third:
            files = {}
            for item in args.third_file or []:
                kind, sep, digest = item.partition("=")
                kind, _, classifier = kind.partition(":")
                if not sep or not kind or len(digest) != 64:
                    raise WorkspaceError(f"--third-file {item!r}: KIND[:CLASSIFIER]=SHA256")
                files[kind] = {"sha256": digest.lower(), **({"classifier": classifier} if classifier else {})}
            if args.module or args.repository:
                if not (args.module and args.repository and files):
                    raise WorkspaceError("--third needs --module, --repository and at least one --third-file")
                third[args.third] = {"module": args.module, "repository": args.repository, "files": files,
                                     "licence": args.licence or "", "mod_id": args.mod_id or ""}
            else:
                # No coordinate: the one JAR in the drop folder beside the Store (<out>/third/<name>/).
                if not (args.source and args.licence):
                    raise WorkspaceError("--third without --module takes the JAR in <out>/third/<name>/ and needs "
                                         "--source (where it came from) and --licence")
                if set(files) - {"runtime"}:
                    raise WorkspaceError("--third without --module has one file: --third-file runtime=<sha256>")
                third[args.third] = {"source": args.source, "licence": args.licence, "mod_id": args.mod_id or "",
                                     **({"sha256": files["runtime"]["sha256"]} if files else {})}
        doctor_exempt = {}
        if args.doctor_exempt:
            if not args.reason:
                raise WorkspaceError("--doctor-exempt needs --reason: why the doctor leaves the path alone (one "
                                     "reason for every --doctor-exempt of the landing)")
            doctor_exempt = {key: args.reason for key in args.doctor_exempt}
        doctor_unexempt = list(args.doctor_unexempt or [])
        given = [t for t in args.targets or [] if not t.startswith("platform.")]
        platform = {}
        for item in args.targets or []:
            if item.startswith("platform."):
                key, sep, value = item[len("platform."):].partition("=")
                if not sep or not key or not value:
                    raise WorkspaceError(f"{item!r}: a platform move is platform.<key>=<value>")
                platform[key] = value
        moves = holds or args.drop_hold or third or platform or doctor_exempt or doctor_unexempt
        targets = _targets(workspace, given) if given or not moves else {}
        for name in urls:
            targets.setdefault(name, "HEAD")
        result = lander.land(targets, urls=urls, branch=args.branch, dry=args.dry, holds=holds,
                             drop_holds=list(args.drop_hold or []), third=third, platform=platform,
                             tag=args.tag, only=list(args.only or []), doctor_exempt=doctor_exempt,
                             doctor_unexempt=doctor_unexempt)
        reached = result.get("reached") or {}
        if result.get("state") in ("landed", "dry", "set-pushed"):
            _say("reached: " + (", ".join(f"{n} at {i['commit'][:12]} ({', '.join(i.get('why') or [])})"
                                          for n, i in sorted(reached.items())) or "no other project"))
        if result.get("state") == "landed" and not args.targets:
            from . import setwork  # a landing from this folder moves this folder's .set
            found = setwork.find(Path.cwd())
            place = found[0].parent if found else setwork.home_of(workspace, Path.cwd())
            setwork.write(place, result["revision"])
            _say(f"set {result['revision'][:7]} (head), written to {place / setwork.NAME}")
    except WorkspaceError as error:
        _say(str(error))
        return 2
    if args.json:
        print(json.dumps(result, indent=1))
    elif result.get("state") == "dry":
        print(result["message"])
    return 0 if result.get("state") in ("landed", "unchanged", "dry") else 1
