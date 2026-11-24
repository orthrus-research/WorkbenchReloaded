"""Maven Central through the Central Publisher Portal: a channel of ``kind = "central"``.

Central takes one deployment bundle per version: a zip in the Maven layout under the group's path, holding the JAR,
its ``-sources.jar`` and ``-javadoc.jar``, the POM, any other file of the publication (a Gradle module file), an
``.md5`` and ``.sha1`` (and by default ``.sha256`` and ``.sha512``) for every one of them, and an ``.asc`` detached
signature for every file that is not a checksum. Workbench builds it from the approved files:

- The POM is written by Workbench: the build's POM when it is among the files (its coordinates, packaging and
  dependencies), with the channel's ``pom`` metadata (name, description, url, developers, scm) and the rules'
  ``licence`` set in it; without one, a POM of the coordinates alone. Its bytes are part of the plan.
- A javadoc JAR must be among the files, unless the channel says ``javadoc = "placeholder"``: then Workbench writes
  a placeholder JAR holding only a README that points to the project, which Central accepts. The plan names it.
- Every file is signed with the channel's key: ``gpg --batch --pinentry-mode loopback --detach-sign --armor``, the
  gpg on PATH, the passphrase from the secret ``signing.secret`` given on gpg's standard input. With
  ``signing.keyring`` (a secret holding the armored secret key), the key is imported into a throwaway gpg home that
  is removed afterwards; without it, gpg's own home (GNUPGHOME or ~/.gnupg) must hold the key. Each signature is
  verified to be by that key before anything is sent.

Before uploading, the publish asks the Portal whether Central has published the version
(``GET /api/v1/publisher/published``) and stops if it has, or if the Portal does not answer. The bundle goes to
``POST /api/v1/publisher/upload`` with the user token (``Authorization: Bearer base64(username:password)``), and the
publish then polls ``POST /api/v1/publisher/status`` until the deployment is VALIDATED (USER_MANAGED: a person
presses Publish in the Portal), PUBLISHED (AUTOMATIC) or FAILED. The receipt keeps the deployment id, its last state
and the bundle's digest.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from wb_core.module import EnvironmentProblem

from .channels import Channel, Holdings, Receipt, _Client
from .transport import ChannelError, Form, excerpt

POM_NS = "http://maven.apache.org/POM/4.0.0"
POM_SCHEMA = "http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
WRITTEN = ("name", "description", "url", "licenses", "developers", "scm")  # what Workbench sets in a POM
COORDINATES = ("modelVersion", "parent", "groupId", "artifactId", "version", "packaging")
DEVELOPER = (("id", "id"), ("name", "name"), ("email", "email"), ("url", "url"), ("organization", "organization"),
             ("organization_url", "organizationUrl"))
STEADY = (1980, 1, 1, 0, 0, 0)  # the time stamp of every entry of a JAR Workbench writes, so its bytes repeat
DEPLOYMENT = re.compile(r"^[0-9A-Za-z][0-9A-Za-z-]{0,63}$")
TARGET = {"USER_MANAGED": "VALIDATED", "AUTOMATIC": "PUBLISHED"}
GPG_TIMEOUT = 120


# ---------------------------------------------------------------------------------------------
# what Workbench writes: the POM and a placeholder javadoc JAR

def problems(chan: Channel, version: str, sources: list, licence: dict | None, rules_name: str | None) -> list[str]:
    """What keeps these files from being a Central publication, before Workbench writes anything."""
    artifact = chan.project.split(":", 1)[1]
    prefix = f"{artifact}-{version}"
    names = {s.name for s in sources}
    out = []
    if version.upper().endswith("-SNAPSHOT"):
        out.append(f"{version} is a snapshot, and Central publishes releases only")
    if f"{prefix}.jar" not in names:
        out.append(f"a Central publication needs its JAR among the files: {prefix}.jar")
    if f"{prefix}-sources.jar" not in names:
        out.append(f"Central needs the sources JAR {prefix}-sources.jar among the files")
    if f"{prefix}-javadoc.jar" not in names and chan.settings["javadoc"] != "placeholder":
        out.append(f"Central needs the javadoc JAR {prefix}-javadoc.jar among the files: build one, or set "
                   f'{chan.where}.javadoc = "placeholder" to publish a placeholder JAR holding only a README')
    if not licence:
        where = f"[modules.release.rules.{rules_name}]" if rules_name else "the rules the channel names"
        out.append(f"Central needs the POM's licence: set licence = {{ name = ..., url = ... }} in {where}")
    elif not str(licence.get("url", "")).startswith(("https://", "http://")):
        out.append(f"the rules' licence url {licence.get('url')!r} is not an http(s) URL")
    return out


def pom(chan: Channel, version: str, licence: dict, given: bytes | None) -> bytes:
    """The POM Central gets: the build's (coordinates, packaging, dependencies) with the channel's metadata and the
    rules' licence set in it, or one of the coordinates alone."""
    group, artifact = chan.project.split(":", 1)
    ElementTree.register_namespace("", POM_NS)
    ElementTree.register_namespace("xsi", XSI)
    if given is not None:
        root = ElementTree.fromstring(given)
        ns = root.tag[:root.tag.index("}") + 1] if root.tag.startswith("{") else ""
    else:
        ns = "{" + POM_NS + "}"
        root = ElementTree.Element(f"{ns}project", {f"{{{XSI}}}schemaLocation": POM_SCHEMA})
        for tag, text in (("modelVersion", "4.0.0"), ("groupId", group), ("artifactId", artifact),
                          ("version", version)):
            ElementTree.SubElement(root, ns + tag).text = text
    for child in [c for c in root if c.tag in {ns + tag for tag in WRITTEN}]:
        root.remove(child)
    at = max((i + 1 for i, c in enumerate(root) if c.tag in {ns + tag for tag in COORDINATES}), default=0)
    meta = chan.settings["pom"]

    def element(tag: str, text: str | None = None, parent=None):
        node = ElementTree.Element(ns + tag) if parent is None else ElementTree.SubElement(parent, ns + tag)
        if text is not None:
            node.text = text
        return node

    written = [element("name", meta["name"]), element("description", meta["description"]),
               element("url", meta["url"])]
    licenses = element("licenses")
    entry = element("license", parent=licenses)
    element("name", licence["name"], entry)
    element("url", licence["url"], entry)
    if licence.get("distribution"):
        element("distribution", licence["distribution"], entry)
    developers = element("developers")
    for person in meta["developers"]:
        node = element("developer", parent=developers)
        for key, tag in DEVELOPER:
            if person.get(key):
                element(tag, person[key], node)
    scm = element("scm")
    for key, tag in (("connection", "connection"), ("developer_connection", "developerConnection"), ("url", "url")):
        element(tag, meta["scm"][key], scm)
    written += [licenses, developers, scm]
    for offset, node in enumerate(written):
        root.insert(at + offset, node)
    ElementTree.indent(root)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ElementTree.tostring(root, encoding="utf-8") + b"\n"


