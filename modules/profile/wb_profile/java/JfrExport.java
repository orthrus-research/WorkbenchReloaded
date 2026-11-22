import java.io.BufferedWriter;
import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.OutputStreamWriter;
import java.io.Writer;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;

import jdk.jfr.EventType;
import jdk.jfr.Timespan;
import jdk.jfr.Timestamp;
import jdk.jfr.ValueDescriptor;
import jdk.jfr.consumer.RecordedClass;
import jdk.jfr.consumer.RecordedEvent;
import jdk.jfr.consumer.RecordedFrame;
import jdk.jfr.consumer.RecordedMethod;
import jdk.jfr.consumer.RecordedStackTrace;
import jdk.jfr.consumer.RecordedThread;
import jdk.jfr.consumer.RecordingFile;

/**
 * Writes the events of a JFR recording that a profile command reads as JSON lines on standard output,
 * filtered before anything is expanded: only events of the named types, only inside a time range.
 *
 *   java JfrExport.java RECORDING FROM_NS TO_NS TYPES [ANYTIME_TYPES]
 *
 * TYPES (comma-separated event names) are kept when they start in, or overlap, [FROM_NS, TO_NS), in epoch
 * nanoseconds; ANYTIME_TYPES (the JVM's and the machine's description) are kept wherever they fall.
 * Methods, stacks and threads are written once, as table rows, before the first event that uses them:
 *
 *   {"m":ID,"n":"pkg.Class.method"}                                     a method
 *   {"s":ID,"f":[METHOD,...],"x":1}                                      a stack, leaf first; x: truncated
 *   {"h":ID,"n":"name","j":JAVA_ID,"o":OS_ID}                            a thread
 *   {"e":"jdk.Type","t":START_NS,"d":NS,"h":THREAD,"s":STACK,"v":{..}}   an event
 *   {"end":{"events":N,"exported":N,"first":NS,"last":NS,"types":{..}}}  the whole recording, last line
 *
 * An event's thread is its sampled thread when it has one, else its event thread. Field values are
 * numbers, strings and booleans as recorded; timespans and timestamps in nanoseconds; classes by name;
 * methods and threads by table ID. Fields of other structured types are left out.
 */
final class JfrExport {
    private static final int SKIP = 0, TIMESPAN = 1, TIMESTAMP = 2, BOOL = 3, INTEGER = 4, REAL = 5,
            TEXT = 6, CHAR = 7, THREAD = 8, CLASS = 9, METHOD = 10;
    private static final Set<String> HANDLED = Set.of("startTime", "duration", "eventThread", "sampledThread",
            "stackTrace");

    private final Writer out;
    private final Map<String, Integer> methodIds = new HashMap<>();
    private final Map<String, Integer> stackIds = new HashMap<>();
    private final Map<Long, Integer> threadIds = new HashMap<>();
    private final Map<RecordedMethod, Integer> methodCache = new IdentityHashMap<>();
    private final Map<RecordedStackTrace, Integer> stackCache = new IdentityHashMap<>();
    private final Map<EventType, List<Field>> fields = new IdentityHashMap<>();

    private record Field(String name, int kind) {}

    private JfrExport(Writer out) {
        this.out = out;
    }

    public static void main(String[] args) throws IOException {
        if (args.length < 4) {
            System.err.println("usage: JfrExport RECORDING FROM_NS TO_NS TYPES [ANYTIME_TYPES]");
            System.exit(2);
        }
        long from = Long.parseLong(args[1]), to = Long.parseLong(args[2]);
        Set<String> windowed = names(args[3]), anytime = names(args.length > 4 ? args[4] : "");
        Writer out = new BufferedWriter(new OutputStreamWriter(new FileOutputStream(FileDescriptor.out),
                StandardCharsets.UTF_8), 1 << 16);
        new JfrExport(out).export(Path.of(args[0]), from, to, windowed, anytime);
        out.flush();
    }

    private static Set<String> names(String list) {
        Set<String> out = new HashSet<>();
        for (String name : list.split(",")) {
            if (!name.isBlank()) out.add(name.trim());
        }
        return out;
    }

