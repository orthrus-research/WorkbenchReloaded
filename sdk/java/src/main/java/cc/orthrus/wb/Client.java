package cc.orthrus.wb;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;

/**
 * Workbench Reloaded client for Java (D24): token, submit, long-poll, envelope; the JDK only.
 *
 * <pre>
 * Client wb = Client.discover();
 * Envelope result = wb.submit("frames.compare", Map.of("reference", ref, "candidate", cand)).result();
 * </pre>
 *
 * The endpoint and token come from WB_URL and WB_TOKEN (or WB_TOKEN_FILE), else from the run folder {@code wb up}
 * writes: ~/.config/wb/run/endpoint.json and ~/.config/wb/run/token.
 */
public final class Client {
    private final String url;
    private final String token;
    private final HttpClient http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(10)).build();
    private final String pathStyle = System.getProperty("os.name", "").toLowerCase().contains("win") ? "windows" : "posix";

    public Client(String url, String token) {
        this.url = url.replaceAll("/$", "");
        this.token = token;
    }

    public static Client discover() {
        String url = System.getenv("WB_URL");
        String token = System.getenv("WB_TOKEN");
        String config = System.getenv("WB_CONFIG");
        Path run = (config != null ? Path.of(config) : Path.of(System.getProperty("user.home"), ".config", "wb")).resolve("run");
        try {
            if (url == null && Files.isRegularFile(run.resolve("endpoint.json"))) {
                url = Json.asString(Json.parseObject(Files.readString(run.resolve("endpoint.json"))).get("url"));
            }
            if (token == null) {
                String file = System.getenv("WB_TOKEN_FILE");
                Path path = file != null ? Path.of(file) : run.resolve("token");
                if (Files.isRegularFile(path)) token = Files.readString(path, StandardCharsets.US_ASCII).trim();
            }
        } catch (IOException e) {
            throw new WbException("cannot read the Workbench run folder: " + e.getMessage(), -1, Map.of());
        }
        return new Client(url != null ? url : "http://127.0.0.1:7377", token);
    }

    Map<String, Object> request(String method, String path, Object body, Map<String, String> headers, Duration timeout) {
        HttpRequest.Builder builder = HttpRequest.newBuilder(URI.create(url + path)).timeout(timeout)
                .header("Accept", "application/json");
        if (token != null) builder.header("Authorization", "Bearer " + token);
        headers.forEach(builder::header);
        if (body == null) builder.method(method, HttpRequest.BodyPublishers.noBody());
        else builder.header("Content-Type", "application/json")
                .method(method, HttpRequest.BodyPublishers.ofString(Json.write(body)));
        HttpResponse<String> response;
        try {
            response = http.send(builder.build(), HttpResponse.BodyHandlers.ofString());
        } catch (IOException e) {
            throw new WbException("Workbench is not reachable at " + url + ": " + e.getMessage(), -1, Map.of());
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new WbException("interrupted", -1, Map.of());
        }
        Map<String, Object> payload = response.body().isEmpty() ? Map.of() : Json.parseObject(response.body());
        if (response.statusCode() >= 400) {
            Object error = payload.get("error");
            throw new WbException(error != null ? error.toString() : "HTTP " + response.statusCode(),
                    response.statusCode(), payload);
        }
        return payload;
    }

    Map<String, Object> request(String method, String path, Object body) {
        return request(method, path, body, Map.of(), Duration.ofSeconds(90));
    }

    public Map<String, Object> health() {
        return request("GET", "/v1/health", null);
    }

    public List<Object> commands() {
        return Json.asList(request("GET", "/v1/commands", null).get("commands"));
    }

    public Job submit(String command, Map<String, ?> params) {
        return submit(command, params, null, false);
    }

    public Job submit(String command, Map<String, ?> params, String out, boolean fresh) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("command", command);
        body.put("params", params == null ? Map.of() : params);
        body.put("fresh", fresh);
        body.put("path_style", pathStyle);
        if (out != null) body.put("out", out);
        Map<String, Object> envelope = request("POST", "/v1/jobs", body,
                Map.of("Idempotency-Key", UUID.randomUUID().toString()), Duration.ofSeconds(90));
        return new Job(this, Json.asString(envelope.get("job")), new Envelope(envelope));
    }

    public Job job(String id) {
        return new Job(this, id, null);
    }
}
