package cc.orthrus.wb;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** A small, strict JSON reader and writer, so the client needs nothing beyond the JDK. */
public final class Json {
    private final String text;
    private int at;

    private Json(String text) {
        this.text = text;
    }

    public static Object parse(String text) {
        Json reader = new Json(text);
        reader.space();
        Object value = reader.value();
        reader.space();
        if (reader.at != text.length()) throw new IllegalArgumentException("trailing characters at " + reader.at);
        return value;
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> parseObject(String text) {
        Object value = parse(text);
        if (!(value instanceof Map)) throw new IllegalArgumentException("expected a JSON object");
        return (Map<String, Object>) value;
    }

    private void space() {
        while (at < text.length() && Character.isWhitespace(text.charAt(at))) at++;
    }

    private Object value() {
        if (at >= text.length()) throw new IllegalArgumentException("unexpected end");
        char c = text.charAt(at);
        switch (c) {
            case '{': return object();
            case '[': return array();
            case '"': return string();
            case 't': return literal("true", Boolean.TRUE);
            case 'f': return literal("false", Boolean.FALSE);
            case 'n': return literal("null", null);
            default: return number();
        }
    }

    private Object literal(String word, Object value) {
        if (!text.startsWith(word, at)) throw new IllegalArgumentException("bad literal at " + at);
        at += word.length();
        return value;
    }

    private Map<String, Object> object() {
        Map<String, Object> out = new LinkedHashMap<>();
        at++;
        space();
        if (text.charAt(at) == '}') { at++; return out; }
        while (true) {
            space();
            String key = string();
            space();
            if (text.charAt(at++) != ':') throw new IllegalArgumentException("expected : at " + (at - 1));
            space();
            out.put(key, value());
            space();
            char c = text.charAt(at++);
            if (c == '}') return out;
            if (c != ',') throw new IllegalArgumentException("expected , or } at " + (at - 1));
        }
    }

    private List<Object> array() {
        List<Object> out = new ArrayList<>();
        at++;
        space();
        if (text.charAt(at) == ']') { at++; return out; }
        while (true) {
            space();
            out.add(value());
            space();
            char c = text.charAt(at++);
            if (c == ']') return out;
            if (c != ',') throw new IllegalArgumentException("expected , or ] at " + (at - 1));
        }
    }

    private String string() {
        if (text.charAt(at) != '"') throw new IllegalArgumentException("expected a string at " + at);
        at++;
        StringBuilder out = new StringBuilder();
        while (true) {
            char c = text.charAt(at++);
            if (c == '"') return out.toString();
            if (c != '\\') { out.append(c); continue; }
            char e = text.charAt(at++);
            switch (e) {
                case 'n': out.append('\n'); break;
                case 't': out.append('\t'); break;
                case 'r': out.append('\r'); break;
                case 'b': out.append('\b'); break;
                case 'f': out.append('\f'); break;
                case 'u': out.append((char) Integer.parseInt(text.substring(at, at + 4), 16)); at += 4; break;
                default: out.append(e);
            }
        }
    }

    private Object number() {
        int start = at;
        while (at < text.length() && "+-0123456789.eE".indexOf(text.charAt(at)) >= 0) at++;
        String token = text.substring(start, at);
        if (token.isEmpty()) throw new IllegalArgumentException("unexpected character at " + start);
        if (token.contains(".") || token.contains("e") || token.contains("E")) return Double.parseDouble(token);
        return Long.parseLong(token);
    }

    public static String write(Object value) {
        StringBuilder out = new StringBuilder();
        write(out, value);
        return out.toString();
    }

    private static void write(StringBuilder out, Object value) {
        if (value == null) out.append("null");
        else if (value instanceof String s) quote(out, s);
        else if (value instanceof Number || value instanceof Boolean) out.append(value);
        else if (value instanceof Map<?, ?> map) {
            out.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> entry : map.entrySet()) {
                if (!first) out.append(',');
                first = false;
                quote(out, String.valueOf(entry.getKey()));
                out.append(':');
                write(out, entry.getValue());
            }
            out.append('}');
        } else if (value instanceof Iterable<?> items) {
            out.append('[');
            boolean first = true;
            for (Object item : items) {
                if (!first) out.append(',');
                first = false;
                write(out, item);
            }
            out.append(']');
        } else quote(out, value.toString());
    }

    private static void quote(StringBuilder out, String s) {
        out.append('"');
        for (char c : s.toCharArray()) {
            switch (c) {
                case '"': out.append("\\\""); break;
                case '\\': out.append("\\\\"); break;
                case '\n': out.append("\\n"); break;
                case '\r': out.append("\\r"); break;
                case '\t': out.append("\\t"); break;
                default:
                    if (c < 0x20) out.append(String.format("\\u%04x", (int) c));
                    else out.append(c);
            }
        }
        out.append('"');
    }

    static String asString(Object value) { return value == null ? null : value.toString(); }
    static Long asLong(Object value) { return value instanceof Number n ? n.longValue() : null; }
    static Double asDouble(Object value) { return value instanceof Number n ? n.doubleValue() : null; }
    static Boolean asBoolean(Object value) { return value instanceof Boolean b ? b : null; }
    static Object raw(Object value) { return value; }

    @SuppressWarnings("unchecked")
    static List<Object> asList(Object value) { return value instanceof List ? (List<Object>) value : List.of(); }

    @SuppressWarnings("unchecked")
    static Map<String, Object> asMap(Object value) { return value instanceof Map ? (Map<String, Object>) value : Map.of(); }
}