    private void export(Path recording, long from, long to, Set<String> windowed, Set<String> anytime)
            throws IOException {
        Map<String, Long> types = new TreeMap<>();
        long events = 0, exported = 0, first = Long.MAX_VALUE, last = Long.MIN_VALUE;
        try (RecordingFile input = new RecordingFile(recording)) {
            while (input.hasMoreEvents()) {
                RecordedEvent event = input.readEvent();
                String type = event.getEventType().getName();
                long start = nanos(event.getStartTime()), end = nanos(event.getEndTime());
                events++;
                types.merge(type, 1L, Long::sum);
                first = Math.min(first, start);
                last = Math.max(last, end);
                boolean inside = start < to && (start >= from || end > from);
                if (anytime.contains(type) || windowed.contains(type) && inside) {
                    write(event, type, start, end);
                    exported++;
                }
                if (methodCache.size() > 200_000) methodCache.clear();
                if (stackCache.size() > 200_000) stackCache.clear();
            }
        }
        StringBuilder line = new StringBuilder("{\"end\":{\"events\":").append(events)
                .append(",\"exported\":").append(exported)
                .append(",\"first\":").append(events == 0 ? "null" : Long.toString(first))
                .append(",\"last\":").append(events == 0 ? "null" : Long.toString(last))
                .append(",\"types\":{");
        boolean comma = false;
        for (Map.Entry<String, Long> entry : types.entrySet()) {
            if (comma) line.append(',');
            quote(line, entry.getKey()).append(':').append(entry.getValue());
            comma = true;
        }
        out.write(line.append("}}}\n").toString());
    }

    private void write(RecordedEvent event, String type, long start, long end) throws IOException {
        RecordedThread owner = event.hasField("sampledThread") ? event.getThread("sampledThread") : event.getThread();
        Integer thread = owner == null ? null : thread(owner);
        RecordedStackTrace trace = event.getStackTrace();
        Integer stack = trace == null ? null : stack(trace);
        StringBuilder values = new StringBuilder();
        for (Field field : fields.computeIfAbsent(event.getEventType(), JfrExport::describe)) {
            int mark = values.length();
            values.append(values.isEmpty() ? "" : ",");
            quote(values, field.name()).append(':');
            if (!value(values, event, field)) values.setLength(mark);
        }
        StringBuilder line = new StringBuilder("{\"e\":");
        quote(line, type).append(",\"t\":").append(start);
        if (end > start) line.append(",\"d\":").append(end - start);
        if (thread != null) line.append(",\"h\":").append(thread);
        if (stack != null) line.append(",\"s\":").append(stack);
        if (!values.isEmpty()) line.append(",\"v\":{").append(values).append('}');
        out.write(line.append("}\n").toString());
    }

    private static List<Field> describe(EventType type) {
        List<Field> out = new ArrayList<>();
        for (ValueDescriptor field : type.getFields()) {
            if (HANDLED.contains(field.getName())) continue;
            int kind = kind(field);
            if (kind != SKIP) out.add(new Field(field.getName(), kind));
        }
        return out;
    }

    private static int kind(ValueDescriptor field) {
        // A parsed recording's descriptors carry their annotations but no content type.
        if (field.getAnnotation(Timespan.class) != null) return TIMESPAN;
        if (field.getAnnotation(Timestamp.class) != null) return TIMESTAMP;
        return switch (field.getTypeName()) {
            case "boolean" -> BOOL;
            case "byte", "short", "int", "long" -> INTEGER;
            case "float", "double" -> REAL;
            case "char" -> CHAR;
            case "java.lang.String" -> TEXT;
            case "java.lang.Thread" -> THREAD;
            case "java.lang.Class" -> CLASS;
            case "jdk.types.Method" -> METHOD;
            default -> SKIP;
        };
    }

