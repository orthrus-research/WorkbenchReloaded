"""wb mcp: the protocol in both its eras, the skills as a client verifies them, the tools against a daemon, approval
that only the person gives, and the server over real pipes."""

import contextlib
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import REPO, Daemon  # noqa: E402

from wb import cli, mcp  # noqa: E402

META = {mcp.META_VERSION: "2026-07-28", mcp.META_CLIENT: {"name": "test client", "version": "1"},
        mcp.META_CAPABILITIES: {}}
ASKS = {**META, mcp.META_CAPABILITIES: {"elicitation": {}}}


def request(method, params=None, meta=META, ident=1):
    params = dict(params or {})
    if meta is not None:
        params["_meta"] = meta
    return {"jsonrpc": "2.0", "id": ident, "method": method, "params": params}


class Frontmatter(unittest.TestCase):
    def test_plain_and_quoted_strings_are_read_as_every_yaml_parser_reads_them(self):
        text = '---\nname: start\ndescription: "Run it: then read it"\nlicense: Apache-2.0\n---\n# Body\nkey: not read\n'
        self.assertEqual(mcp.frontmatter(text),
                         {"name": "start", "description": "Run it: then read it", "license": "Apache-2.0"})
        self.assertEqual(mcp.frontmatter("---\r\nname: a\r\ndescription: b c\r\n---\r\n"), {"name": "a", "description": "b c"})
        self.assertEqual(mcp.frontmatter('---\nname: x\ndescription: "\u00dcber caf\u00e9, 3 of them"\n---\n')["description"],
                         "\u00dcber caf\u00e9, 3 of them")
        self.assertEqual(mcp.frontmatter("---\nname: x\ndescription: Caf\u00e9 tools - a project's own\n---\n")["description"],
                         "Caf\u00e9 tools - a project's own")

    def test_a_value_with_another_reading_is_refused_and_not_guessed(self):
        typed = ("true", "12", "1.5", ".5", "+.5", "0b1010", "+0x1F", "2026-11-24", "2026-11-24T10:00:00Z", "null",
                 "y", "N", "Off", "~", "=", "<<", "-fast", "3d tools")
        shaped = ("[a, b]", "{a: b}", "a: b", "a #b", "'quoted'", "|", "> folded", "*alias", '"open', "", "ends:")
        unusual = ("a\tb", "Run it\u00a0", "Run\u2028it", "Run\x0cthe checks", "Run\x85it", "\u00dcber",
                   '"smile \\ud83d\\ude00"', '"a\\nb"', '"bell \x07"')
        for value in typed + shaped + unusual:
            with self.subTest(value=value):
                with self.assertRaises(mcp.SkillError):
                    mcp.frontmatter(f"---\nname: x\ndescription: {value}\n---\n")

    def test_a_block_that_is_missing_open_nested_or_repeated_is_refused(self):
        for text in ("# no frontmatter\n", "---\nname: x\ndescription: y\n", "---\nname: x\nmetadata:\n  a: b\n---\n",
                     "---\nname: x\nname: y\n---\n", "---\n- name\n---\n", "---\nname:\tx\n---\n", "---\non: x\n---\n",
                     "---\nYes: x\n---\n", "\ufeff---\nname: x\n---\n", ""):
            with self.subTest(text=text):
                with self.assertRaises(mcp.SkillError):
                    mcp.frontmatter(text)


