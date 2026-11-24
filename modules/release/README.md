# release: Maven Central and the mirror

`release.publish` sends one version's files to one channel, after an explicit yes on its plan (see
`wb_release/commands.py`). This page covers the two Maven routes beside the plain `maven` kind: `central` for Maven
Central, through the Central Publisher Portal, and `mirror` for our own public Maven repository at
https://maven.orthrus.cc. The other kinds are described in `wb_release/channels.py`.

Both lay a version out as Maven does, under `<group path>/<artifact>/<version>/`, and both take the files in their
Maven names: `<artifact>-<version>.jar`, `<artifact>-<version>-sources.jar`, and so on (a build's `pom-default.xml`
and `module.json` are renamed by role). A POM or Gradle module file among the files must be of the channel's
coordinates.

## central

What a publish does, in order:

1. The quote reads local files only. Workbench writes the POM it will publish: the build's POM when one is among the
   files (its coordinates, packaging and dependencies) with the channel's `pom` metadata and the rules' `licence`
   set in it, or a POM of the coordinates alone. Central needs the JAR, its `-sources.jar` and its `-javadoc.jar`;
   with `javadoc = "placeholder"` Workbench writes a javadoc JAR holding only a README, and the plan says so. The
   written files are in the plan with their SHA-256, like every other file.
2. After the yes, it asks the Portal whether Central has published the version, and stops if it has, or if the
   Portal does not answer.
3. It signs every file with the channel's key: `gpg --batch --pinentry-mode loopback --detach-sign --armor`, the
   gpg on PATH, the passphrase given on gpg's standard input. With `signing.keyring`, the armored secret key is
   imported into a throwaway gpg home that is removed afterwards; without it, gpg's own home must hold the key.
   Each signature is verified to be by that key.
4. It builds the bundle (every file, its `.md5`, `.sha1`, `.sha256` and `.sha512`, and its `.asc`), uploads it with
   the user token, and polls the deployment until it is `VALIDATED` (`USER_MANAGED`, the default: a person then
   presses Publish in the Portal, which Workbench never does) or `PUBLISHED` (`AUTOMATIC`), or `FAILED`.
5. The receipt keeps the deployment id, its last state, the Portal's errors when it failed, and the bundle's
   SHA-256.

## mirror

What a publish does, in order:

1. Every request goes to the bucket's S3 API, signed with Signature Version 4 by the access key from Workbench
   secrets. The channel publishes its `group` and that group's subgroups only, and refuses any other at load.
2. Before writing anything, it reads every path of the version (each file and its four checksum files). A path
   that holds the same bytes is kept as it is; a path that holds other bytes stops the publish, so a published file
   is never replaced. A version whose every file is already there with the same bytes, and listed in the metadata,
   is reported as held and nothing is written.
3. It writes the missing paths, each with `If-None-Match: *`, so a writer racing the publish is refused rather
   than overwritten. Then it merges `maven-metadata.xml` with what the bucket holds (every version listed stays;
   latest and release name the newest) and writes it with `If-Match` on the ETag it read, then its checksums.
4. It reads every path back from `public_url`, past any cache, and records in the receipt whether each file served
   is the file published. A path served with other bytes is a finding.

## Config

Placeholders are in angle brackets. Secrets are files named in `[secrets.<name>]`; a channel names a secret, never
its value.

```toml
[secrets.release_central]          # the Portal user token, as <token username>:<token password>
host = '<file>'
[secrets.release_signing_passphrase]
host = '<file>'
[secrets.release_signing_key]      # the armored secret key: gpg --armor --export-secret-keys <FINGERPRINT>
host = '<file>'
[secrets.release_mirror_key_id]    # the bucket's access key id
host = '<file>'
[secrets.release_mirror_secret]    # the bucket's secret access key
host = '<file>'

[modules.release.rules.<rules name>]
java = 8
production = ["<artifact>-{version}.jar"]
dependencies = ["<group>:*"]
licence = { name = "<licence name>", url = "<licence URL>" }   # the licence the POM declares

[modules.release.channels.central]
kind = "central"
base_url = "https://central.sonatype.com"
project = "cc.orthrus:<artifact>"
secret = "release_central"
rules = "<rules name>"
publishing_type = "USER_MANAGED"   # or "AUTOMATIC"
javadoc = "built"                  # or "placeholder": a javadoc JAR holding only a README
signing = { key = "<FINGERPRINT>", secret = "release_signing_passphrase", keyring = "release_signing_key" }
# poll_interval = "10s", poll_timeout = "40m"; checksums = ["md5", "sha1", "sha256", "sha512"]

[modules.release.channels.central.pom]   # the POM's metadata
name = "<name>"
description = "<description>"
url = "<project URL>"
developers = [{ id = "<id>", name = "<name>", url = "<profile URL>" }]
scm = { url = "<repository URL>", connection = "scm:git:<clone URL>", developer_connection = "scm:git:<push URL>" }

[modules.release.channels.mirror]
kind = "mirror"
base_url = "<the account's S3 API endpoint>"
bucket = "orthrus-maven"
public_url = "https://maven.orthrus.cc"
group = "cc.orthrus"
project = "cc.orthrus:<artifact>"
key_id_secret = "release_mirror_key_id"
secret = "release_mirror_secret"
rules = "<rules name>"
# region = "auto"; checksums = ["md5", "sha1", "sha256", "sha512"]
```

Then `wb run release.publish channel=central version=<version> 'files=[...]'` (or `set=<project>@<revision>`)
quotes the plan, and nothing is sent before `wb approve`. `release.status channel=<name> version=<version>` says
what the channel holds, and `release.check channel=<name>` checks files as a publish to it would, with no network.

## Before a first publish

- Central: a Portal account whose namespace `cc.orthrus` is verified, and a user token generated in the Portal
  (its username and password go in one secret file, joined by a colon).
- A signing key whose public key is on a key server Central reads, its full fingerprint in `signing.key`, its
  passphrase in a secret file, and its armored secret key in another (`signing.keyring`). The image carries gpg;
  rebuild it after updating if it was built before.
- The mirror: an API token for the bucket with object read and write, whose access key id and secret access key go
  in two secret files, the account's S3 endpoint in `base_url`, and the bucket served publicly at `public_url`.
- A `licence` in the rules the central channel names.