    /** Appends one field's value; false when it has none worth writing. */
    private boolean value(StringBuilder out, RecordedEvent event, Field field) throws IOException {
        String name = field.name();
        try {
            switch (field.kind()) {
                case TIMESPAN -> {
                    Duration span = event.getDuration(name);
                    if (span == null) return false;
                    out.append(span.toNanos());
                }
                case TIMESTAMP -> {
                    Instant instant = event.getInstant(name);
                    if (instant == null) return false;
                    out.append(nanos(instant));
                }
                case BOOL -> out.append(event.getBoolean(name));
                case INTEGER -> out.append(event.getLong(name));
                case REAL -> {
                    double number = event.getDouble(name);
                    if (Double.isNaN(number) || Double.isInfinite(number)) return false;
                    out.append(number);
                }
                case CHAR -> quote(out, String.valueOf(event.getChar(name)));
                case TEXT -> {
                    String text = event.getString(name);
                    if (text == null) return false;
                    quote(out, text);
                }
                case THREAD -> {
                    RecordedThread thread = event.getThread(name);
                    if (thread == null) return false;
                    out.append(thread(thread));
                }
                case CLASS -> {
                    RecordedClass type = event.getClass(name);
                    if (type == null) return false;
                    quote(out, type.getName());
                }
                case METHOD -> {
                    RecordedMethod method = event.getValue(name);
                    if (method == null) return false;
                    out.append(method(method));
                }
                default -> {
                    return false;
                }
            }
        } catch (ArithmeticException | IllegalArgumentException | ClassCastException ignored) {
            return false; // an unbounded timespan, or a field of another shape in this recording
        }
        return true;
    }

    private int thread(RecordedThread thread) throws IOException {
        Integer id = threadIds.get(thread.getId());
        if (id != null) return id;
        id = threadIds.size() + 1;
        threadIds.put(thread.getId(), id);
        String name = thread.getJavaName() != null ? thread.getJavaName() : thread.getOSName();
        StringBuilder line = new StringBuilder("{\"h\":").append(id).append(",\"n\":");
        quote(line, name == null ? "(unnamed)" : name).append(",\"j\":").append(thread.getJavaThreadId())
                .append(",\"o\":").append(thread.getOSThreadId());
        out.write(line.append("}\n").toString());
        return id;
    }

    private int method(RecordedMethod method) throws IOException {
        Integer cached = methodCache.get(method);
        if (cached != null) return cached;
        RecordedClass type = method.getType();
        String name = (type == null ? "(unknown)" : type.getName()) + "." + method.getName();
        Integer id = methodIds.get(name);
        if (id == null) {
            id = methodIds.size() + 1;
            methodIds.put(name, id);
            StringBuilder line = new StringBuilder("{\"m\":").append(id).append(",\"n\":");
            out.write(quote(line, name).append("}\n").toString());
        }
        methodCache.put(method, id);
        return id;
    }

    private int stack(RecordedStackTrace trace) throws IOException {
        Integer cached = stackCache.get(trace);
        if (cached != null) return cached;
        StringBuilder frames = new StringBuilder();
        for (RecordedFrame frame : trace.getFrames()) {
            RecordedMethod method = frame.getMethod();
            if (method == null) continue;
            if (!frames.isEmpty()) frames.append(',');
            frames.append(method(method));
        }
        String key = frames + (trace.isTruncated() ? "+" : "");
        Integer id = stackIds.get(key);
        if (id == null) {
            id = stackIds.size() + 1;
            stackIds.put(key, id);
            StringBuilder line = new StringBuilder("{\"s\":").append(id).append(",\"f\":[").append(frames).append(']');
            if (trace.isTruncated()) line.append(",\"x\":1");
            out.write(line.append("}\n").toString());
        }
        stackCache.put(trace, id);
        return id;
    }

    private static long nanos(Instant time) {
        return Math.addExact(Math.multiplyExact(time.getEpochSecond(), 1_000_000_000L), time.getNano());
    }

    private static StringBuilder quote(StringBuilder out, String text) {
        out.append('"');
        for (int i = 0; i < text.length(); i++) {
            char c = text.charAt(i);
            switch (c) {
                case '"' -> out.append("\\\"");
                case '\\' -> out.append("\\\\");
                case '\n' -> out.append("\\n");
                case '\r' -> out.append("\\r");
                case '\t' -> out.append("\\t");
                default -> {
                    if (c < 0x20 || Character.isSurrogate(c)) out.append(String.format("\\u%04x", (int) c));
                    else out.append(c);
                }
            }
        }
        return out.append('"');
    }
}