class ShippedSkills(unittest.TestCase):
    """What Workbench ships, held to the Skills extension's and the skill format's rules."""

    @classmethod
    def setUpClass(cls):
        cls.skills = mcp.Skills()
        cls.entries = {entry["frontmatter"]["name"]: entry for entry in cls.skills.entries()}

    def test_every_skill_folder_is_served_under_its_own_name(self):
        folders = sorted(p.name for p in mcp.SKILLS.iterdir() if p.is_dir() and not p.name.startswith("_"))
        self.assertEqual(sorted(self.entries), folders)
        self.assertIn("start", self.entries)
        for name, entry in self.entries.items():
            with self.subTest(skill=name):
                self.assertRegex(name, r"^[a-z0-9]+(-[a-z0-9]+)*$")
                self.assertLessEqual(len(name), 64)
                self.assertEqual(entry["uri"], f"skill://{name}/SKILL.md")
                self.assertEqual(sorted(entry["frontmatter"]), ["description", "name"])
                self.assertTrue(20 <= len(entry["frontmatter"]["description"]) <= 1024)

    def test_each_file_is_served_as_the_bytes_its_entry_vouches_for(self):
        for name, entry in self.entries.items():
            uris = [item["uri"] for item in entry["resources"]]
            self.assertEqual(uris[0], entry["uri"])
            self.assertEqual(len(uris), len(set(uris)))
            self.assertLessEqual(len(uris), 512)
            self.assertLessEqual(sum(item["size"] for item in entry["resources"]), 16 * 2**20)
            for item in entry["resources"]:
                with self.subTest(uri=item["uri"]):
                    served = self.skills.read(item["uri"])
                    raw = served["text"].encode("utf-8")
                    self.assertEqual(len(raw), item["size"])
                    self.assertEqual("sha256:" + hashlib.sha256(raw).hexdigest(), item["digest"])
                    self.assertNotIn("\r", served["text"])  # one form of every file, whatever checked it out

    def test_a_skill_links_only_to_files_it_serves_and_names_only_tools_and_skills_that_exist(self):
        tools = {tool["name"] for tool in mcp.TOOLS}
        for name, entry in self.entries.items():
            served = {item["uri"] for item in entry["resources"]}
            for item in entry["resources"]:
                text = self.skills.read(item["uri"])["text"]
                base = item["uri"].rsplit("/", 1)[0]
                for link in re.findall(r"\]\(([^)#]+)\)", text):
                    if "://" not in link:
                        with self.subTest(file=item["uri"], link=link):
                            self.assertIn(f"{base}/{link}", served)
                for called in re.findall(r"\b([a-z]+)\((?:[a-z]+=|\))", text):
                    with self.subTest(file=item["uri"], tool=called):
                        self.assertIn(called, tools)
            body = self.skills.read(entry["uri"])["text"]
            self.assertLess(len(body.splitlines()), 200, f"{name}: a skill's first file stays short")
        for other in re.findall(r"^- `([a-z-]+)`:", self.skills.read("skill://start/SKILL.md")["text"], re.MULTILINE):
            self.assertIn(other, self.entries)
        for named in re.findall(r"`([a-z-]+)`", mcp.INSTRUCTIONS):
            self.assertIn(named, tools | set(self.entries))

    def test_the_verbs_cli_refuses_are_verbs_and_the_verbs_the_skills_teach_are_not_refused(self):
        verbs = set(cli.build_parser()._subparsers._group_actions[0].choices)
        self.assertLessEqual(set(mcp.REFUSED), verbs)
        taught = set(re.findall(r'cli\(args=\["([a-z]+)"', "".join(
            self.skills.read(item["uri"])["text"] for entry in self.entries.values() for item in entry["resources"])))
        self.assertTrue(taught)
        self.assertLessEqual(taught, verbs - set(mcp.REFUSED))


class SkillFolders(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wb-skills-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))

    def skill(self, name, text, **files):
        (self.root / name).mkdir(parents=True)
        (self.root / name / "SKILL.md").write_bytes(text.encode("utf-8"))
        for relative, data in files.items():
            path = self.root / name / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def test_a_skill_that_cannot_be_served_is_left_out_and_the_rest_are_listed(self):
        self.skill("good", "---\nname: good\ndescription: a good one\n---\nbody\n")
        self.skill("renamed", "---\nname: other\ndescription: named for another folder\n---\n")
        self.skill("typed", "---\nname: typed\ndescription: true\n---\n")
        self.skill("Bad_Name", "---\nname: Bad_Name\ndescription: not a skill name\n---\n")
        (self.root / "lower").mkdir()
        (self.root / "lower" / "skill.md").write_text("---\nname: lower\ndescription: the file is not SKILL.md\n---\n")
        skills = mcp.Skills(self.root)
        with contextlib.redirect_stderr(io.StringIO()) as said:
            self.assertEqual([entry["frontmatter"]["name"] for entry in skills.entries()], ["good"])
        self.assertIn("skill left out: renamed/SKILL.md names itself 'other'", said.getvalue())
        self.assertIn("skill left out: typed/SKILL.md: frontmatter line 3", said.getvalue())
        self.assertNotIn("lower", skills.names())
        with self.assertRaises(mcp.SkillError):
            skills.entry("renamed")
        server = mcp.Server(skills=skills)
        for uri in ("skill://typed/SKILL.md", "skill://lower/SKILL.md", "skill://lower/skill.md"):
            with self.subTest(uri=uri):  # a folder that is no skill as it stands: none is served there
                self.assertEqual(server.handle(request("skills/get", {"uri": uri}))["error"]["code"], -32602)
        with contextlib.redirect_stderr(io.StringIO()):
            listed = server.handle(request("skills/list"))["result"]["skills"]
        self.assertEqual([skill["uri"] for skill in listed], ["skill://good/SKILL.md"])

    def test_files_keep_their_bytes_binary_ones_as_base64_and_leftovers_are_not_served(self):
        self.skill("mixed", "---\nname: mixed\ndescription: text and bytes\n---\r\nwindows line\r\n",
                   **{"assets/blob.bin": b"\xff\x00\xfe", "__pycache__/x.pyc": b"x", ".hidden": b"x",
                      "scripts/run.py": b"print('hi')\n", "scripts/__init__.py": b""})
        skills = mcp.Skills(self.root)
        entry = skills.entry("mixed")
        self.assertEqual([item["uri"] for item in entry["resources"]],
                         ["skill://mixed/SKILL.md", "skill://mixed/assets/blob.bin", "skill://mixed/scripts/__init__.py",
                          "skill://mixed/scripts/run.py"])
        text = skills.read("skill://mixed/SKILL.md")
        self.assertIn("\r\n", text["text"])
        self.assertEqual(len(text["text"].encode()), entry["resources"][0]["size"])
        blob = skills.read("skill://mixed/assets/blob.bin")
        self.assertEqual((blob["blob"], blob["mimeType"]), ("/wD+", "application/octet-stream"))
        for outside in ("skill://mixed/.hidden", "skill://mixed/../good/SKILL.md", "skill://mixed/assets",
                        "skill://none/SKILL.md", "file:///etc/passwd", None, 7):
            self.assertIsNone(skills.read(outside))
        self.assertEqual([(c["name"], c["mimeType"]) for c in skills.children("skill://mixed")],
                         [("SKILL.md", "text/markdown"), ("assets", "inode/directory"), ("scripts", "inode/directory")])
        self.assertEqual([c["uri"] for c in skills.children("skill://mixed/scripts")],
                         ["skill://mixed/scripts/__init__.py", "skill://mixed/scripts/run.py"])
        self.assertIsNone(skills.children("skill://mixed/SKILL.md"))
        self.assertIsNone(skills.children("skill://mixed/nothing"))