def placeholder_javadoc(chan: Channel, version: str) -> bytes:
    """A javadoc JAR holding only a README that points to the project (Central accepts one); the same bytes each
    time for the same channel and version."""
    meta = chan.settings["pom"]
    readme = (f"# {meta['name']} {version}\n\nNo Javadoc is published for {chan.project}:{version}. This JAR is a "
              f"placeholder.\n\nThe project: {meta['url']}\n")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as jar:
        for name, data in (("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\r\n\r\n"), ("README.md", readme)):
            info = zipfile.ZipInfo(name, date_time=STEADY)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            jar.writestr(info, data.encode("utf-8"))
    return buffer.getvalue()


def complete(chan: Channel, version: str, sources: list, licence: dict, folder: Path, measure) -> list:
    """The files of the publication: the POM rewritten (or written), and a placeholder javadoc JAR when the channel
    asks for one. ``measure(path, label)`` makes a source of a written file; the label stands for its path in the
    plan."""
    prefix = f"{chan.project.split(':', 1)[1]}-{version}"
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    out, given = [], None
    for source in sources:
        if source.name == f"{prefix}.pom":
            given = source
            continue
        out.append(source)
    if f"{prefix}-javadoc.jar" not in {s.name for s in sources}:
        path = folder / f"{prefix}-javadoc.jar"
        path.write_bytes(placeholder_javadoc(chan, version))
        out.append(measure(path, f"(a placeholder written by Workbench: {chan.where}.javadoc = \"placeholder\")"))
    path = folder / f"{prefix}.pom"
    path.write_bytes(pom(chan, version, licence, Path(given.path).read_bytes() if given else None))
    label = (f"(written by Workbench from {given.host_path} and {chan.where}.pom)" if given
             else f"(written by Workbench from {chan.where}.pom)")
    out.append(measure(path, label))
    return out


# ---------------------------------------------------------------------------------------------
# signing

def gpg_program() -> str:
    found = shutil.which("gpg")
    if not found:
        raise EnvironmentProblem("a central channel signs with gpg, and there is no gpg on PATH")
    return found


