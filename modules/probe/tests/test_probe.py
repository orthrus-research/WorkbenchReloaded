"""The probe module: probes compiled against JARs built in the test and run in throwaway JVMs on every JDK
found here, and an API JAR's consumer run phase by phase in fresh JVMs."""

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
for extra in (REPO / "tests", REPO / "modules" / "jar" / "tests"):
    sys.path.insert(0, str(extra))

import jvmkit  # noqa: E402
from harness import Daemon  # noqa: E402

JDKS = jvmkit.find_jdks()
JAVAC = jvmkit.newest(JDKS)
jvmkit.export(JDKS)

LIBRARY = {"lib/Counter.java": "package lib; public class Counter { public static int twice(int x) { return 2 * x; } }"}
PROBES = {
    "Twice.java": """
        public class Twice {
            public static void main(String[] args) throws Exception {
                if (lib.Counter.twice(21) != 42) System.exit(1);
                java.nio.file.Files.write(java.nio.file.Paths.get(args[0], "seen"), new byte[0]);
                System.out.println("PROBE_OK twice " + System.getProperty("java.specification.version"));
            }
        }
    """,
    "Quiet.java": "public class Quiet { public static void main(String[] args) { } }",
    "Helper.java": "public class Helper { static int one() { return 1; } }",
}
STORE = {
    "api/Store.java": """
        package api;
        import java.nio.file.*;
        public final class Store {
            public static void save(Path folder, String value) throws Exception {
                Files.write(folder.resolve("state.txt"), value.getBytes("UTF-8"));
            }
            public static String load(Path folder) throws Exception {
                return new String(Files.readAllBytes(folder.resolve("state.txt")), "UTF-8");
            }
        }
    """,
    "api/Leaky.java": "package api; public final class Leaky { public static int hidden() { return dep.Hidden.value(); } }",
    "dep/Hidden.java": "package dep; public final class Hidden { public static int value() { return 7; } }",
}
CONSUMER = {
    "probe/Consumer.java": """
        package probe;
        import java.nio.file.*;
        public class Consumer {
            public static void main(String[] args) throws Exception {
                Path state = Paths.get(args[1]);
                String me = Long.toString(ProcessHandle.current().pid());
                if (args[0].equals("prepare")) {
                    api.Store.save(state, me);
                } else if (api.Store.load(state).equals(me)) {
                    System.exit(3);  // the same process: no handoff was proved
                }
                System.out.println("PROBE_OK " + args[0]);
            }
        }
    """,
}


