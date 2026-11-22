"""Publishing channels: ``[modules.release.channels.<name>]``, one per destination, each with one secret::

    kind = "modrinth"                    # github, modrinth, curseforge or maven
    base_url = "https://api.modrinth.com"   # the service's API; no default, so a channel always names where it goes
    project = "AbCdEf12"                 # github owner/repo; modrinth project id; curseforge project id (a number);
                                         # maven group:artifact
    secret = "release_modrinth"          # the [secrets.<name>] holding the token (maven: user:password, or just
                                         # the password when username is set)
    rules = "examplemod"                 # the [modules.release.rules.<name>] its files must pass (default: "default")
    page_url = "https://modrinth.com/mod/examplemod/version/{id}"   # receipts link here ({id}, {version})

    [modules.release.channels.<name>.defaults]   # publish parameters a call leaves out
    release_type = "beta"
    game_versions = ["1.12.2"]
    loaders = ["forge"]

The defaults a kind takes are its publish parameters: github ``release_type``, ``visibility``, ``name``, ``tag``,
``commit``; modrinth and curseforge ``release_type``, ``visibility``, ``name``, ``game_versions``, ``loaders``,
``dependencies``; maven none. Kind settings: github ``upload_url`` (default: base_url; https://uploads.github.com
for github.com); curseforge ``changelog_type`` (markdown, text or html), ``status_url`` and ``status_secret`` (the
Core API and its key: the upload API lists no files, so without them release.status cannot answer for the channel
and a publish has no pre-check); maven ``username``, ``checksums`` (default md5, sha1, sha256, sha512) and
``metadata`` (update maven-metadata.xml; default true).
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path
from xml.etree import ElementTree

from wb_core import types
from wb_core.module import EnvironmentProblem, ParamError

from . import rules as rulebook
from .transport import ChannelError, Form, Http, basic, checked_url, excerpt

KINDS = ("github", "modrinth", "curseforge", "maven")
RELEASE_TYPES = ("release", "beta", "alpha")
VISIBILITY = {"github": ("public", "draft"), "modrinth": ("public", "unlisted", "draft"),
              "curseforge": ("public",), "maven": ("public",)}
SETTINGS = {  # the publish parameters a kind takes; a channel's defaults may set the same
    "github": ("release_type", "visibility", "name", "tag", "commit"),
    "modrinth": ("release_type", "visibility", "name", "game_versions", "loaders", "dependencies"),
    "curseforge": ("release_type", "visibility", "name", "game_versions", "loaders", "dependencies"),
    "maven": (),
}
SETTING_FIELDS = {
    "release_type": {"type": "enum", "values": list(RELEASE_TYPES), "optional": True},
    "visibility": {"type": "enum", "values": ["public", "unlisted", "draft"], "optional": True},
    "name": {"type": "string", "optional": True},
    "tag": {"type": "string", "optional": True},
    "commit": {"type": "string", "optional": True},
    "game_versions": {"type": "list", "items": "string", "optional": True},
    "loaders": {"type": "list", "items": "string", "optional": True},
    "dependencies": {"type": "list", "items": "string", "optional": True},
}
BUILTIN = {"release_type": "release", "visibility": "public", "name": "{version}", "tag": "{version}", "commit": None,
           "game_versions": [], "loaders": [], "dependencies": []}
RELATIONS = {"modrinth": ("required", "optional", "embedded", "incompatible"),
             "curseforge": ("required", "optional", "embedded", "incompatible", "tool")}
CURSEFORGE_RELATIONS = {"required": "requiredDependency", "optional": "optionalDependency",
                        "embedded": "embeddedLibrary", "incompatible": "incompatible", "tool": "tool"}
CURSEFORGE_TYPES = {1: "release", 2: "beta", 3: "alpha"}
MODRINTH_STATUS = {"public": "listed", "unlisted": "unlisted", "draft": "draft"}
CHECKSUMS = ("md5", "sha1", "sha256", "sha512")
PROJECT = {
    "github": (re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"), "owner/repo"),
    "modrinth": (re.compile(r"^[A-Za-z0-9_.-]{1,64}$"), "a project id or slug"),
    "curseforge": (re.compile(r"^[0-9]{1,12}$"), "a numeric project id"),
    "maven": (re.compile(r"^[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+$"), "group:artifact"),
}
COMMON = {
    "kind": {"type": "enum", "values": list(KINDS)},
    "base_url": {"type": "string"},
    "project": {"type": "string"},
    "secret": {"type": "string", "optional": True},
    "rules": {"type": "string", "optional": True},
    "page_url": {"type": "string", "optional": True},
    "defaults": {"type": "object", "default": {}},
}
KIND_FIELDS = {
    "github": {"upload_url": {"type": "string", "optional": True}},
    "modrinth": {},
    "curseforge": {"changelog_type": {"type": "enum", "values": ["markdown", "text", "html"], "default": "markdown"},
                   "status_url": {"type": "string", "optional": True},
                   "status_secret": {"type": "string", "optional": True}},
    "maven": {"username": {"type": "string", "optional": True},
              "checksums": {"type": "list", "items": {"type": "enum", "values": list(CHECKSUMS)},
                            "default": list(CHECKSUMS)},
              "metadata": {"type": "bool", "default": True}},
}
REF = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+/-]{0,99}$")       # a tag or commit
PROJECT_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")  # a project a relation names


@dataclass
class Channel:
    name: str
    kind: str
    base_url: str
    project: str
    secret: str | None
    rules: str | None
    page_url: str | None
    defaults: dict
    settings: dict  # the kind's own keys

    @property
    def where(self) -> str:
        return f"modules.release.channels.{self.name}"

    def page(self, **values) -> str | None:
        if not self.page_url:
            return None
        try:
            return self.page_url.format_map(values)
        except (KeyError, IndexError, ValueError, AttributeError):
            return None


def load(config: dict, name: str) -> Channel:
    channels = config.get("channels") or {}
    if not isinstance(channels, dict) or name not in channels:
        have = ", ".join(sorted(channels)) if isinstance(channels, dict) else ""
        raise ParamError(f"no channel {name!r} in [modules.release.channels] (have: {have or 'none'})")
    where = f"modules.release.channels.{name}"
    raw = channels[name]
    if not isinstance(raw, dict):
        raise EnvironmentProblem(f"{where} must be a table")
    kind = raw.get("kind")
    if kind not in KINDS:
        raise EnvironmentProblem(f"{where}.kind must be one of {', '.join(KINDS)}, not {kind!r}")
    try:
        values = types.validate({**COMMON, **KIND_FIELDS[kind]}, raw, where="keys")
    except types.ValueError_ as error:
        raise EnvironmentProblem(f"{where}.{error.field}: {error.detail}") from None
    pattern, shape = PROJECT[kind]
    if not pattern.match(values["project"]):
        raise EnvironmentProblem(f"{where}.project = {values['project']!r} is not {shape}")
    try:
        values["base_url"] = checked_url(values["base_url"], f"{where}.base_url")
        for key in ("upload_url", "status_url"):
            if values.get(key):
                values[key] = checked_url(values[key], f"{where}.{key}")
    except ValueError as error:
        raise EnvironmentProblem(str(error)) from None
    if values.get("page_url") and not values["page_url"].startswith(("https://", "http://")):
        raise EnvironmentProblem(f"{where}.page_url must be an http(s) URL template")
    defaults = values["defaults"]
    unknown = sorted(set(defaults) - set(SETTINGS[kind]))
    if unknown:
        raise EnvironmentProblem(f"{where}.defaults: a {kind} channel takes no {', '.join(unknown)} "
                                 f"(it takes: {', '.join(SETTINGS[kind]) or 'none'})")
    try:
        defaults = types.validate(SETTING_FIELDS, defaults, where="keys")
    except types.ValueError_ as error:
        raise EnvironmentProblem(f"{where}.defaults.{error.field}: {error.detail}") from None
    own = {k: values[k] for k in KIND_FIELDS[kind] if k in values}
    return Channel(name=name, kind=kind, base_url=values["base_url"], project=values["project"],
                   secret=values.get("secret"), rules=values.get("rules"), page_url=values.get("page_url"),
                   defaults=defaults, settings=own)


def resolve(channel: Channel, given: dict, version: str, *, strict: bool = True) -> dict:
    """The settings of one publish: each parameter given, else the channel's default, else the built-in one."""
    wrong = sorted(k for k, v in given.items() if v is not None and k not in SETTINGS[channel.kind])
    if wrong:
        raise ParamError(f"a {channel.kind} channel takes no {', '.join(wrong)} "
                         f"(it takes: {', '.join(SETTINGS[channel.kind]) or 'none of these'})")
    out = {}
    for key in SETTINGS[channel.kind]:
        value = given.get(key)
        if value is None:
            value = channel.defaults.get(key)
        if value is None:
            value = BUILTIN[key]
        out[key] = list(value) if isinstance(value, list) else value
    if out.get("visibility", "public") not in VISIBILITY[channel.kind]:
        raise ParamError(f"a {channel.kind} channel publishes {' or '.join(VISIBILITY[channel.kind])}, "
                         f"not {out['visibility']}")
    for key in ("name", "tag"):
        if key in out:
            out[key] = _render(out[key], key, version, out.get("release_type"))
    for key in ("tag", "commit"):
        if out.get(key) and not REF.match(out[key]):
            raise ParamError(f"{key} {out[key]!r} is not a git ref (letters, digits and . _ + / -)")
    if strict and channel.kind == "modrinth" and not (out["game_versions"] and out["loaders"]):
        raise ParamError(f"a modrinth channel needs game_versions and loaders (give them, or set them in "
                         f"{channel.where}.defaults)")
    if strict and channel.kind == "curseforge" and not out["game_versions"]:
        raise ParamError(f"a curseforge channel needs game_versions (give them, or set them in "
                         f"{channel.where}.defaults)")
    if "dependencies" in out:
        out["dependencies"] = [_relation(channel.kind, value) for value in out["dependencies"]]
    if channel.kind == "maven":
        out["visibility"] = "public"
    return out


