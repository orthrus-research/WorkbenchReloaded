package cc.orthrus.wb.gradle;

import cc.orthrus.wb.Json;
import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.io.UncheckedIOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.Instant;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.TreeMap;
import java.util.stream.Stream;
import java.util.zip.ZipFile;
import org.gradle.api.GradleException;
import org.gradle.api.file.DirectoryProperty;
import org.gradle.api.file.RegularFileProperty;
import org.gradle.api.provider.ListProperty;
import org.gradle.api.provider.MapProperty;
import org.gradle.api.tasks.Input;
import org.gradle.api.tasks.Internal;
import org.gradle.api.tasks.JavaExec;
import org.gradle.api.tasks.OutputFile;
import org.gradle.api.tasks.TaskAction;
import org.gradle.jvm.toolchain.JavaLauncher;
import org.gradle.work.DisableCachingByDefault;

/**
 * Runs an entry point on a class path the build prepares, a mod build's remapped configurations included, with no
 * game client or server, and keeps a receipt of what ran. Remapping stays with the build's own plugin: this task takes
 * the class path it makes. Before the run starts, every pinned file must still have its SHA-256, and every class named
 * in {@code singleProvider} must come from exactly one class path entry, so an unmapped copy left beside the mapped one
 * is caught. The receipt ({@code wb.gradle-run.v1}, in {@code build/wb/<task>.run.json}) records the main class, the
 * arguments, the JVM, every class path entry with its SHA-256, the pins, the providers found and the exit code; a run
 * that exits non-zero fails the task once its receipt is written.
 *
 * <pre>
 * tasks.register('checkGenerator', cc.orthrus.wb.gradle.WbRun) {
 *     classpath = sourceSets.checks.runtimeClasspath + configurations.remappedMods
 *     mainClass = 'com.example.checks.GeneratorCheck'
 *     pin('libs/engine.jar', '9f86d081884c7d65...')
 *     singleProvider = ['com.example.api.Generator']
 * }
 * </pre>
 */
@DisableCachingByDefault(because = "a run is evidence: its receipt says what ran, each time it runs")
public abstract class WbRun extends JavaExec {
    public static final String SCHEMA = "wb.gradle-run.v1";

    /** Files that must still have these SHA-256 digests when the run starts, by absolute path ({@link #pin}). */
    @Input
    public abstract MapProperty<String, String> getPins();

    /** Classes ({@code com.example.Api}) that must each be found in exactly one class path entry. */
    @Input
    public abstract ListProperty<String> getSingleProvider();

    @OutputFile
    public abstract RegularFileProperty getReceipt();

    /** Where relative pins resolve; set from the project, so the run itself never reads the project. */
    @Internal
    public abstract DirectoryProperty getProjectDirectory();

    public WbRun() {
        getReceipt().convention(getProject().getLayout().getBuildDirectory().file("wb/" + getName() + ".run.json"));
        getProjectDirectory().convention(getProject().getLayout().getProjectDirectory());
        setGroup("workbench");
        setIgnoreExitValue(true); // the exit code goes into the receipt first; the task fails after
    }

    /** {@code pin('libs/engine.jar', '<sha256>')}: the run refuses to start when the file has other bytes. */
    public void pin(Object path, String sha256) {
        getPins().put(getProject().file(path).getAbsolutePath(), sha256.trim().toLowerCase(Locale.ROOT));
    }

