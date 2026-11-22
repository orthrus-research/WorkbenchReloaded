package cc.orthrus.wb.gradle;

import org.gradle.api.provider.Property;

/** {@code wb { url = '...'; token = '...' }}: both optional; the client otherwise discovers them as `wb up` wrote. */
public abstract class WbExtension {
    public abstract Property<String> getUrl();

    public abstract Property<String> getToken();
}