def _render(template: str, key: str, version: str, release_type: str | None) -> str:
    try:
        return template.format_map({"version": version, "release_type": release_type or ""})
    except (KeyError, IndexError, ValueError, AttributeError):
        raise ParamError(f"{key} {template!r} cannot be filled in: it may use {{version}} and {{release_type}}") \
            from None


def _relation(kind: str, value) -> dict:
    relation, sep, project = str(value).partition(":")
    if not sep or relation not in RELATIONS[kind] or not PROJECT_REF.match(project):
        raise ParamError(f"dependency {value!r} is not type:project, with type one of {', '.join(RELATIONS[kind])}")
    return {"type": relation, "project": project}


def precheck(channel: Channel) -> str:
    """What a publish asks the channel before it uploads anything, for the plan."""
    if channel.kind == "github":
        return "a release with the tag"
    if channel.kind == "modrinth":
        return "versions with this number and a loader in common"
    if channel.kind == "maven":
        return "the files at this version"
    return ("files of the same name, on the Core API" if channel.settings.get("status_url")
            else "none: the upload API lists no files (set status_url and status_secret)")


def maven_names(channel: Channel, version: str, sources: list, *, need_pom: bool = True) -> list[str]:
    """Names the files as the Maven layout does (a POM and a Gradle module file by role); returns the problems."""
    group, artifact = channel.project.split(":", 1)
    prefix = f"{artifact}-{version}"
    problems = []
    for source in sources:
        local = source.local_name
        if local == "pom-default.xml" or local.endswith(".pom"):
            source.name = f"{prefix}.pom"
        elif local == "module.json" or local.endswith(".module"):
            source.name = f"{prefix}.module"
        elif local.startswith((prefix + "-", prefix + ".")):
            source.name = local
        else:
            problems.append(f"{local} has no place in the Maven layout of {channel.project}:{version}: name it "
                            f"{prefix}[-classifier].ext")
    names = [s.name for s in sources]
    problems += [f"{name} would be published twice" for name in dict.fromkeys(n for n in names if names.count(n) > 1)]
    poms = [s for s in sources if s.name == f"{prefix}.pom"]
    if need_pom and not poms:
        problems.append(f"a Maven publication needs its POM among the files ({prefix}.pom or pom-default.xml)")
    for source in poms:
        try:
            pom = rulebook.read_pom(Path(source.path).read_bytes())
        except (ElementTree.ParseError, ValueError, OSError) as error:
            problems.append(f"{source.local_name} is not a readable POM: {error}")
            continue
        declared = f"{pom['group']}:{pom['artifact']}:{pom['version']}"
        if declared != f"{group}:{artifact}:{version}":
            problems.append(f"{source.local_name} is the POM of {declared}, not of {group}:{artifact}:{version}")
    return problems


