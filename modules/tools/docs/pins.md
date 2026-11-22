# orthrus-pins

Downloads the third-party files a project needs but does not commit (JARs, archives,
installers) and refuses any whose SHA-256 differs from the lock file.

## Use

```sh
orthrus-pins add Demonica-0.9.0.jar https://github.com/.../Demonica-0.9.0.jar --path build-inputs/Demonica-0.9.0.jar
orthrus-pins fetch            # download what is missing or differs; idempotent
orthrus-pins fetch --only Demonica-0.9.0.jar
orthrus-pins verify           # offline: exit 1 if anything is missing or differs
orthrus-pins hash some.jar
```

`add` trusts the file it downloads the first time, so compare the printed digest with one the
publisher states before committing the lock file.

Exit status: 0 done or all present, 1 a pin is missing or differs (`verify`) or a download does
not match its hash, 2 no lock file or a bad entry, 3 the download failed after three tries.

## Lock file

```json
{
  "pins": [
    {
      "name": "Demonica-0.9.0.jar",
      "url": "https://github.com/.../Demonica-0.9.0.jar",
      "sha256": "241632b9...",
      "path": "build-inputs/Demonica-0.9.0.jar"
    }
  ]
}
```

- `url` must be `https://`.
- `path` is relative to the project root and may not leave it; without it a pin goes to
  `<dest>/<name>`, where `dest` defaults to `.local/pins`.
- Names are unique.

Downloads go to a `.part` file and replace the target only once the digest matches. Network
errors are retried three times with backoff. Proxy settings come from the usual `HTTPS_PROXY`
environment variable.

## The pin cache

When `WB_PIN_CACHE` names a folder, `fetch` takes a pin from there first (a file named by its SHA-256, checked again
before use) and keeps every verified download there, so a machine downloads each pinned file once however many
clean trees need it. Workbench sets it for every CI job. Other tools that build or download pinned files can use the
same folder the same way: look up `<cache>/<sha256>` first, and store what they made under its digest.

## Settings

```toml
# orthrus.toml
[pins]
lock = "dev/pins.lock.json"   # default: pins.lock.json
dest = "dev/pins"             # default: .local/pins
```
