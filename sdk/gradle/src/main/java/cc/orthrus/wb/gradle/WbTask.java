package cc.orthrus.wb.gradle;

import cc.orthrus.wb.ApprovalRequired;
import cc.orthrus.wb.Client;
import cc.orthrus.wb.Envelope;
import cc.orthrus.wb.Json;
import java.io.IOException;
import java.nio.file.Files;
import java.time.Duration;
import java.util.List;
import org.gradle.api.DefaultTask;
import org.gradle.api.GradleException;
import org.gradle.api.file.RegularFileProperty;
import org.gradle.api.provider.ListProperty;
import org.gradle.api.provider.MapProperty;
import org.gradle.api.provider.Property;
import org.gradle.api.tasks.Input;
import org.gradle.api.tasks.Internal;
import org.gradle.api.tasks.Optional;
import org.gradle.api.tasks.OutputFile;
import org.gradle.api.tasks.TaskAction;
import org.gradle.work.DisableCachingByDefault;

/**
 * Runs one Workbench command and fails the build on the outcomes in {@code failOn} (default: finding, usage,
 * environment). The settled envelope is written to {@code envelope} for later tasks to read.
 *
 * <pre>
 * tasks.register('framesGate', cc.orthrus.wb.gradle.WbTask) {
 *     command = 'frames.compare'
 *     params = [reference: 'D:/work/ref', candidate: 'D:/work/cand', profile: 'near']  // scrub: allow
 * }
 * </pre>
 */
@DisableCachingByDefault(because = "Workbench caches deterministic commands itself")
public abstract class WbTask extends DefaultTask {
    @Input
    public abstract Property<String> getCommand();

    @Input
    public abstract MapProperty<String, Object> getParams();

    @Input
    @Optional
    public abstract Property<String> getOut();

    @Input
    public abstract ListProperty<String> getFailOn();

    @Input
    public abstract Property<Integer> getTimeoutSeconds();

    @Internal
    public abstract Property<String> getUrl();

    @Internal
    public abstract Property<String> getToken();

    @OutputFile
    public abstract RegularFileProperty getEnvelope();

    public WbTask() {
        getFailOn().convention(List.of("finding", "usage", "environment"));
        getTimeoutSeconds().convention(3600);
        getEnvelope().convention(getProject().getLayout().getBuildDirectory().file("wb/" + getName() + ".json"));
        setGroup("workbench");
    }

    /** {@code failOn('usage', 'environment')} to let findings pass. */
    public void failOn(String... outcomes) {
        getFailOn().set(List.of(outcomes));
    }

    @TaskAction
    public void run() throws IOException {
        Client client = getUrl().isPresent() ? new Client(getUrl().get(), getToken().getOrNull()) : Client.discover();
        Envelope result;
        try {
            result = client.submit(getCommand().get(), getParams().get(), getOut().getOrNull(), false)
                    .result(Duration.ofSeconds(getTimeoutSeconds().get()));
        } catch (ApprovalRequired pending) {
            throw new GradleException(getCommand().get() + " spends money and waits for approval: "
                    + pending.prompt().get("question") + " (approve with: wb approve " + pending.job().id()
                    + " --quote " + pending.quote().get("id") + ")");
        }
        Files.createDirectories(getEnvelope().get().getAsFile().toPath().getParent());
        Files.writeString(getEnvelope().get().getAsFile().toPath(), Json.write(result.raw()));
        String outcome = result.outcome() == null ? result.state() : result.outcome();
        getLogger().lifecycle("{} {} {}: {}", outcome, getCommand().get(), result.job(), result.summary());
        if (getFailOn().get().contains(outcome) || !"settled".equals(result.state())) {
            throw new GradleException(getCommand().get() + " ended " + outcome + ": " + result.summary());
        }
    }
}