def content_type(name: str) -> str:
    lower = name.lower()
    if lower.endswith(".jar"):
        return "application/java-archive"
    if lower.endswith((".pom", ".xml")):
        return "application/xml"
    if lower.endswith((".module", ".json")):
        return "application/json"
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


# ---------------------------------------------------------------------------------------------
# what a channel holds, and what a publish leaves behind

@dataclass
class Holdings:
    exists: bool = False
    release: dict | None = None
    files: list = field(default_factory=list)       # name, bytes, sha256 / sha512 / sha1 when told, id, url
    conflicts: list = field(default_factory=list)   # what a publish of this version would collide with

    def as_dict(self) -> dict:
        return asdict(self)


def compare(source, remote: dict | None, *, listed: bool = True) -> tuple[bool | None, str | None]:
    """Whether what a channel reports for a file is the file: by every digest it reports, else by size.
    (None, None) when it reports nothing to compare."""
    if not listed:
        return False, "absent"
    remote = {k: v for k, v in (remote or {}).items() if v not in (None, "")}
    checks = [(algo, str(remote[algo]).lower() == getattr(source, algo)) for algo in ("sha256", "sha512", "sha1")
              if algo in remote]
    if str(remote.get("bytes", "")).isdigit():
        checks.append(("size", int(remote["bytes"]) == source.bytes))
    if not checks:
        return None, None
    return all(ok for _, ok in checks), checks[0][0]