@unittest.skipUnless(JAVAC, "no JDK with javac on this machine")
class Probes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Daemon().start()
        cls.c = cls.d.client
        data = cls.d.data
        classes = jvmkit.compile_java(JAVAC, LIBRARY, cls.d.root / "lib", release=8)
        cls.library = jvmkit.make_jar(data / "lib.jar", folders=[classes])
        cls.probes = data / "probes"
        jvmkit.write_sources(cls.probes, PROBES)
        stored = jvmkit.compile_java(JAVAC, STORE, cls.d.root / "store", release=11)
        cls.api = jvmkit.make_jar(data / "api.jar", folders=[stored], skip=("dep/", "api/Leaky"))
        cls.leaky = jvmkit.make_jar(data / "leaky.jar", folders=[stored], skip=("dep/",))
        cls.dep = jvmkit.make_jar(data / "dep.jar", folders=[stored], skip=("api/",))
        cls.consumer = data / "consumer"
        jvmkit.write_sources(cls.consumer, CONSUMER)
        cls.newest = max(JDKS, key=int)

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    def submit(self, command, params):
        return self.c.submit(command, params).result(timeout=300)

    def test_matrix_passes_on_every_jdk(self):
        result = self.submit("probe.jdk-matrix", {"sources": [str(self.probes / "Twice.java"), str(self.probes / "Helper.java")],
                                                  "classpath": [str(self.library)], "args": ["{work}"]})
        self.assertEqual(result.outcome, "ok", result.summary)
        self.assertEqual(result.data.jdks, {key: True for key in JDKS})
        self.assertEqual({r["probe"] for r in result.data.runs}, {"Twice"})  # Helper declares no main
        report = Path(result.data.report).parent
        self.assertTrue((report / "logs" / "compile.log").is_file())
        self.assertIn("PROBE_OK twice", (report / "logs" / f"{self.newest}-Twice.log").read_text(encoding="utf-8"))

    def test_matrix_findings(self):
        quiet = self.submit("probe.jdk-matrix", {"sources": [str(self.probes)], "classpath": [str(self.library)],
                                                 "jdks": [self.newest], "args": ["{work}"]})
        self.assertEqual((quiet.outcome, quiet.code), ("finding", 1))
        self.assertIn(f"JDK {self.newest}: Quiet exited 0 without a line starting PROBE_OK", quiet["findings"])
        self.assertEqual(quiet.data.jdks, {self.newest: False})
        (self.d.data / "slow").mkdir(exist_ok=True)
        (self.d.data / "slow" / "Slow.java").write_text(
            "public class Slow { public static void main(String[] a) throws Exception { Thread.sleep(60000); } }",
            encoding="utf-8")
        slow = self.submit("probe.jdk-matrix", {"sources": [str(self.d.data / "slow")], "jdks": [self.newest],
                                                "timeout": "3s"})
        self.assertEqual(slow.outcome, "finding")
        self.assertIn("Slow timed out", slow.summary)
        (self.d.data / "broken").mkdir(exist_ok=True)
        (self.d.data / "broken" / "Broken.java").write_text(
            "public class Broken { public static void main(String[] a) { lib.Counter.thrice(1); } }", encoding="utf-8")
        broken = self.submit("probe.jdk-matrix", {"sources": [str(self.d.data / "broken")],
                                                  "classpath": [str(self.library)], "jdks": [self.newest]})
        self.assertEqual(broken.outcome, "finding")
        self.assertFalse(broken.data.compiled)
        self.assertIn("do not compile against the class path", broken.summary)
        absent = self.submit("probe.jdk-matrix", {"sources": [str(self.probes / "Twice.java")], "jdks": ["5"]})
        self.assertEqual(absent.outcome, "environment")

    def test_api_stands_alone(self):
        result = self.submit("probe.api-consumer", {"api": str(self.api), "consumer": [str(self.consumer)],
                                                    "release": 11, "jdk": self.newest, "forbid": ["dep"]})
        self.assertEqual(result.outcome, "ok", result.summary)
        self.assertEqual([(p["phase"], p["passed"]) for p in result.data.phases], [("prepare", True), ("resume", True)])
        self.assertEqual((result.data.missing, result.data.forbidden), ([], []))

    def test_api_that_leans_on_more(self):
        leaky = self.submit("probe.api-consumer", {"api": str(self.leaky), "consumer": [str(self.consumer)],
                                                   "release": 11, "jdk": self.newest, "forbid": ["dep"]})
        self.assertEqual(leaky.outcome, "finding")
        self.assertEqual(leaky.data.missing, ["api.Leaky -> dep.Hidden"])
        self.assertEqual(leaky.data.forbidden, ["api.Leaky refers to dep.Hidden"])
        declared = self.submit("probe.api-consumer", {"api": str(self.leaky), "consumer": [str(self.consumer)],
                                                      "libraries": [str(self.dep)], "release": 11, "jdk": self.newest})
        self.assertEqual(declared.outcome, "ok", declared.summary)  # the dependency is declared: it stands
        one = self.submit("probe.api-consumer", {"api": str(self.api), "consumer": [str(self.consumer)], "release": 11,
                                                 "jdk": self.newest, "phases": ["resume"]})
        self.assertEqual(one.outcome, "finding")  # resume alone finds no state from an earlier process
        self.assertIn("phase resume: probe.Consumer exited 1", one.summary)
        (self.d.data / "needs").mkdir(exist_ok=True)
        (self.d.data / "needs" / "Needs.java").write_text(
            "public class Needs { public static void main(String[] a) { System.out.println(dep.Hidden.value()); } }",
            encoding="utf-8")
        outside = self.submit("probe.api-consumer", {"api": str(self.api), "consumer": [str(self.d.data / "needs")],
                                                     "release": 11, "jdk": self.newest})
        self.assertEqual(outside.outcome, "finding")
        self.assertFalse(outside.data.compiled)
        self.assertIn("does not compile against the API JAR alone", outside.summary)


if __name__ == "__main__":
    unittest.main()
