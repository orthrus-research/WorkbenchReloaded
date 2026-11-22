package cc.orthrus.wb.gradle;

import org.gradle.api.Plugin;
import org.gradle.api.Project;

/** {@code plugins { id 'cc.orthrus.wb' }}: the {@code wb} extension, and every WbTask reads it. */
public final class WbPlugin implements Plugin<Project> {
    @Override
    public void apply(Project project) {
        WbExtension extension = project.getExtensions().create("wb", WbExtension.class);
        project.getTasks().withType(WbTask.class).configureEach(task -> {
            task.getUrl().convention(extension.getUrl());
            task.getToken().convention(extension.getToken());
        });
    }
}