@dataclass
class Receipt:
    channel: str
    kind: str
    endpoint: str
    project: str
    version: str
    job: str
    quote: str | None
    plan_sha256: str | None
    started: str = field(default_factory=lambda: _now())
    finished: str | None = None
    complete: bool = False
    error: str | None = None
    release: dict = field(default_factory=dict)
    files: list = field(default_factory=list)
    notes_sha256: str | None = None

    def add(self, source, *, id=None, url=None, remote: dict | None = None, listed: bool = True):
        entry = {"name": source.name, "sha256": source.sha256, "bytes": source.bytes, "id": id, "url": url}
        entry["verified"], entry["verified_by"] = compare(source, remote, listed=listed)
        if remote:
            entry["remote"] = {k: v for k, v in remote.items() if v not in (None, "")}
        self.files.append(entry)

    def as_dict(self) -> dict:
        return {"schema": "wb/release-receipt/v1", **asdict(self)}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def client(channel: Channel, http: Http, token: str | None, status_token: str | None = None):
    kinds = {"github": GitHub, "modrinth": Modrinth, "curseforge": CurseForge, "maven": Maven}
    return kinds[channel.kind](channel, http, token, status_token)


class _Client:
    def __init__(self, channel: Channel, http: Http, token: str | None, status_token: str | None):
        self.channel, self.http, self.token, self.status_token = channel, http, token, status_token
        self.api = channel.base_url


# ---------------------------------------------------------------------------------------------
# GitHub releases