class StubTools(mcp.Tools):
    """Tools that answer without a daemon: ``slow`` waits until it is cancelled or released."""

    def __init__(self):
        super().__init__(client=lambda: None)
        self.release, self.began = threading.Event(), threading.Event()

    def call(self, name, arguments, call):
        if name == "slow":
            call.tell("working")
            self.began.set()
            while not (self.release.is_set() or call.cancelled.is_set()):
                self.release.wait(0.01)
            call.tell("a word too late" if call.cancelled.is_set() else "nearly done")
            return mcp.Answer("done")
        if name == "boom":
            raise RuntimeError("broken tool")
        return super().call(name, arguments, call)


class Protocol(unittest.TestCase):
    def setUp(self):
        self.tools = StubTools()
        self.server = mcp.Server(tools=self.tools)
        self.sent = []
        self.server._send = self.sent.append

    def result(self, method, params=None, meta=META):
        response = self.server.handle(request(method, params, meta))
        self.assertNotIn("error", response, response)
        return response["result"]

    def error(self, method, params=None, meta=META):
        return self.server.handle(request(method, params, meta))["error"]

    def test_discover_names_the_versions_the_skills_extension_and_the_server(self):
        found = self.result("server/discover")
        self.assertEqual(found["resultType"], "complete")
        self.assertEqual(found["supportedVersions"], ["2026-07-28"])
        self.assertEqual(found["capabilities"], {"tools": {}, "resources": {}, "extensions": {
            "io.modelcontextprotocol/skills": {"directoryRead": True}}})
        self.assertEqual(found["_meta"][mcp.META_SERVER]["name"], "workbench")
        self.assertIn("skill://start/SKILL.md", found["instructions"])
        self.assertLess(len(found["instructions"]), 1000)
        self.assertEqual((found["cacheScope"], found["ttlMs"] > 0), ("public", True))

    def test_a_modern_request_says_its_version_and_capabilities_each_time(self):
        for asked in ("2099-01-01", "2025-11-25"):  # one it does not know, and one that is opened with initialize
            other = self.error("tools/list", meta={**META, mcp.META_VERSION: asked})
            self.assertEqual((other["code"], other["data"]), (-32022, {"supported": ["2026-07-28"], "requested": asked}))
        bare = {mcp.META_VERSION: "2026-07-28"}
        self.assertEqual(self.error("tools/list", meta=bare)["code"], -32602)
        for method in ("server/discover", "tools/list", "resources/templates/list"):  # no version, and no handshake
            with self.subTest(method=method):
                self.assertEqual(self.error(method, meta=None)["code"], -32602)
        self.assertEqual(self.error("tools/list", meta={mcp.META_CAPABILITIES: {}})["code"], -32602)
        self.assertEqual(self.result("tools/list")["resultType"], "complete")  # nothing was remembered against it

    def test_a_client_that_opens_with_initialize_is_served_as_its_revision_expects(self):
        opened = self.result("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                            "clientInfo": {"name": "old", "version": "1"}}, meta=None)
        self.assertEqual(opened["protocolVersion"], "2025-06-18")
        self.assertEqual(opened["capabilities"], {"tools": {}, "resources": {}})
        self.assertNotIn("resultType", opened)
        self.assertEqual(self.result("initialize", {"protocolVersion": "1999-01-01"}, meta=None)["protocolVersion"],
                         "2025-11-25")
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        listed = self.result("tools/list", meta=None)
        self.assertEqual(sorted(listed), ["tools"])  # no fields a client of those revisions does not know
        self.assertEqual(self.result("ping", meta=None), {})
        self.assertEqual(sorted(self.result("resources/read", {"uri": "skill://start/SKILL.md"}, meta=None)), ["contents"])
        self.assertEqual(self.error("resources/read", {"uri": "skill://start/none.md"}, meta=None)["code"], -32002)
        self.assertEqual(self.error("server/discover", meta=None)["code"], -32602)  # still the modern probe only
        self.assertEqual(self.result("tools/list")["resultType"], "complete")       # and both are served at once

    def test_tools_are_few_described_briefly_and_listed_in_one_order(self):
        listed = self.result("tools/list")
        names = [tool["name"] for tool in listed["tools"]]
        self.assertEqual(names, ["commands", "describe", "run", "job", "jobs", "logs", "cancel", "approve", "status", "cli"])
        self.assertLess(len(json.dumps(listed["tools"])), 8000)  # what a client holds before any skill is loaded
        for tool in listed["tools"]:
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertLess(len(tool["description"]), 330, tool["name"])
        readers = {tool["name"] for tool in listed["tools"] if tool["annotations"].get("readOnlyHint")}
        self.assertEqual(readers, {"commands", "describe", "job", "jobs", "logs", "status"})

    def test_skills_are_listed_whole_fetched_one_by_one_and_read_as_resources(self):
        listed = self.result("skills/list")
        self.assertEqual((listed["resultType"], listed["cacheScope"]), ("complete", "public"))
        entry = next(skill for skill in listed["skills"] if skill["frontmatter"]["name"] == "start")
        self.assertEqual(self.result("skills/get", {"uri": entry["uri"]})["skill"], entry)
        read = self.result("resources/read", {"uri": entry["uri"]})
        self.assertEqual((read["contents"][0]["mimeType"], read["ttlMs"] >= 0), ("text/markdown", True))
        self.assertEqual(mcp.frontmatter(read["contents"][0]["text"]), entry["frontmatter"])
        for uri in ("skill://nope/SKILL.md", "skill://start/references/envelope.md", "skill://start", None):
            self.assertEqual(self.error("skills/get", {"uri": uri})["code"], -32602)
        missing = self.error("resources/read", {"uri": "skill://start/none.md"})
        self.assertEqual((missing["code"], missing["data"]), (-32602, {"uri": "skill://start/none.md"}))
        children = self.result("resources/directory/read", {"uri": "skill://start"})["resources"]
        self.assertIn({"uri": "skill://start/references", "name": "references", "mimeType": "inode/directory"}, children)
        self.assertEqual(self.error("resources/directory/read", {"uri": entry["uri"]})["code"], -32602)
        resources = self.result("resources/list")["resources"]
        first = next(item for item in resources if item["uri"] == entry["uri"])
        self.assertEqual((first["name"], first["description"]), ("start", entry["frontmatter"]["description"]))
        self.assertEqual(self.result("resources/templates/list")["resourceTemplates"], [])

    def test_what_is_not_a_request_is_refused_or_ignored_and_one_failure_ends_nothing(self):
        self.assertEqual(self.error("prompts/list")["code"], -32601)
        self.assertEqual(self.server.handle([request("ping")])["error"]["code"], -32600)
        self.assertEqual(self.server.handle({"jsonrpc": "1.0", "id": 1, "method": "ping"})["error"]["code"], -32600)
        self.assertEqual(self.server.handle({"jsonrpc": "2.0", "id": None, "method": "ping"})["error"]["code"], -32600)
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "id": 4, "result": {}}))
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/unknown"}))
        self.assertEqual(self.error("tools/call", {"name": "none"})["code"], -32602)
        with contextlib.redirect_stderr(io.StringIO()) as said:  # the traceback is for the server's own log
            broken = self.error("tools/call", {"name": "boom"})
        self.assertIn("RuntimeError: broken tool", said.getvalue())
        self.assertEqual((broken["code"], broken["message"]), (-32603, "RuntimeError: broken tool"))
        self.assertEqual(self.result("ping")["resultType"], "complete")

    def test_a_call_tells_of_its_progress_until_it_is_cancelled_and_then_says_nothing_more(self):
        answers = []
        waiting = request("tools/call", {"name": "slow"}, {**META, "progressToken": "p1"}, ident="call-7")
        thread = threading.Thread(target=lambda: answers.append(self.server.handle(waiting)))
        thread.start()
        self.assertTrue(self.tools.began.wait(5))
        self.server.handle({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "call-7"}})
        thread.join(5)
        self.assertEqual(answers, [None])
        self.assertEqual(self.server._running, {})
        self.assertEqual(self.sent, [{"jsonrpc": "2.0", "method": "notifications/progress", "params": {
            "progressToken": "p1", "progress": 1, "message": "working"}}])  # and not the word after the cancellation
        self.tools.release.set()
        del self.sent[:]
        done = self.result("tools/call", {"name": "slow"}, {**META, "progressToken": 9})
        self.assertEqual((done["content"], done["isError"]), ([{"type": "text", "text": "done"}], False))
        self.assertEqual([note["params"]["message"] for note in self.sent], ["working", "nearly done"])

    def test_a_cancellation_that_overtakes_its_request_still_ends_it_and_an_odd_one_is_ignored(self):
        for odd in ([1], {"a": 1}, None, True, 1.5):
            self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                                  "params": {"requestId": odd}}))
        self.server.handle({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 31}})
        started = time.monotonic()
        self.assertIsNone(self.server.handle(request("tools/call", {"name": "slow"}, ident=31)))
        self.assertLess(time.monotonic() - started, 5)
        self.tools.release.set()  # the same id used again, later, is a request of its own
        self.assertIn("result", self.server.handle(request("tools/call", {"name": "slow"}, ident=31)))

    def test_no_line_a_client_sends_ends_the_server_or_puts_anything_but_json_on_its_output(self):
        self.tools.release.set()
        lines = [b'{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":[1]}}',
                 b"[" * 200_000,
                 b'{"jsonrpc":"2.0","id":1,"method":"resources/read","params":{"uri":NaN,"_meta":%s}}' % json.dumps(META).encode(),
                 json.dumps(request("tools/list", meta={**META, mcp.META_VERSION: "x"}, ident=2)).encode().replace(b'"x"', b"Infinity"),
                 json.dumps(request("resources/read", {"uri": "skill://x/\udc00"}, ident="\ud800")).encode(),
                 json.dumps(request("ping", ident="a\u2028b"), ensure_ascii=False).encode("utf-8"),
                 b'{"jsonrpc":"2.0","id":3,"method":"tools/call","params":"not an object"}',
                 b'{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":["x"],"arguments":7,"_meta":%s}}' % json.dumps(META).encode(),
                 json.dumps(request("ping", ident="last")).encode()]
        sink = io.BytesIO()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(mcp.Server(tools=self.tools).serve(io.BytesIO(b"\n".join(lines) + b"\n"), sink), 0)
        written = sink.getvalue()
        self.assertEqual(written, written.decode("ascii").encode("ascii"))  # no character that could break a line

        def strict(name):
            raise AssertionError(f"{name} on the output")

        answers = [json.loads(line, parse_constant=strict) for line in written.splitlines()]
        by_id = {answer.get("id"): answer for answer in answers}
        self.assertEqual(len(answers), 8)  # every line but the notification is answered, each with one line
        self.assertEqual(by_id["last"]["result"]["resultType"], "complete")
        self.assertEqual(by_id["\ud800"]["error"]["code"], -32602)
        self.assertEqual(by_id["a\u2028b"]["result"]["resultType"], "complete")
        self.assertEqual(sorted(answer["error"]["code"] for answer in answers if answer.get("id") is None),
                         [-32700, -32700, -32700])  # the nesting, NaN and Infinity: not JSON
        self.assertEqual((by_id[3]["error"]["code"], by_id[4]["error"]["code"]), (-32602, -32602))

    def test_a_call_still_waiting_when_the_input_ends_is_cancelled_and_the_server_goes(self):
        sink = io.BytesIO()
        started = time.monotonic()
        mcp.Server(tools=self.tools).serve(io.BytesIO(json.dumps(request("tools/call", {"name": "slow"})).encode() + b"\n"), sink)
        self.assertLess(time.monotonic() - started, mcp.DRAIN_S + 3)
        self.assertEqual(sink.getvalue(), b"")  # a cancelled call is not answered


