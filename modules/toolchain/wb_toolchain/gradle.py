"""The Gradle init script that serves the rescue repository to a build and applies the substitutions.

The repository is added as a Maven repository, first, to the plugin repositories, to each project's buildscript
repositories and to the repositories its dependencies resolve from (the settings' central ones when the build keeps
its repositories there). Gradle asks it only for the exact versions it holds (a content filter, Gradle 5.1 and
newer), so it never stands in for an artifact the public repositories still serve, and a dead or failing host never
gets the chance to fail a build over coordinates the repository holds. An older Gradle gets it unfiltered and last.
Plugin repositories that were empty, meaning the Gradle Plugin Portal, keep the portal.

A project that resolves from the settings' repositories gets it there; such a project gets it among its own only once
it declares repositories of its own, which is when Gradle stops falling back to the settings'.

Substitutions apply to every configuration of every project and its buildscript, through
``resolutionStrategy.dependencySubstitution``; a plugin marker's coordinates (``<id>:<id>.gradle.plugin:<version>``)
name the plugin request ``id '<id>' version '<version>'`` instead, rewritten in ``pluginManagement.resolutionStrategy``
to the substitute's module (``useModule``), or to the substitute marker's version (``useVersion``).

Plugins the settings script applies itself resolve before the script runs (``settingsEvaluated``) and are not served.
"""

from __future__ import annotations

from .maven import Coordinates, CoordinatesError

NAME = "Workbench rescue repository"
HEADER = "// Written by Workbench (toolchain.repo-init)"

TEMPLATE = r"""{header}: the rescue repository and the coordinate substitutions, from Workbench's config
// and the repository's receipts. Workbench writes this file again for every run; edits here do not last.
import org.gradle.util.GradleVersion

def wbRepository = new File('{location}')
def wbName = '{name}'
// The versions the repository holds: Gradle asks it for these and nothing else.
def wbHeld = [{held}]
// Vanished coordinates, and the coordinates builds get instead.
def wbModules = [{modules}]
// Plugin requests (id, version), and how they resolve instead: a module, or another version.
def wbPlugins = [{plugins}]
def wbGradle = GradleVersion.current().baseVersion
def wbFiltered = wbGradle >= GradleVersion.version('5.1')
def wbProjects = true   // the projects' own repositories are where their dependencies resolve
def wbFallback = false  // projects that declare none resolve from the settings' repositories

def wbServe = {{ repositories ->
    if (wbHeld.isEmpty() || repositories.findByName(wbName) != null) {{
        return
    }}
    def repository = repositories.maven {{ maven ->
        maven.name = wbName
        maven.url = wbRepository.toURI()
        if (wbFiltered) {{
            maven.content {{ content -> wbHeld.each {{ held -> content.includeVersion(held[0], held[1], held[2]) }} }}
        }}
    }}
    if (wbFiltered) {{  // it answers only for what it holds, so it goes first
        repositories.remove(repository)
        repositories.addFirst(repository)
    }}
}}

def wbSubstitute = {{ configurations ->
    if (wbModules.isEmpty()) {{
        return
    }}
    configurations.all {{ configuration ->
        configuration.resolutionStrategy.dependencySubstitution {{ rules ->
            wbModules.each {{ rule ->
                def substitution = rules.substitute(rules.module(rule[0]))
                if (wbGradle >= GradleVersion.version('6.6')) {{
                    substitution.using(rules.module(rule[1])).because(rule[0] + ' vanished: Workbench substitutes ' + rule[1])
                }} else {{
                    substitution.with(rules.module(rule[1]))
                }}
            }}
        }}
    }}
}}

settingsEvaluated {{ settings ->
    def plugins = settings.pluginManagement
    if (!wbHeld.isEmpty() && plugins.repositories.isEmpty() && wbGradle >= GradleVersion.version('4.4')) {{
        plugins.repositories.gradlePluginPortal()  // what an empty list meant
    }}
    wbServe(plugins.repositories)
    if (!wbPlugins.isEmpty()) {{
        plugins.resolutionStrategy.eachPlugin {{ details ->
            wbPlugins.each {{ rule ->
                if (details.requested.id.id == rule[0] && details.target.version == rule[1]) {{
                    if (rule[2] == 'module') {{
                        details.useModule(rule[3])
                    }} else {{
                        details.useVersion(rule[3])
                    }}
                }}
            }}
        }}
    }}
    if (wbGradle >= GradleVersion.version('6.8')) {{
        def central = settings.dependencyResolutionManagement
        def mode = central.repositoriesMode.get().toString()
        if (mode != 'PREFER_PROJECT' || !central.repositories.isEmpty()) {{
            wbServe(central.repositories)
        }}
        wbProjects = mode == 'PREFER_PROJECT'
        wbFallback = !central.repositories.isEmpty()
    }}
}}

beforeProject {{ project ->
    wbSubstitute(project.buildscript.configurations)
    wbSubstitute(project.configurations)
    wbServe(project.buildscript.repositories)
    if (!wbProjects) {{
        return
    }}
    if (!wbFallback || !project.repositories.isEmpty()) {{
        wbServe(project.repositories)
    }} else {{  // served once the project declares repositories, so it keeps the settings' until then
        project.repositories.whenObjectAdded {{ added ->
            if (added.name != wbName) {{
                wbServe(project.repositories)
            }}
        }}
    }}
}}
"""


def _literal(text: str) -> str:
    """A Groovy single-quoted string literal's content: backslashes and quotes escaped, the rest ASCII."""
    out = []
    for char in str(text):
        if char in "\\'":
            out.append("\\" + char)
        elif " " <= char <= "~":
            out.append(char)
        else:
            raw = char.encode("utf-16-be")
            out += [f"\\u{raw[i]:02x}{raw[i + 1]:02x}" for i in range(0, len(raw), 2)]
    return "".join(out)


def _rows(rows: list[tuple]) -> str:
    if not rows:
        return ""
    lines = ["[" + ", ".join(f"'{_literal(value)}'" for value in row) + "]" for row in rows]
    return "\n    " + ",\n    ".join(lines) + "\n"


def rules(substitutions: list[tuple[Coordinates, Coordinates]]) -> tuple[list[tuple], list[tuple]]:
    """The substitutions as plugin rules (id, version, module|version, target) and module rules (from, to)."""
    plugins, modules = [], []
    for source, target in substitutions:
        plugin = source.plugin
        if plugin is None:
            modules.append((source.module, target.module))
        elif target.plugin is None:
            plugins.append((plugin, source.version, "module", target.module))
        elif target.plugin == plugin:
            plugins.append((plugin, source.version, "version", target.version))
        else:
            raise CoordinatesError(f"{source.module} = {target.module}: a plugin request cannot become another "
                                   f"plugin ({target.plugin}); substitute the module that implements {plugin}")
    return sorted(plugins), sorted(modules)


def init_script(location: str, held: list[tuple[str, str, str]],
                substitutions: list[tuple[Coordinates, Coordinates]]) -> str:
    """The init script for builds that see the repository at ``location``. Empty when there is nothing to serve."""
    plugins, modules = rules(substitutions)
    if not held and not substitutions:
        return ""
    return TEMPLATE.format(header=HEADER, location=_literal(location.replace("\\", "/")), name=NAME,
                           held=_rows(sorted(held)), modules=_rows(modules), plugins=_rows(plugins))