class GitHub(_Client):
    def __init__(self, *args):
        super().__init__(*args)
        self.owner, self.repo = self.channel.project.split("/", 1)
        self.uploads = self.channel.settings.get("upload_url") or self.api
        self.releases = f"{self.api}/repos/{self.owner}/{self.repo}/releases"

    def _headers(self) -> dict:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _release(self, tag: str) -> dict | None:
        """The release with the tag, drafts included (which the tag lookup does not find)."""
        for page in range(1, 11):
            listed = self.http.json("GET", f"{self.releases}?per_page=100&page={page}", headers=self._headers())
            if not isinstance(listed, list):
                raise ChannelError(f"GitHub answered the release list of {self.channel.project} with "
                                   f"{type(listed).__name__}")
            for release in listed:
                if isinstance(release, dict) and release.get("tag_name") == tag:
                    return release
            if len(listed) < 100:
                return None
        return None

    @staticmethod
    def _asset(asset: dict) -> dict:
        out = {"name": asset.get("name"), "bytes": asset.get("size"), "id": asset.get("id"),
               "url": asset.get("browser_download_url")}
        digest = str(asset.get("digest") or "")
        if digest.startswith("sha256:"):
            out["sha256"] = digest.split(":", 1)[1].lower()
        return out

    def holdings(self, version: str, settings: dict, names: list[str]) -> Holdings:
        tag = settings["tag"]
        release = self._release(tag)
        if release is None:
            return Holdings(exists=False, release={"tag": tag})
        info = {"id": release.get("id"), "url": release.get("html_url"), "tag": tag, "name": release.get("name"),
                "draft": bool(release.get("draft")), "prerelease": bool(release.get("prerelease"))}
        files = [self._asset(a) for a in release.get("assets") or [] if isinstance(a, dict)]
        conflict = (f"{self.channel.name} already has a {'draft ' if info['draft'] else ''}release tagged {tag} "
                    f"(id {info['id']}{', ' + info['url'] if info['url'] else ''}) with {len(files)} file(s); "
                    "nothing was uploaded")
        return Holdings(exists=True, release=info, files=files, conflicts=[conflict])

    def publish(self, ctx, plan: dict, sources: list, notes: str, receipt: Receipt):
        payload = {"tag_name": plan["tag"], "name": plan["name"], "body": notes,
                   "draft": plan["visibility"] == "draft", "prerelease": plan["release_type"] != "release"}
        if plan.get("commit"):
            payload["target_commitish"] = plan["commit"]
        ctx.progress("release", msg=f"creating the release tagged {plan['tag']}")
        created = self.http.json("POST", self.releases, headers=self._headers(), payload=payload)
        if not isinstance(created, dict) or created.get("id") is None:
            raise ChannelError(f"GitHub created no release: {created!r}"[:300])
        receipt.release = {"id": created["id"], "url": created.get("html_url"), "tag": plan["tag"],
                           "draft": payload["draft"], "prerelease": payload["prerelease"]}
        for index, source in enumerate(sources):
            ctx.check_cancel()
            ctx.progress("upload", index / len(sources), f"{source.name} ({source.bytes} bytes)")
            url = (f"{self.uploads}/repos/{self.owner}/{self.repo}/releases/{created['id']}/assets"
                   f"?name={urllib.parse.quote(source.name)}")
            asset = self.http.upload("POST", url, source.path, headers=self._headers(),
                                     content_type=content_type(source.name)).json()
            remote = self._asset(asset if isinstance(asset, dict) else {})
            receipt.add(source, id=remote.pop("id"), url=remote.pop("url"),
                        remote={k: v for k, v in remote.items() if k != "name"})


# ---------------------------------------------------------------------------------------------
# Modrinth

class Modrinth(_Client):
    def _headers(self) -> dict:
        return {"Authorization": self.token} if self.token else {}

    def _versions(self) -> list:
        project = urllib.parse.quote(self.channel.project)
        listed = self.http.json("GET", f"{self.api}/v2/project/{project}/version?include_changelog=false",
                                headers=self._headers(), missing=True)
        if listed is None:
            raise ChannelError(f"modrinth has no project {self.channel.project} (or the token cannot see it)", 404)
        if not isinstance(listed, list):
            raise ChannelError(f"modrinth answered the version list with {type(listed).__name__}")
        return [v for v in listed if isinstance(v, dict)]

    def holdings(self, version: str, settings: dict, names: list[str]) -> Holdings:
        found = [v for v in self._versions() if v.get("version_number") == version]
        loaders = set(settings.get("loaders") or [])
        files, conflicts = [], []
        for item in found:
            for f in item.get("files") or []:
                hashes = f.get("hashes") or {}
                files.append({"name": f.get("filename"), "bytes": f.get("size"), "sha512": hashes.get("sha512"),
                              "sha1": hashes.get("sha1"), "id": item.get("id"), "url": f.get("url")})
            if not loaders or loaders & set(item.get("loaders") or []):
                conflicts.append(f"{self.channel.name} already has version {version} ({item.get('id')}, "
                                 f"{', '.join(item.get('loaders') or []) or 'no loaders'}); nothing was uploaded")
        release = {"versions": [{k: item.get(k) for k in ("id", "name", "version_type", "status", "loaders",
                                                          "game_versions")} for item in found]}
        return Holdings(exists=bool(found), release=release, files=files, conflicts=conflicts)

    def publish(self, ctx, plan: dict, sources: list, notes: str, receipt: Receipt):
        primary = next(i for i, f in enumerate(plan["files"]) if f["primary"])
        data = {"name": plan["name"], "version_number": plan["version"], "changelog": notes,
                "dependencies": [{"project_id": d["project"], "dependency_type": d["type"]}
                                 for d in plan["dependencies"]],
                "game_versions": plan["game_versions"], "version_type": plan["release_type"],
                "loaders": plan["loaders"], "featured": False, "status": MODRINTH_STATUS[plan["visibility"]],
                "project_id": self.channel.project, "file_parts": [f"file{i}" for i in range(len(sources))],
                "primary_file": f"file{primary}"}
        form = Form()
        form.field("data", _json(data))
        for index, source in enumerate(sources):
            form.file(f"file{index}", source.name, source.path, content_type(source.name))
        ctx.progress("upload", 0.0, f"version {plan['version']} with {len(sources)} file(s) in one request")
        created = self.http.form("POST", f"{self.api}/v2/version", form, headers=self._headers()).json()
        if not isinstance(created, dict) or not created.get("id"):
            raise ChannelError(f"modrinth created no version: {created!r}"[:300])
        receipt.release = {"id": created["id"], "url": self.channel.page(id=created["id"], version=plan["version"]),
                           "version_number": created.get("version_number")}
        remote = {f.get("filename"): f for f in created.get("files") or [] if isinstance(f, dict)}
        for source in sources:
            entry = remote.get(source.name)
            hashes = (entry or {}).get("hashes") or {}
            receipt.add(source, url=(entry or {}).get("url"), listed=entry is not None,
                        remote={"sha512": hashes.get("sha512"), "sha1": hashes.get("sha1"),
                                "bytes": (entry or {}).get("size")})


