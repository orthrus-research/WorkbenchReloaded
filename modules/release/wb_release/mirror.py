"""Our own public Maven repository in an S3-compatible bucket (Cloudflare R2): a channel of ``kind = "mirror"``.

Every request goes to the bucket's S3 API (``base_url``, path style: ``<base_url>/<bucket>/<key>``), signed with AWS
Signature Version 4 (written here with the standard library alone) by the access key id and secret access key from
Workbench secrets. A publish lays the version out as Maven does: every file and an ``.md5``, ``.sha1``, ``.sha256``
and ``.sha512`` beside it, then ``maven-metadata.xml`` (and its checksums) merged with what the bucket holds.

A path that already holds bytes is never replaced: every path is read first (HEAD; an object without the SHA-256
Workbench stores beside it is read in full and hashed). The same bytes are kept as they are, and other bytes stop the
publish before anything is written. Each write is conditional as well (``If-None-Match: *``; the metadata
``If-Match`` its ETag), so a writer racing the publish is refused, not overwritten. Only maven-metadata.xml and its
checksums are rewritten, with every version they listed kept. Then every path is read back from the public base URL
(``public_url``, past any cache), and the receipt says whether each file served is the file published.

The channel publishes one group (``group``) and its subgroups, and refuses any other.
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import itertools
import re
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from wb_core.module import EnvironmentProblem

from .channels import Holdings, Receipt, _Client, content_type
from .transport import ChannelError, excerpt

ALGORITHM = "AWS4-HMAC-SHA256"
EMPTY = hashlib.sha256(b"").hexdigest()
UNRESERVED = "-_.~"
VERIFY_TRIES = 4
VERIFY_WAIT = 2.0


# ---------------------------------------------------------------------------------------------
# AWS Signature Version 4

def _encode(text: str, safe: str = UNRESERVED) -> str:
    return urllib.parse.quote(text, safe=safe)


def sign(method: str, url: str, *, key_id: str, secret: str, region: str, payload_sha256: str,
         headers: dict | None = None, now: float | None = None, service: str = "s3") -> dict:
    """The headers that sign one request (Authorization, x-amz-date, x-amz-content-sha256). ``headers`` are the
    other headers it sends that are signed too; the URL's path is taken as already encoded, once."""
    parts = urllib.parse.urlsplit(url)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() if now is None else now))
    day = stamp[:8]
    signed = {key.lower(): " ".join(str(value).split()) for key, value in (headers or {}).items()}
    signed.update({"host": parts.netloc.lower(), "x-amz-content-sha256": payload_sha256, "x-amz-date": stamp})
    names = sorted(signed)
    query = "&".join(f"{_encode(k)}={_encode(v)}" for k, v in
                     sorted(urllib.parse.parse_qsl(parts.query, keep_blank_values=True)))
    canonical = "\n".join([method, parts.path or "/", query, "".join(f"{n}:{signed[n]}\n" for n in names),
                           ";".join(names), payload_sha256])
    scope = f"{day}/{region}/{service}/aws4_request"
    text = "\n".join([ALGORITHM, stamp, scope, hashlib.sha256(canonical.encode("utf-8")).hexdigest()])
    key = f"AWS4{secret}".encode("utf-8")
    for part in (day, region, service, "aws4_request"):
        key = hmac.new(key, part.encode("utf-8"), hashlib.sha256).digest()
    signature = hmac.new(key, text.encode("utf-8"), hashlib.sha256).hexdigest()
    return {"Authorization": f"{ALGORITHM} Credential={key_id}/{scope}, SignedHeaders={';'.join(names)}, "
                             f"Signature={signature}",
            "x-amz-date": stamp, "x-amz-content-sha256": payload_sha256}


# ---------------------------------------------------------------------------------------------
# maven-metadata.xml, merged

def _tokens(version: str) -> list:
    return [(1, int(t), "") if t.isdigit() else (0, 0, t.lower()) for t in re.split(r"[.\-_+]", version) if t]


def compare_versions(a: str, b: str) -> int:
    """Maven's order, near enough for release versions: numbers by value, and a qualifier (1.0-beta) before the
    release it qualifies (1.0)."""
    for x, y in itertools.zip_longest(_tokens(a), _tokens(b), fillvalue=(1, 0, "")):  # 1.0 is 1.0.0
        if x != y:
            return -1 if x < y else 1
    return 0


