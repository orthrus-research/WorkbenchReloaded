package cc.orthrus.wb;

import java.util.Map;

/** The API refused a request or could not be reached. */
public class WbException extends RuntimeException {
    private final int status;
    private static final long serialVersionUID = 1L;
    private final transient Map<String, Object> body;

    public WbException(String message, int status, Map<String, Object> body) {
        super(message);
        this.status = status;
        this.body = body;
    }

    public int status() {
        return status;
    }

    public Map<String, Object> body() {
        return body;
    }
}