# ---------------------------------------------------------------------------------------------
# CurseForge: the upload API publishes; the Core API (optional) says what a project holds

class CurseForge(_Client):
    def _headers(self) -> dict:
        return {"X-Api-Token": self.token} if self.token else {}

    def game_version_ids(self, game_versions: list[str], loaders: list[str]) -> list[int]:
        """CurseForge's ids for version and loader names (a number is taken as an id); a Minecraft version is
        preferred over a same-named one of another game, a loader over a same-named version."""
        types_ = self.http.json("GET", f"{self.api}/api/game/version-types", headers=self._headers())
        versions = self.http.json("GET", f"{self.api}/api/game/versions", headers=self._headers())
        if not isinstance(types_, list) or not isinstance(versions, list):
            raise ChannelError("CurseForge answered the game version lists with something else")
        slug = {t.get("id"): str(t.get("slug") or "") for t in types_ if isinstance(t, dict)}
        ids = []
        for value, prefer in [(v, "minecraft") for v in game_versions] + [(v, "modloader") for v in loaders]:
            if str(value).isdigit():
                ids.append(int(value))
                continue
            wanted = str(value).lower()
            matches = [v for v in versions if isinstance(v, dict)
                       and wanted in (str(v.get("name", "")).lower(), str(v.get("slug", "")).lower())]
            chosen = [v for v in matches if slug.get(v.get("gameVersionTypeID"), "").startswith(prefer)] or matches
            if not chosen:
                raise ParamError(f"CurseForge knows no game version or loader {value!r}")
            if len(chosen) > 1:
                kinds = ", ".join(f"{slug.get(v.get('gameVersionTypeID'), '?')} {v.get('id')}" for v in chosen)
                raise ParamError(f"CurseForge has {len(chosen)} versions named {value!r} ({kinds}); give its id")
            ids.append(int(chosen[0]["id"]))
        return list(dict.fromkeys(ids))

    def _upload(self, source, metadata: dict) -> int:
        form = Form()
        form.field("metadata", _json(metadata))
        form.file("file", source.name, source.path, content_type(source.name))
        answer = self.http.form("POST", f"{self.api}/api/projects/{self.channel.project}/upload-file", form,
                                headers=self._headers()).json()
        file_id = answer.get("id") if isinstance(answer, dict) else None
        if not isinstance(file_id, int):
            raise ChannelError(f"CurseForge answered the upload of {source.name} without a file id: {answer!r}"[:300])
        return file_id

    def publish(self, ctx, plan: dict, sources: list, notes: str, receipt: Receipt):
        ctx.progress("resolve", msg="looking up CurseForge's ids for the game versions and loaders")
        ids = self.game_version_ids(plan["game_versions"], plan["loaders"])
        primary = next(i for i, f in enumerate(plan["files"]) if f["primary"])
        main, others = sources[primary], [s for i, s in enumerate(sources) if i != primary]
        metadata = {"changelog": notes, "changelogType": plan["changelog_type"], "displayName": plan["name"],
                    "gameVersions": ids, "releaseType": plan["release_type"]}
        if plan["dependencies"]:
            metadata["relations"] = {"projects": [{"slug": d["project"], "type": CURSEFORGE_RELATIONS[d["type"]]}
                                                  for d in plan["dependencies"]]}
        ctx.progress("upload", 0.0, f"{main.name} ({main.bytes} bytes)")
        main_id = self._upload(main, metadata)
        receipt.release = {"id": main_id, "url": self.channel.page(id=main_id, version=plan["version"]),
                           "game_version_ids": ids}
        receipt.add(main, id=main_id, url=receipt.release["url"])
        for index, source in enumerate(others, start=1):
            ctx.check_cancel()
            ctx.progress("upload", index / len(sources), f"{source.name} ({source.bytes} bytes)")
            child = {"changelog": notes, "changelogType": plan["changelog_type"], "parentFileID": main_id,
                     "releaseType": plan["release_type"]}
            file_id = self._upload(source, child)
            receipt.add(source, id=file_id, url=self.channel.page(id=file_id, version=plan["version"]))

    def holdings(self, version: str, settings: dict, names: list[str]) -> Holdings:
        status_url = self.channel.settings.get("status_url")
        if not status_url:
            raise EnvironmentProblem(f"CurseForge's upload API lists no files, so {self.channel.name} cannot be asked "
                                     f"what it holds: set {self.channel.where}.status_url (the Core API) and "
                                     "status_secret (its key)")
        if not self.status_token:
            raise EnvironmentProblem(f"{self.channel.where}.status_secret is not set or not mounted")
        headers = {"x-api-key": self.status_token}
        listed = []
        for index in range(0, 10_000, 50):
            page = self.http.json("GET", f"{status_url}/v1/mods/{self.channel.project}/files?index={index}"
                                  "&pageSize=50", headers=headers)
            data = page.get("data") if isinstance(page, dict) else None
            if not isinstance(data, list):
                raise ChannelError(f"the CurseForge Core API answered with {excerpt(repr(page).encode())}")
            listed += [f for f in data if isinstance(f, dict)]
            total = int(((page.get("pagination") or {}).get("totalCount")) or 0)
            if len(data) < 50 or index + 50 >= total:
                break
        wanted = set(names)
        matched = [f for f in listed if f.get("fileName") in wanted or f.get("displayName") == settings.get("name")
                   or rulebook.carries(f.get("fileName"), version)]
        files = []
        for f in matched:
            sha1 = next((h.get("value") for h in f.get("hashes") or [] if isinstance(h, dict) and h.get("algo") == 1),
                        None)
            files.append({"name": f.get("fileName"), "bytes": f.get("fileLength"), "sha1": sha1, "id": f.get("id"),
                          "url": f.get("downloadUrl"), "display_name": f.get("displayName"),
                          "release_type": CURSEFORGE_TYPES.get(f.get("releaseType")), "status": f.get("fileStatus")})
        conflicts = [f"{self.channel.name} already holds {f['name']} (file {f['id']}); nothing was uploaded"
                     for f in files if f["name"] in wanted]
        return Holdings(exists=bool(files), release=None, files=files, conflicts=conflicts)