    @TaskAction
    @Override
    public void exec() {
        Path base = getProjectDirectory().get().getAsFile().toPath();
        Map<String, Object> pins = new LinkedHashMap<>();
        List<String> refusals = new ArrayList<>();
        for (Map.Entry<String, String> pin : new TreeMap<>(getPins().get()).entrySet()) {
            Path file = base.resolve(pin.getKey());
            String expected = pin.getValue().trim().toLowerCase(Locale.ROOT);
            String actual = Files.isRegularFile(file) ? sha256(file) : null;
            Map<String, Object> record = new LinkedHashMap<>();
            record.put("expected", expected);
            record.put("actual", actual);
            pins.put(file.toString(), record);
            if (actual == null) {
                refusals.add(file + " is missing");
            } else if (!actual.equals(expected)) {
                refusals.add(file + " is " + actual.substring(0, 12) + ", not its pin " + abbreviated(expected));
            }
        }
        List<File> entries = new ArrayList<>(getClasspath().getFiles());
        Map<String, Object> providers = new LinkedHashMap<>();
        for (String name : getSingleProvider().get()) {
            String resource = name.replace('.', '/') + ".class";
            List<String> found = new ArrayList<>();
            for (File entry : entries) {
                if (provides(entry, resource)) {
                    found.add(entry.getAbsolutePath());
                }
            }
            providers.put(name, found);
            if (found.size() != 1) {
                refusals.add(name + (found.isEmpty() ? " is in no class path entry"
                        : " is in " + found.size() + " class path entries: " + String.join(", ", found)));
            }
        }
        if (!refusals.isEmpty()) {
            throw new GradleException(getName() + " does not start: " + String.join("; ", refusals));
        }
        List<Object> classpath = new ArrayList<>();
        for (File entry : entries) {
            classpath.add(describe(entry));
        }
        Instant started = Instant.now();
        long began = System.nanoTime();
        super.exec();
        double seconds = (System.nanoTime() - began) / 1e9;
        int exit = getExecutionResult().get().getExitValue();
        Map<String, Object> receipt = new LinkedHashMap<>();
        receipt.put("schema", SCHEMA);
        receipt.put("task", getPath());
        receipt.put("main_class", getMainClass().getOrNull());
        receipt.put("args", new ArrayList<>(getArgs()));
        receipt.put("jvm_args", new ArrayList<>(getJvmArgs()));
        receipt.put("java", java());
        receipt.put("classpath", classpath);
        receipt.put("pins", pins);
        receipt.put("single_provider", providers);
        receipt.put("started", started.toString());
        receipt.put("seconds", Math.round(seconds * 1000) / 1000.0);
        receipt.put("exit", exit);
        Path written = getReceipt().get().getAsFile().toPath();
        try {
            Files.createDirectories(written.getParent());
            Files.writeString(written, Json.write(receipt) + "\n", StandardCharsets.UTF_8);
        } catch (IOException error) {
            throw new UncheckedIOException(error);
        }
        getLogger().lifecycle("{} exited {} in {} s; receipt {}", getMainClass().getOrNull(), exit,
                Math.round(seconds * 10) / 10.0, written);
        if (exit != 0) {
            throw new GradleException(getMainClass().getOrNull() + " exited " + exit + " (receipt " + written + ")");
        }
    }

    private Map<String, Object> java() {
        Map<String, Object> out = new LinkedHashMap<>();
        if (getJavaLauncher().isPresent()) {
            JavaLauncher launcher = getJavaLauncher().get();
            out.put("version", launcher.getMetadata().getJavaRuntimeVersion());
            out.put("vendor", launcher.getMetadata().getVendor());
            out.put("home", launcher.getMetadata().getInstallationPath().getAsFile().getAbsolutePath());
        } else {
            out.put("executable", getExecutable());
        }
        return out;
    }

    private static String abbreviated(String digest) {
        return digest.length() > 12 ? digest.substring(0, 12) : digest;
    }

    /** One class path entry: a file by its SHA-256, a folder by the SHA-256 of its files' paths and digests. */
    private static Map<String, Object> describe(File entry) {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("path", entry.getAbsolutePath());
        if (entry.isFile()) {
            out.put("kind", "file");
            out.put("bytes", entry.length());
            out.put("sha256", sha256(entry.toPath()));
        } else if (entry.isDirectory()) {
            Path root = entry.toPath();
            StringBuilder listing = new StringBuilder();
            int[] count = {0};
            try (Stream<Path> walk = Files.walk(root)) {
                walk.filter(Files::isRegularFile)
                        .map(p -> root.relativize(p).toString().replace(File.separatorChar, '/'))
                        .sorted()
                        .forEach(relative -> {
                            listing.append(relative).append('\0').append(sha256(root.resolve(relative))).append('\n');
                            count[0]++;
                        });
            } catch (IOException error) {
                throw new UncheckedIOException(error);
            }
            out.put("kind", "folder");
            out.put("files", count[0]);
            out.put("sha256", sha256(listing.toString().getBytes(StandardCharsets.UTF_8)));
        } else {
            out.put("kind", "missing");
        }
        return out;
    }

    private static boolean provides(File entry, String resource) {
        if (entry.isDirectory()) {
            return new File(entry, resource).isFile();
        }
        if (!entry.isFile()) {
            return false;
        }
        try (ZipFile archive = new ZipFile(entry)) {
            return archive.getEntry(resource) != null;
        } catch (IOException notAnArchive) {
            return false;
        }
    }

    private static String sha256(Path file) {
        try (InputStream in = Files.newInputStream(file)) {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            byte[] buffer = new byte[1 << 16];
            for (int read; (read = in.read(buffer)) > 0; ) {
                digest.update(buffer, 0, read);
            }
            return HexFormat.of().formatHex(digest.digest());
        } catch (IOException error) {
            throw new UncheckedIOException(error);
        } catch (NoSuchAlgorithmException impossible) {
            throw new IllegalStateException(impossible);
        }
    }

    private static String sha256(byte[] data) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(data));
        } catch (NoSuchAlgorithmException impossible) {
            throw new IllegalStateException(impossible);
        }
    }
}