class Signer:
    """Detached armored signatures by one key, with the gpg on PATH."""

    def __init__(self, ctx, signing: dict):
        self.ctx, self.key = ctx, signing["key"]
        self.gpg = gpg_program()
        self.passphrase = ctx.secret(signing["secret"])
        self.keyring = ctx.secret_path(signing["keyring"]) if signing.get("keyring") else None
        self.home: Path | None = None

    def __enter__(self) -> "Signer":
        if self.keyring is not None:
            self.home = Path(tempfile.mkdtemp(prefix="wb-gpg-"))  # short: gpg-agent's socket path has a limit
            os.chmod(self.home, 0o700)
            (self.home / "private-keys-v1.d").mkdir(mode=0o700)
            self._run(["--import", _arg(self.keyring)], "import the signing key")
        return self

    def __exit__(self, *exc):
        if self.home is not None:
            gpgconf = shutil.which("gpgconf")
            if gpgconf:
                subprocess.run([gpgconf, "--homedir", _arg(self.home), "--kill", "gpg-agent"], capture_output=True,
                               timeout=30, check=False)
            shutil.rmtree(self.home, ignore_errors=True)

    def _run(self, args: list[str], what: str, *, passphrase: bool = False) -> str:
        command = [self.gpg, *(["--homedir", _arg(self.home)] if self.home else []), "--batch", "--no-tty",
                   *(["--pinentry-mode", "loopback", "--passphrase-fd", "0"] if passphrase else []), *args]
        self.ctx.log.debug("$ " + " ".join(command))
        try:
            done = subprocess.run(command, input=(self.passphrase + "\n") if passphrase else "", capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=GPG_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise EnvironmentProblem(f"gpg did not {what} within {GPG_TIMEOUT}s; nothing was uploaded") from None
        if done.returncode != 0:
            raise EnvironmentProblem(f"gpg could not {what} (exit {done.returncode}): "
                                     f"{excerpt(done.stderr.encode('utf-8'), 400)}; nothing was uploaded")
        return done.stdout

    def sign(self, path: Path, name: str, out: Path) -> Path:
        """The signature of ``path`` (published as ``name``), checked to be the key's."""
        signature = out / f"{name}.asc"
        signature.unlink(missing_ok=True)
        self._run(["--yes", "--local-user", self.key, "--detach-sign", "--armor", "--output", _arg(signature),
                   _arg(path)], f"sign {name} with {self.key}", passphrase=True)
        status = self._run(["--status-fd", "1", "--verify", _arg(signature), _arg(path)], f"verify {name}.asc")
        for line in status.splitlines():
            parts = line.split()
            if parts[:2] == ["[GNUPG:]", "VALIDSIG"] and self.key in (parts[2].upper(), parts[-1].upper()):
                return signature
        raise EnvironmentProblem(f"{name}.asc is not a valid signature by {self.key}; nothing was uploaded")


def _arg(path) -> str:
    return Path(path).as_posix()  # forward slashes: what every gpg build on Windows reads too


# ---------------------------------------------------------------------------------------------
# the bundle

def bundle(sources: list, signatures: dict, algos: list[str], group: str, artifact: str, version: str,
           path: Path) -> dict:
    """The deployment bundle: every file, its checksums and its signature under the version's folder."""
    folder = f"{group.replace('.', '/')}/{artifact}/{version}"
    entries = []
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for source in sources:
            archive.write(source.path, f"{folder}/{source.name}")
            entries.append(source.name)
            for algo in algos:
                archive.writestr(f"{folder}/{source.name}.{algo}", getattr(source, algo))
                entries.append(f"{source.name}.{algo}")
            archive.write(signatures[source.name], f"{folder}/{source.name}.asc")
            entries.append(f"{source.name}.asc")
    data = path.read_bytes()
    return {"name": path.name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "folder": folder,
            "entries": entries}


# ---------------------------------------------------------------------------------------------
# the Portal

class Central(_Client):
    def __init__(self, *args):
        super().__init__(*args)
        self.group, self.artifact = self.channel.project.split(":", 1)
        self.portal = f"{self.api}/api/v1/publisher"

    def _headers(self) -> dict:
        if not self.token:
            raise EnvironmentProblem(f"{self.channel.where}.secret is not mounted: the Portal answers only with the "
                                     "user token")
        if ":" not in self.token:
            raise EnvironmentProblem(f"{self.channel.name}'s secret must be the Portal user token as "
                                     "username:password")
        return {"Authorization": "Bearer " + base64.b64encode(self.token.encode("utf-8")).decode("ascii")}

    def published(self, version: str) -> bool:
        query = urllib.parse.urlencode({"namespace": self.group, "name": self.artifact, "version": version})
        answer = self.http.json("GET", f"{self.portal}/published?{query}", headers=self._headers(), missing=True)
        if not isinstance(answer, dict) or not isinstance(answer.get("published"), bool):
            raise ChannelError(f"the Portal did not say whether {self.channel.project}:{version} is published "
                               f"({'HTTP 404' if answer is None else excerpt(repr(answer).encode())}), so nothing "
                               "is sent")
        return answer["published"]

    def holdings(self, version: str, settings: dict, names: list[str]) -> Holdings:
        coordinates = f"{self.channel.project}:{version}"
        published = self.published(version)
        conflicts = [f"Maven Central already has {coordinates}; nothing was uploaded"] if published else []
        return Holdings(exists=published, release={"coordinates": coordinates, "published": published},
                        conflicts=conflicts)

    def publish(self, ctx, plan: dict, sources: list, notes: str, receipt: Receipt):
        version = plan["version"]
        central = plan["central"]
        out = ctx.work / "signed"
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True)
        signatures = {}
        with Signer(ctx, self.channel.settings["signing"]) as signer:
            for index, source in enumerate(sources):
                ctx.check_cancel()
                ctx.progress("sign", index / len(sources), f"{source.name} with {signer.key}")
                signatures[source.name] = signer.sign(source.path, source.name, out)
        built = bundle(sources, signatures, plan["maven"]["checksums"], self.group, self.artifact, version,
                       ctx.work / f"{self.artifact}-{version}-bundle.zip")
        receipt.release = {"coordinates": f"{self.channel.project}:{version}",
                           "publishing_type": central["publishing_type"], "signing_key": central["signing_key"],
                           "bundle": {k: built[k] for k in ("name", "sha256", "bytes")}, "deployment": None}
        ctx.check_cancel()
        ctx.progress("upload", 0.0, f"{built['name']} ({built['bytes']} bytes, {len(built['entries'])} entries)")
        form = Form()
        form.file("bundle", built["name"], ctx.work / built["name"], "application/octet-stream")
        query = urllib.parse.urlencode({"name": f"{self.channel.project}:{version}",
                                        "publishingType": central["publishing_type"]})
        answer = self.http.form("POST", f"{self.portal}/upload?{query}", form, headers=self._headers())
        deployment = answer.body.decode("utf-8", "replace").strip().strip('"')
        if not DEPLOYMENT.match(deployment):
            raise ChannelError(f"the Portal answered the upload without a deployment id: {excerpt(answer.body)}")
        receipt.release.update(deployment=deployment, url=self.channel.page(id=deployment, version=version)
                               or f"{self.api}/publishing/deployments", state="UPLOADED")
        for source in sources:
            receipt.add(source)
        self._wait(ctx, deployment, central["publishing_type"], receipt)

    def _wait(self, ctx, deployment: str, publishing_type: str, receipt: Receipt):
        """Polls the deployment until it reaches what the publishing type stops at, or fails."""
        interval = float(self.channel.settings["poll_interval"])
        deadline = time.monotonic() + float(self.channel.settings["poll_timeout"])
        target = TARGET[publishing_type]
        query = urllib.parse.urlencode({"id": deployment})
        while True:
            status = self.http.json("POST", f"{self.portal}/status?{query}", headers=self._headers())
            if not isinstance(status, dict):
                raise ChannelError(f"the Portal answered the status of {deployment} with "
                                   f"{excerpt(repr(status).encode())}")
            state = str(status.get("deploymentState") or "")
            receipt.release.update(state=state, purls=status.get("purls") or [])
            if state == "FAILED":
                errors = status.get("errors")
                receipt.release["errors"] = errors
                raise ChannelError(f"Central failed deployment {deployment}: "
                                   f"{excerpt(repr(errors).encode(), 600) if errors else 'it gave no reason'}")
            if state == target or (publishing_type == "USER_MANAGED" and state in ("PUBLISHING", "PUBLISHED")):
                if state == "VALIDATED":
                    receipt.release["next"] = (f"deployment {deployment} is validated and waits for a person to "
                                               f"press Publish in the Portal ({receipt.release['url']})")
                return
            if time.monotonic() >= deadline:
                raise ChannelError(f"deployment {deployment} is still {state or 'unknown'} after "
                                   f"{self.channel.settings['poll_timeout']:.0f}s; follow it in the Portal "
                                   f"({receipt.release['url']})")
            ctx.progress("validate", msg=f"deployment {deployment}: {state or 'unknown'}")
            ctx.sleep(interval)