def listed_versions(existing: bytes | None, group: str, artifact: str) -> list[str]:
    """The versions a maven-metadata.xml lists, checked to be of ``group:artifact``."""
    if not existing:
        return []
    try:
        root = ElementTree.fromstring(existing)
    except ElementTree.ParseError as error:
        raise ChannelError(f"the bucket's maven-metadata.xml is not XML ({error}); nothing was replaced") from None
    declared = (root.findtext("groupId"), root.findtext("artifactId"))
    if declared != (group, artifact):
        raise ChannelError(f"the bucket's maven-metadata.xml is of {declared[0]}:{declared[1]}, not of "
                           f"{group}:{artifact}; nothing was replaced")
    return [v.text.strip() for v in root.findall("versioning/versions/version") if v.text and v.text.strip()]


def merged_metadata(existing: bytes | None, group: str, artifact: str, version: str) -> tuple[bytes, list[str]]:
    """maven-metadata.xml listing every version ``existing`` lists and ``version``; latest and release name the
    newest. Returns the document and its versions."""
    versions = list(dict.fromkeys([*listed_versions(existing, group, artifact), version]))
    newest = max(versions, key=functools.cmp_to_key(compare_versions))
    root = ElementTree.Element("metadata")
    ElementTree.SubElement(root, "groupId").text = group
    ElementTree.SubElement(root, "artifactId").text = artifact
    versioning = ElementTree.SubElement(root, "versioning")
    ElementTree.SubElement(versioning, "latest").text = newest
    ElementTree.SubElement(versioning, "release").text = newest
    holder = ElementTree.SubElement(versioning, "versions")
    for item in versions:
        ElementTree.SubElement(holder, "version").text = item
    ElementTree.SubElement(versioning, "lastUpdated").text = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    ElementTree.indent(root)
    body = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ElementTree.tostring(root, encoding="utf-8") + b"\n"
    return body, versions


# ---------------------------------------------------------------------------------------------
# the bucket

@dataclass
class Path_:
    """One object a publish writes: a file of the publication (``source``) or bytes it makes (a checksum)."""

    key: str
    name: str
    sha256: str
    bytes: int
    md5: str
    content_type: str
    source: object | None = None   # the file's Source, read from its path
    data: bytes | None = None

    @classmethod
    def of(cls, key: str, name: str, data: bytes, kind: str = "text/plain") -> "Path_":
        return cls(key, name, hashlib.sha256(data).hexdigest(), len(data), hashlib.md5(data).hexdigest(), kind,
                   data=data)


