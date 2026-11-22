package cc.orthrus.wb;

import java.time.Duration;
import java.util.Map;
import java.util.Set;

/** A promise of one result: {@link #result()} waits for it and returns the settled envelope. */
public final class Job {
    private static final Set<String> TERMINAL = Set.of("settled", "cancelled", "interrupted");
    private final Client client;
    private final String id;
    private Envelope envelope;

    Job(Client client, String id, Envelope envelope) {
        this.client = client;
        this.id = id;
        this.envelope = envelope;
    }

    public String id() {
        return id;
    }

    public Envelope envelope() {
        return envelope;
    }

    public Envelope refresh(int waitSeconds) {
        String query = waitSeconds > 0 ? "?wait=" + waitSeconds : "";
        envelope = new Envelope(client.request("GET", "/v1/jobs/" + id + query, null, Map.of(),
                Duration.ofSeconds(waitSeconds + 60L)));
        return envelope;
    }

    /** Waits until the job reaches a terminal state or awaits approval, or until the timeout passes. */
    public Envelope await(Duration timeout) {
        long deadline = timeout == null ? Long.MAX_VALUE : System.nanoTime() + timeout.toNanos();
        Envelope current = envelope != null ? envelope : refresh(0);
        while (!TERMINAL.contains(current.state()) && !"awaiting_approval".equals(current.state())) {
            long remaining = (deadline - System.nanoTime()) / 1_000_000_000L;
            if (remaining <= 0) return current;
            current = refresh((int) Math.max(1, Math.min(60, remaining)));
        }
        return current;
    }

    /** The settled envelope; throws {@link ApprovalRequired} for a command that spends money. */
    public Envelope result() {
        return result(null);
    }

    public Envelope result(Duration timeout) {
        Envelope current = await(timeout);
        if ("awaiting_approval".equals(current.state())) throw new ApprovalRequired(this, current);
        return current;
    }

    public Envelope approve(String quote, String via) {
        envelope = new Envelope(client.request("POST", "/v1/jobs/" + id + "/approve", Map.of("quote", quote, "via", via)));
        return envelope;
    }

    public Envelope cancel() {
        envelope = new Envelope(client.request("POST", "/v1/jobs/" + id + "/cancel", Map.of()));
        return envelope;
    }
}