class ToolsWithADaemon(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Daemon().start()
        cls.ran = []

        def runner(words, cwd, seconds, call):
            cls.ran.append((words, cwd, seconds))
            return 1, "a finding, as the verb printed it"

        cls.server = mcp.Server(tools=mcp.Tools(client=lambda: cls.d.client, runner=runner))
        cls.server.handle(request("initialize", {"protocolVersion": "2025-11-25"}, None))  # for the calls without _meta
        cls.saved = os.environ.get("WB_CALLER")
        os.environ["WB_CALLER"] = "mcp"

    @classmethod
    def tearDownClass(cls):
        if cls.saved is None:
            os.environ.pop("WB_CALLER", None)
        else:
            os.environ["WB_CALLER"] = cls.saved
        cls.d.stop()

    def call(self, name, arguments=None, meta=META, **more):
        response = self.server.handle(request("tools/call", {"name": name, "arguments": arguments or {}, **more}, meta))
        self.assertNotIn("error", response, response)
        return response["result"]

    def text(self, name, arguments=None, error=False, **more):
        result = self.call(name, arguments, **more)
        self.assertEqual(result["isError"], error, result)
        return result["content"][0]["text"]

    def test_commands_are_found_by_module_and_word_and_described_with_their_parameters(self):
        index = self.text("commands")
        self.assertIn("\nselftest: ", index)
        self.assertLess(len(index), 4000)  # names only: the summaries come by module or word
        found = self.text("commands", {"module": "selftest", "query": "zero-cost"})
        self.assertIn("selftest.spend $", found)
        self.assertNotIn("selftest.echo", found)
        self.assertIn("no command matches", self.text("commands", {"query": "nothing-has-this-word"}))
        described = self.text("describe", {"command": "selftest.echo"})
        self.assertIn('text: string = "ping"', described)
        self.assertIn("deterministic (cached)", described)
        self.assertIn("unknown command", self.text("describe", {"command": "no.such"}, error=True))

    def test_run_answers_with_the_envelopes_lines_and_the_job_can_be_read_again(self):
        ran = self.text("run", {"command": "selftest.echo", "params": {"text": "mcp", "times": 2}, "fresh": True})
        self.assertRegex(ran, r"^ok  selftest\.echo  j_\w+  ")
        job = re.search(r"j_\w+", ran).group()
        self.assertTrue(self.text("job").startswith(f"ok  selftest.echo  {job}"))  # this client's newest
        whole = json.loads(self.text("job", {"job": job, "detail": "full"}))
        self.assertEqual((whole["caller"], whole["data"]["text"]), ("mcp:test-client", "mcpmcp"))
        self.assertIn(job, self.text("jobs", {"mine": True, "limit": 5}))
        self.assertIn("settled", self.text("jobs", {"command": "selftest.echo", "state": "settled"}))
        self.assertIsInstance(self.text("logs", {"target": job, "limit": 5}), str)
        self.assertIn("up ", self.text("status"))

    def test_a_wrong_request_is_answered_in_words_that_put_it_right(self):
        self.assertIn("no argument named parms; it takes: command, params",
                      self.text("run", {"command": "selftest.echo", "parms": {}}, error=True))
        self.assertEqual(self.text("describe", {}, error=True), "describe: it needs command")
        self.assertIn("`wait` is a number", self.text("run", {"command": "selftest.echo", "wait": "long"}, error=True))
        self.assertIn("`detail` is one of: summary, full", self.text("job", {"detail": "all"}, error=True))
        self.assertTrue(self.text("run", {"command": "selftest.echo", "wait": 10**400}).startswith("ok  "))
        self.assertEqual([mcp._seconds(value, 7, 60) for value in (None, 10**400, float("nan"), float("inf"), -5, 1e308, 3)],
                         [7, 60, 0, 60, 0, 60, 3])
        usage = self.text("run", {"command": "selftest.echo", "params": {"nope": 1}})
        self.assertTrue(usage.startswith("USAGE  selftest.echo"))  # the daemon's own answer: a result, not a broken tool
        self.assertIn('logs(target="j_', usage)

    def test_a_relative_path_starts_from_cwd_or_is_refused(self):
        schema = {"properties": {"src": {"type": "string", "format": "path.in"}, "text": {"type": "string"},
                                 "many": {"type": "array", "items": {"type": "string", "format": "path.out"}}}}
        problem, _ = mcp._params({"src": "a/b.jar"}, schema, None)
        self.assertIn("`src` is a relative path", problem)
        base = str(Path(tempfile.gettempdir()))
        _, ready = mcp._params({"src": "a/b.jar", "text": "a/b", "many": "out"}, schema, base)
        self.assertEqual(ready, {"src": str(Path(base, "a/b.jar")), "text": "a/b", "many": [str(Path(base, "out"))]})
        absolute = str(Path(base, "x.jar"))
        self.assertEqual(mcp._params({"src": absolute}, schema, None), (None, {"src": absolute}))

    def test_a_wait_that_ends_leaves_the_job_running_and_says_how_to_follow_it(self):
        started = self.text("run", {"command": "selftest.sleep", "params": {"seconds": "30s"}, "wait": 0})
        job = re.search(r"j_\w+", started).group()
        self.assertIn(f'job(job="{job}", wait=...)', started)
        self.assertIn(f'job(job="{job}", wait=...)', self.text("job", {"job": job, "wait": 1}))
        ended = self.text("cancel", {"job": job})  # a running job ends once its process has stopped
        self.assertRegex(ended, rf"^(cancelled  selftest\.sleep  {job}|cancel requested; {job} ends once)")
        self.assertEqual(self.d.client.job(job).wait(timeout=30)["state"], "cancelled")
        self.assertTrue(self.text("cancel", {"job": job}).startswith("cancelled  selftest.sleep"))

    def test_a_quote_is_approved_only_by_the_persons_own_answer(self):
        waiting = self.text("run", {"command": "selftest.spend", "params": {"price": 0.0}})
        self.assertTrue(waiting.startswith("awaiting approval  selftest.spend"))
        job, quote = re.search(r"j_\w+", waiting).group(), re.search(r"quote: (\S+)", waiting).group(1)
        self.assertIn(f'approve(job="{job}", quote="{quote}")', waiting)
        named = {"job": job, "quote": quote}
        by_hand = self.text("approve", named, error=True)  # a client that cannot ask
        self.assertIn(f"wb approve {job} --quote {quote}", by_hand)
        self.assertIn("wb approve", self.text("approve", named, error=True, meta=None))
        self.assertIn("no longer the job's quote", self.text("approve", {"job": job, "quote": "q_other"}, error=True,
                                                             meta=ASKS))
        yes = {"approval": {"action": "accept", "content": {"approve": True}}}

        def asked(result):
            """The result is the question, put again, and nothing was approved."""
            self.assertEqual(result["resultType"], "input_required", result)
            ask = result["inputRequests"]["approval"]
            self.assertEqual((ask["method"], ask["params"]["mode"]), ("elicitation/create", "form"))
            self.assertEqual(ask["params"]["requestedSchema"]["properties"]["approve"]["type"], "boolean")
            self.assertNotIn("ttlMs", result)
            self.assertEqual(self.d.client.job(job).refresh()["state"], "awaiting_approval")
            return result["requestState"]

        state = asked(self.call("approve", named, meta=ASKS))
        # A yes that does not come back with this server's question about this job and quote is no answer to it.
        other = self.text("run", {"command": "selftest.spend", "params": {"price": 0.0}, "fresh": True})
        elsewhere = asked(self.call("approve", {"job": re.search(r"j_\w+", other).group(),
                                                "quote": re.search(r"quote: (\S+)", other).group(1)}, meta=ASKS))
        for forged in (None, "", "x.y", state[:-1] + ("0" if state[-1] != "0" else "1"), elsewhere, 7):
            more = {} if forged is None else {"requestState": forged}
            asked(self.call("approve", named, meta=ASKS, inputResponses=yes, **more))
        for answer in ({"action": "decline"}, {"action": "cancel"}, {"action": "accept", "content": {"approve": False}},
                       {"action": "accept", "content": {}}, {"action": "accept", "content": {"approve": "yes"}},
                       {"action": "accept", "content": {"approve": 1}}, {"action": "accept", "content": "approve"},
                       {"action": "accept", "content": [True]}, {"action": "accept"}):
            refused = self.text("approve", named, meta=ASKS, inputResponses={"approval": answer}, requestState=state)
            self.assertIn("Not approved", refused)
        self.assertEqual(self.d.client.job(job).refresh()["state"], "awaiting_approval")
        approved = self.text("approve", {**named, "wait": 10**400}, meta=ASKS, inputResponses=yes, requestState=state)
        self.assertTrue(approved.startswith("approved by the person.\nok  selftest.spend"), approved)
        self.assertIn("is not awaiting approval", self.text("approve", named, meta=ASKS, inputResponses=yes,
                                                            requestState=state))

    def test_cli_runs_a_verb_where_it_is_told_and_refuses_those_that_are_not_its_to_run(self):
        here = tempfile.gettempdir()
        ran = self.text("cli", {"args": ["check", "--json"], "cwd": here, "timeout": 30})
        self.assertEqual(ran, "exit 1  wb check --json\na finding, as the verb printed it")
        self.assertEqual(self.ran[-1], (["check", "--json"], here, 30.0))
        before = len(self.ran)
        for verb in ("approve", "deploy", "up", "down", "restart", "host", "mcp", "hook", "dev", "build"):
            self.assertIn(f"wb {verb} is not run from here", self.text("cli", {"args": [verb, "x"]}, error=True))
        for words in (["gpu", "run", "--", "wb", "approve", "j_1", "--quote", "q_1"], ["gpu", "--note", "n", "run", "x"]):
            self.assertIn("wb gpu run is not run from here", self.text("cli", {"args": words}, error=True))
        self.assertIn("begins with a verb", self.text("cli", {"args": ["--json", "check"]}, error=True))
        self.assertIn("begins with a verb", self.text("cli", {"args": []}, error=True))
        self.assertIn("NUL character", self.text("cli", {"args": ["work", "a\x00b"]}, error=True))
        self.assertIn("is not a folder", self.text("cli", {"args": ["work"], "cwd": str(Path(here, "no-such"))}, error=True))
        self.assertEqual(len(self.ran), before)
        self.text("cli", {"args": ["gpu", "stuck"]}, error=False)  # the lease's own verbs are run
        self.assertEqual(self.ran[-1][0], ["gpu", "stuck"])

    def test_a_daemon_that_does_not_answer_is_said_with_what_starts_it(self):
        from wb.client import Client
        lost = mcp.Server(tools=mcp.Tools(client=lambda: Client("http://127.0.0.1:9", "none", timeout=2)))
        result = lost.handle(request("tools/call", {"name": "status", "arguments": {}}))["result"]
        self.assertTrue(result["isError"])
        self.assertIn("not reachable", result["content"][0]["text"])
        self.assertIn("wb up", result["content"][0]["text"])


class OverPipes(unittest.TestCase):
    """The verb as a client starts it: one message per line each way, nothing else on stdout, gone when stdin closes."""

    def start(self, *argv, cwd=None, extra=None):
        paths = os.pathsep.join(str(REPO / part) for part in ("sdk/python", "core", "host"))
        env = {**os.environ, "PYTHONPATH": paths, "WB_URL": "http://127.0.0.1:9", "WB_TOKEN": "none", "WB_DEV": "1",
               **(extra or {})}
        env.pop("WB_CALLER", None)
        process = subprocess.Popen([sys.executable, "-m", "wb", *argv], cwd=cwd, env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: (process.kill(), process.communicate()))
        return process

    def test_messages_go_one_per_line_and_the_server_ends_with_its_input(self):
        process = self.start("mcp")
        lines = [request("server/discover", ident="a"), {"jsonrpc": "2.0", "method": "notifications/initialized"},
                 request("skills/list", ident="b"), request("tools/call", {"name": "status", "arguments": {}}, ident="c")]
        process.stdin.write(b"".join(json.dumps(line).encode() + b"\n" for line in lines) + b"\nnot json\n")
        process.stdin.flush()
        answers = {}
        while len(answers) < 4:  # each answer is one line, in whatever order the requests were done
            line = process.stdout.readline()
            self.assertTrue(line.endswith(b"\n") and b"\r" not in line, line)
            message = json.loads(line)
            answers[message.get("id")] = message
        started = time.monotonic()
        process.stdin.close()
        self.assertEqual(process.wait(timeout=30), 0)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(process.stdout.read(), b"")
        self.assertEqual(sorted(answers, key=str), [None, "a", "b", "c"])
        self.assertEqual(answers[None]["error"]["code"], -32700)
        self.assertEqual(answers["a"]["result"]["supportedVersions"], ["2026-07-28"])
        self.assertTrue(answers["b"]["result"]["skills"])
        self.assertIn("not reachable", answers["c"]["result"]["content"][0]["text"])

    def test_cli_runs_the_same_code_in_the_folder_it_is_given_without_a_terminal(self):
        folder = tempfile.mkdtemp(prefix="wb-mcp-cwd-")
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, ignore_errors=True))
        process = self.start("mcp")
        ask = request("tools/call", {"name": "cli", "arguments": {"args": ["mcp", "--list"], "cwd": folder}})
        out, _ = process.communicate(json.dumps(ask).encode() + b"\n", timeout=60)
        refused = json.loads(out)["result"]
        self.assertIn("wb mcp is not run from here", refused["content"][0]["text"])
        from unittest import mock
        with mock.patch.dict(os.environ, {"WB_URL": "http://127.0.0.1:9", "WB_TOKEN": "none"}):
            code, printed = mcp.run_cli(["status"], folder, 60, mcp.Call())  # no daemon there: exit 3, in words
            self.assertEqual(code, 3)
            self.assertIn("not reachable", printed)
            code, printed = mcp.run_cli(["mcp", "--list", "--json"], folder, 60, mcp.Call())
            self.assertEqual((code, [tool["name"] for tool in json.loads(printed)["tools"]][:2]),
                             (0, ["commands", "describe"]))
        # A wb module in the folder the verb stands in is not the one that runs.
        Path(folder, "wb.py").write_text("print('the folder ran')\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"WB_URL": "http://127.0.0.1:9", "WB_TOKEN": "none"}):
            code, printed = mcp.run_cli(["mcp", "--list"], folder, 60, mcp.Call())
        self.assertEqual((code, printed.splitlines()[0]), (0, "tools:"))
        # A process the verb leaves behind, holding its output open, does not hold the answer up.
        leaves = [sys.executable, "-c", "import subprocess, sys; print('left one', flush=True); "
                  "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])"]
        started = time.monotonic()
        self.assertEqual(mcp.run_cli([], folder, 60, mcp.Call(), program=leaves), (0, "left one"))
        self.assertLess(time.monotonic() - started, 15)
        sleeper = [sys.executable, "-c", "import time; print('began', flush=True); time.sleep(120)"]
        code, printed = mcp.run_cli([], folder, 0.5, mcp.Call(), program=sleeper)  # a verb past its time is stopped
        self.assertEqual((code, printed), (None, "began"))
        gone = mcp.Call()
        gone.cancelled.set()
        self.assertIsNone(mcp.run_cli([], folder, 600, gone, program=sleeper)[0])  # and so is one of a cancelled call

    def test_the_list_and_the_config_entry_are_printed_for_a_person(self):
        listed, _ = self.start("mcp", "--list").communicate(timeout=60)
        self.assertIn(b"tools:", listed)
        self.assertIn(b"\n  start ", listed)
        entry, _ = self.start("mcp", "--config", extra={"WB_RELEASES": str(Path(tempfile.gettempdir()) / "rel")}).communicate(timeout=60)
        config = json.loads(entry)["mcpServers"]["wb"]
        joined = " ".join([config["command"], *config["args"]])
        self.assertIn(str(Path(tempfile.gettempdir()) / "rel" / "current" / "bin"), joined)
        self.assertTrue(joined.endswith(" mcp"))


if __name__ == "__main__":
    unittest.main()
