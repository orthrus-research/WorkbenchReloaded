# Licensing of the former standalone tools

Original code, configuration and documentation of the former standalone tools are offered under
[NRR 1.0](LICENSE), except material with a separate file, directory or upstream license.
NRR requires no permission, attribution, retained notices or source disclosure for the
material it covers. Its public-domain dedication and fallback grant apply only to rights
the contributor owns or can grant.

`LicenseRef-NRR-1.0` is this project's custom identifier. NRR is a new license; it is not
claimed to be an SPDX-listed or OSI-approved license, or a guarantee of identical legal
effect in every jurisdiction.

## Provenance

Every tool here began in an Orthrus Research project and was reworked for the former standalone tools (0.6.0):
generalized for Windows and Linux, stripped of project paths and given its own tests. Each
source file was original Orthrus work in its project, offered here under NRR 1.0.

| Tool | Origin |
| --- | --- |
| `frames`, `profiles/` | Quadrature (NRR 1.0) |
| `mod_survey`, `classfile` | Quadrature (NRR 1.0); the class file reader was split out of the survey |
| `strict_math` | Riven (NRR 1.0) |
| `pins`, `jdk` | Riven's setup script (NRR 1.0) and Cubature's build-input fetcher, both original fork work |
| `stats`, `workload_guard`, `process_lifecycle`, `processes/windows` | Cubature's measurement lab, original fork work not derived from upstream CubicChunks |
| `processes/linux` | Written for this repository |
| `scrub` | Written for this repository from the commit rules every Orthrus project states |

Third-party material, when added, keeps its own license and is listed here with its source.