# ---------------------------------------------------------------------------------------------
# Maven repositories

class Maven(_Client):
    def __init__(self, *args):
        super().__init__(*args)
        self.group, self.artifact = self.channel.project.split(":", 1)
        self.root = f"{self.api}/{self.group.replace('.', '/')}/{self.artifact}"

    def _headers(self) -> dict:
        if not self.token:
            return {}
        user = self.channel.settings.get("username")
        password = self.token
        if not user:
            user, sep, password = self.token.partition(":")
            if not sep:
                raise EnvironmentProblem(f"{self.channel.name}'s secret must be user:password, or set "
                                         f"{self.channel.where}.username")
        return {"Authorization": basic(user, password)}

    def _exists(self, url: str) -> dict | None:
        try:
            answer = self.http.request("HEAD", url, headers=self._headers(), missing=True)
        except ChannelError as error:
            if error.status not in (405, 501):
                raise
            answer = self.http.request("GET", url, headers=self._headers(), missing=True)
        if answer.status == 404:
            return None
        size = {k.lower(): v for k, v in answer.headers.items()}.get("content-length")
        return {"bytes": int(size)} if size and size.isdigit() else {}

    def versions(self) -> list[str] | None:
        answer = self.http.request("GET", f"{self.root}/maven-metadata.xml", headers=self._headers(), missing=True)
        if answer.status == 404:
            return None
        try:
            root = ElementTree.fromstring(answer.body)
        except ElementTree.ParseError as error:
            raise ChannelError(f"{self.root}/maven-metadata.xml is not XML: {error}") from None
        return [v.text.strip() for v in root.findall("versioning/versions/version") if v.text and v.text.strip()]

    def holdings(self, version: str, settings: dict, names: list[str]) -> Holdings:
        folder = f"{self.root}/{version}"
        files, conflicts = [], []
        for name in names or [f"{self.artifact}-{version}.pom", f"{self.artifact}-{version}.jar"]:
            url = f"{folder}/{name}"
            held = self._exists(url)
            if held is None:
                continue
            entry = {"name": name, "url": url, **held}
            for algo in ("sha256", "sha1"):
                answer = self.http.request("GET", f"{url}.{algo}", headers=self._headers(), missing=True)
                if answer.status != 404:
                    entry[algo] = answer.body.decode("ascii", "replace").split()[0].lower() if answer.body.strip() \
                        else None
            files.append(entry)
            conflicts.append(f"{self.channel.name} already holds {name} at {url}; nothing was uploaded")
        listed = self.versions()
        release = {"coordinates": f"{self.group}:{self.artifact}:{version}", "folder": folder,
                   "in_metadata": version in (listed or [])}
        return Holdings(exists=bool(files) or release["in_metadata"], release=release, files=files,
                        conflicts=conflicts)

    def _put(self, url: str, body: bytes, content: str):
        self.http.request("PUT", url, headers={**self._headers(), "Content-Type": content}, body=body,
                          length=len(body))

    def publish(self, ctx, plan: dict, sources: list, notes: str, receipt: Receipt):
        folder = f"{self.root}/{plan['version']}"
        algos = plan["maven"]["checksums"]
        receipt.release = {"coordinates": f"{self.group}:{self.artifact}:{plan['version']}", "url": folder}
        for index, source in enumerate(sources):
            ctx.check_cancel()
            ctx.progress("upload", index / len(sources), f"{source.name} ({source.bytes} bytes)")
            url = f"{folder}/{source.name}"
            self.http.upload("PUT", url, source.path, headers=self._headers(), content_type=content_type(source.name))
            for algo in algos:
                self._put(f"{url}.{algo}", getattr(source, algo).encode("ascii"), "text/plain")
            receipt.add(source, url=url)
        if plan["maven"]["metadata"]:
            ctx.progress("metadata", 1.0, "maven-metadata.xml")
            listed = self.versions() or []
            body = metadata_xml(self.group, self.artifact, listed, plan["version"])
            url = f"{self.root}/maven-metadata.xml"
            self._put(url, body, "application/xml")
            for algo in algos:
                self._put(f"{url}.{algo}", _hex(algo, body).encode("ascii"), "text/plain")
            receipt.release["metadata"] = url


def metadata_xml(group: str, artifact: str, versions: list[str], version: str) -> bytes:
    """maven-metadata.xml with ``version`` added, as latest and release (as Gradle and Maven write it)."""
    listed = list(dict.fromkeys([*versions, version]))
    root = ElementTree.Element("metadata")
    ElementTree.SubElement(root, "groupId").text = group
    ElementTree.SubElement(root, "artifactId").text = artifact
    versioning = ElementTree.SubElement(root, "versioning")
    ElementTree.SubElement(versioning, "latest").text = version
    ElementTree.SubElement(versioning, "release").text = version
    holder = ElementTree.SubElement(versioning, "versions")
    for item in listed:
        ElementTree.SubElement(holder, "version").text = item
    ElementTree.SubElement(versioning, "lastUpdated").text = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    ElementTree.indent(root)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ElementTree.tostring(root, encoding="utf-8") + b"\n"


def _hex(algo: str, data: bytes) -> str:
    return hashlib.new(algo, data, usedforsecurity=False).hexdigest()


def _json(value) -> str:
    return json.dumps(value, separators=(",", ":"))
