"""What a base is, and the placeholders the builder fills in one.

A base is what every instance of a runtime starts from before its members are placed: how the server is installed,
the files the server and client folders get, the JVM profile, the player, and the command lines that start each side.
A launch names one with ``base = '<project>:<name>'``, and lab.resolve reads that project's ``[launch.<name>]`` (with
its ``launch/`` overlay folder) at the commit the set names for it, into launch.json. A launched product's own changes
are never a base's: they are its configure script.

Workbench keeps no copy of any base: a base project whose commit has no such table is refused by lab.resolve, and a
launch file that names no base is refused by lab.build (resolve it again). Cubature's standalone world, the base of
every launch in use, is its own project.toml's ``[launch.standalone]`` with its ``launch/standalone`` overlay. A
base's keys are ``wb_lab.resolve.BASE_KEYS``.

Placeholders, filled by the builder:

- in file texts: ``{port}``, ``{seed}``, ``{player}``, ``{player_uuid}``;
- in ``installer``: ``{java}``, ``{installer}``, ``{target}``, ``{version}``, ``{memory}``;
- in commands: ``{java}``, ``{game}`` (the side's folder), ``{cache}``, ``{version}``, ``{classpath}``,
  ``{main_class}``, ``{player}``, ``{player_uuid}``, ``{assets}``, ``{asset_index}``, ``{port}``, ``{args_file}``,
  and three that stand alone and are replaced by a list: ``{jvm}`` (the launch's JVM arguments for the side),
  ``{profile}`` (the base's JVM profile) and ``{tweakers}`` (``--tweakClass <name>`` per tweaker the runtime names).
"""
