package cc.orthrus.wb;

import java.util.Map;

/** A command that spends money waits for an explicit yes on one quote; show {@link #prompt()}, then approve. */
public final class ApprovalRequired extends RuntimeException {
    private static final long serialVersionUID = 1L;
    private final transient Job job;
    private final transient Envelope envelope;

    ApprovalRequired(Job job, Envelope envelope) {
        super(String.valueOf(Json.asMap(Json.asMap(envelope.approval()).get("prompt")).get("question")));
        this.job = job;
        this.envelope = envelope;
    }

    public Job job() {
        return job;
    }

    public Map<String, Object> quote() {
        return Json.asMap(envelope.approval().get("quote"));
    }

    public Map<String, Object> prompt() {
        return Json.asMap(envelope.approval().get("prompt"));
    }
}