class Mirror(_Client):
    def __init__(self, *args):
        super().__init__(*args)
        settings = self.channel.settings
        self.secret_key, self.key_id = self.token, self.status_token
        self.bucket, self.public, self.region = settings["bucket"], settings["public_url"], settings["region"]
        self.group, self.artifact = self.channel.project.split(":", 1)
        self.root = f"{self.group.replace('.', '/')}/{self.artifact}"

    # -- requests --------------------------------------------------------------------------------
    def _signed(self) -> bool:
        return bool(self.key_id and self.secret_key)

    def _s3(self, method: str, key: str, *, body=None, length: int | None = None, sha256: str | None = None,
            headers: dict | None = None, missing: bool = False):
        if not self._signed():
            raise EnvironmentProblem(f"{self.channel.name} needs its access key id ({self.channel.where}."
                                     "key_id_secret) and secret access key (secret) mounted")
        url = f"{self.api}/{self.bucket}/{_encode(key, '/' + UNRESERVED)}"
        extra = dict(headers or {})
        extra.update(sign(method, url, key_id=self.key_id, secret=self.secret_key, region=self.region,
                          payload_sha256=sha256 or EMPTY, headers=extra))
        return self.http.request(method, url, headers=extra, body=body, length=length, missing=missing)

    def _public(self, key: str, *, fresh: bool = False):
        url = f"{self.public}/{_encode(key, '/' + UNRESERVED)}"
        if fresh:  # a query of its own, so a cache in front of the bucket answers from the bucket
            url += f"?wb-verify={secrets.token_hex(8)}"
        return self.http.request("GET", url, missing=True)

    def _held(self, key: str) -> dict | None:
        """What the bucket holds at ``key``: bytes, sha256 and ETag; None when nothing."""
        if not self._signed():
            answer = self._public(key, fresh=True)
            if answer.status == 404:
                return None
            return {"bytes": len(answer.body), "sha256": hashlib.sha256(answer.body).hexdigest(), "etag": None}
        answer = self._s3("HEAD", key, missing=True)
        if answer.status == 404:
            return None
        head = {k.lower(): v for k, v in answer.headers.items()}
        held = {"bytes": int(head["content-length"]) if str(head.get("content-length", "")).isdigit() else None,
                "sha256": str(head.get("x-amz-meta-sha256") or "").lower() or None,
                "etag": str(head.get("etag") or "").strip('"') or None}
        if not held["sha256"]:  # written by something else: read it and hash it, rather than guess from the ETag
            body = self._s3("GET", key).body
            held.update(sha256=hashlib.sha256(body).hexdigest(), bytes=len(body))
        return held

    def _metadata(self) -> tuple[bytes | None, str | None]:
        key = f"{self.root}/maven-metadata.xml"
        answer = self._s3("GET", key, missing=True) if self._signed() else self._public(key, fresh=True)
        if answer.status == 404:
            return None, None
        etag = {k.lower(): v for k, v in answer.headers.items()}.get("etag")
        return answer.body, etag

    def versions(self) -> list[str] | None:
        body, _ = self._metadata()
        return None if body is None else listed_versions(body, self.group, self.artifact)

    # -- what the bucket holds -------------------------------------------------------------------
    def holdings(self, version: str, settings: dict, names: list[str]) -> Holdings:
        folder = f"{self.root}/{version}"
        planned = {f["name"]: f for f in settings.get("files") or []}
        files, other, same = [], [], 0
        for name in names or [f"{self.artifact}-{version}.pom", f"{self.artifact}-{version}.jar"]:
            key = f"{folder}/{name}"
            held = self._held(key)
            if held is None:
                continue
            files.append({"name": name, "url": f"{self.public}/{key}", "bytes": held["bytes"],
                          "sha256": held["sha256"]})
            wanted = planned.get(name)
            if wanted is not None:
                if held["sha256"] == wanted["sha256"]:
                    same += 1
                else:
                    other.append(f"{self.channel.name} already holds {key} with other bytes (sha256 "
                                 f"{str(held['sha256'])[:12]}, not {wanted['sha256'][:12]}); nothing was uploaded")
        listed = self.versions() or []
        release = {"coordinates": f"{self.channel.project}:{version}", "folder": folder,
                   "url": f"{self.public}/{folder}", "in_metadata": version in listed}
        conflicts = list(other)
        if planned and not other and same == len(planned) and release["in_metadata"]:
            conflicts.append(f"{self.channel.name} already holds {self.channel.project}:{version} with these exact "
                             "bytes; nothing to publish")
        return Holdings(exists=bool(files) or release["in_metadata"], release=release, files=files,
                        conflicts=conflicts)

    # -- publishing ------------------------------------------------------------------------------
    def _paths(self, version: str, sources: list, algos: list[str]) -> list[Path_]:
        folder = f"{self.root}/{version}"
        out = []
        for source in sources:
            key = f"{folder}/{source.name}"
            out.append(Path_(key, source.name, source.sha256, source.bytes, source.md5, content_type(source.name),
                             source=source))
            out += [Path_.of(f"{key}.{algo}", f"{source.name}.{algo}", getattr(source, algo).encode("ascii"))
                    for algo in algos]
        return out

    def _put(self, path: Path_, condition: dict):
        headers = {"Content-Type": path.content_type, "x-amz-meta-sha256": path.sha256, **condition}
        try:
            if path.source is not None:
                with open(Path(path.source.path), "rb") as handle:
                    self._s3("PUT", path.key, body=handle, length=path.bytes, sha256=path.sha256, headers=headers)
            else:
                self._s3("PUT", path.key, body=path.data, length=path.bytes, sha256=path.sha256, headers=headers)
        except ChannelError as error:
            if error.status == 412:
                raise ChannelError(f"{path.key} was written by something else during the publish; it was not "
                                   "replaced", 412) from None
            raise

    def publish(self, ctx, plan: dict, sources: list, notes: str, receipt: Receipt):
        version = plan["version"]
        algos = plan["maven"]["checksums"]
        paths = self._paths(version, sources, algos)
        receipt.release = {"coordinates": f"{self.channel.project}:{version}", "bucket": self.bucket,
                           "url": f"{self.public}/{self.root}/{version}", "written": [], "kept": []}
        ctx.progress("survey", msg=f"reading the {len(paths)} paths of {version} in {self.bucket}")
        plan_of = {}
        for path in paths:
            ctx.check_cancel()
            held = self._held(path.key)
            if held is not None and held["sha256"] != path.sha256:
                raise ChannelError(f"{path.key} already holds other bytes (sha256 {str(held['sha256'])[:12]}, not "
                                   f"{path.sha256[:12]}); a published path is never replaced, so nothing was "
                                   "uploaded")
            plan_of[path.key] = "keep" if held is not None else "write"
        for index, path in enumerate(paths):
            ctx.check_cancel()
            if plan_of[path.key] == "keep":
                receipt.release["kept"].append(path.key)
            else:
                ctx.progress("upload", index / len(paths), f"{path.name} ({path.bytes} bytes)")
                self._put(path, {"If-None-Match": "*"})
                receipt.release["written"].append(path.key)
            if path.source is not None:
                receipt.add(path.source, url=f"{self.public}/{path.key}")
                receipt.files[-1]["kept"] = plan_of[path.key] == "keep"
        ctx.progress("metadata", 1.0, "maven-metadata.xml")
        metadata = self._merge(version, algos, receipt)
        self._verify(ctx, paths + metadata, receipt)

    def _merge(self, version: str, algos: list[str], receipt: Receipt) -> list[Path_]:
        """maven-metadata.xml with the version added, written only if it was not listed."""
        key = f"{self.root}/maven-metadata.xml"
        existing, etag = self._metadata()
        body, versions = merged_metadata(existing, self.group, self.artifact, version)
        receipt.release["metadata"] = {"url": f"{self.public}/{key}", "versions": versions}
        listed = version in listed_versions(existing, self.group, self.artifact)
        document = Path_.of(key, "maven-metadata.xml", existing if listed else body, "application/xml")
        sums = [Path_.of(f"{key}.{algo}", f"maven-metadata.xml.{algo}",
                         hashlib.new(algo, document.data, usedforsecurity=False).hexdigest().encode("ascii"))
                for algo in algos]
        if listed:
            receipt.release["kept"].append(key)
            return [document]  # its checksums are another writer's to keep in step
        self._put(document, {"If-Match": etag} if etag else {"If-None-Match": "*"})
        receipt.release["written"].append(key)
        for path in sums:
            self._put(path, {})
            receipt.release["written"].append(path.key)
        return [document, *sums]

    def _verify(self, ctx, paths: list[Path_], receipt: Receipt):
        """Every path read back from the public base URL; the receipt says what was served."""
        served, mismatched = {}, []
        for path in paths:
            ctx.check_cancel()
            answer = None
            for attempt in range(VERIFY_TRIES):
                answer = self._public(path.key, fresh=True)
                if answer.status != 404:
                    break
                if attempt + 1 < VERIFY_TRIES:
                    ctx.sleep(VERIFY_WAIT)
            if answer.status == 404:
                mismatched.append(f"{self.public}/{path.key} is not served (HTTP 404)")
                continue
            digest = hashlib.sha256(answer.body).hexdigest()
            served[path.key] = {"sha256": digest, "bytes": len(answer.body)}
            if digest != path.sha256:
                mismatched.append(f"{self.public}/{path.key} serves other bytes (sha256 {digest[:12]}, not "
                                  f"{path.sha256[:12]})")
        for entry in receipt.files:
            key = entry["url"][len(self.public) + 1:]
            remote = served.get(key)
            entry["verified"], entry["verified_by"] = (
                (remote["sha256"] == entry["sha256"] and remote["bytes"] == entry["bytes"], "sha256") if remote
                else (False, "absent"))
            if remote:
                entry["remote"] = remote
        receipt.release["verified"] = len(paths) - len(mismatched)
        if mismatched:
            receipt.release["mismatched"] = mismatched
        if not served and paths:
            raise ChannelError(f"nothing published is served from {self.public}: {excerpt(mismatched[0].encode())}")
