// generated from schema/envelope.schema.json by schema/generate.py; do not edit
package cc.orthrus.wb;

import java.util.Map;

/** wb/result/v1: the one shape of every job, at every state. code equals the CLI exit code once settled. */
public final class Envelope {
    private final Map<String, Object> raw;

    public Envelope(Map<String, Object> raw) {
        this.raw = raw;
    }

    public Map<String, Object> raw() {
        return raw;
    }

    /** schema */
    public String schema() {
        return Json.asString(raw.get("schema"));
    }

    /** job */
    public String job() {
        return Json.asString(raw.get("job"));
    }

    /** command */
    public String command() {
        return Json.asString(raw.get("command"));
    }

    /** command_version */
    public String commandVersion() {
        return Json.asString(raw.get("command_version"));
    }

    /** module */
    public String module() {
        return Json.asString(raw.get("module"));
    }

    /** module_version */
    public String moduleVersion() {
        return Json.asString(raw.get("module_version"));
    }

    /** params */
    public java.util.Map<String, Object> params() {
        return Json.asMap(raw.get("params"));
    }

    /** params_hash */
    public String paramsHash() {
        return Json.asString(raw.get("params_hash"));
    }

    /** state */
    public String state() {
        return Json.asString(raw.get("state"));
    }

    /** quoting while a spends command's quote is being made */
    public String phase() {
        return Json.asString(raw.get("phase"));
    }

    /** the label the submitter gave itself (WB_CALLER); never enforced */
    public String caller() {
        return Json.asString(raw.get("caller"));
    }

    /** the named locks the submit asked for, taken with the command's own, all or none */
    public java.util.List<Object> locks() {
        return Json.asList(raw.get("locks"));
    }

    /** why a queued job has not started: budget, behind <job>, gpu or lock <name> held by <job> (with every lock it takes, all or none, and those its submit asked for), quiet, approval */
    public java.util.Map<String, Object> waiting() {
        return Json.asMap(raw.get("waiting"));
    }

    /** outcome */
    public String outcome() {
        return Json.asString(raw.get("outcome"));
    }

    /** code */
    public Long code() {
        return Json.asLong(raw.get("code"));
    }

    /** summary */
    public String summary() {
        return Json.asString(raw.get("summary"));
    }

    /** reasons */
    public java.util.List<Object> reasons() {
        return Json.asList(raw.get("reasons"));
    }

    /** inputs */
    public java.util.List<Object> inputs() {
        return Json.asList(raw.get("inputs"));
    }

    /** outputs */
    public java.util.List<Object> outputs() {
        return Json.asList(raw.get("outputs"));
    }

    /** checked against the command's declared result schema */
    public java.util.Map<String, Object> data() {
        return Json.asMap(raw.get("data"));
    }

    /** measurements */
    public java.util.List<Object> measurements() {
        return Json.asList(raw.get("measurements"));
    }

    /** findings */
    public java.util.List<Object> findings() {
        return Json.asList(raw.get("findings"));
    }

    /** machine */
    public java.util.Map<String, Object> machine() {
        return Json.asMap(raw.get("machine"));
    }

    /** toolchain */
    public java.util.Map<String, Object> toolchain() {
        return Json.asMap(raw.get("toolchain"));
    }

    /** timings */
    public java.util.Map<String, Object> timings() {
        return Json.asMap(raw.get("timings"));
    }

    /** cost */
    public java.util.Map<String, Object> cost() {
        return Json.asMap(raw.get("cost"));
    }

    /** cache */
    public java.util.Map<String, Object> cache() {
        return Json.asMap(raw.get("cache"));
    }

    /** approval */
    public java.util.Map<String, Object> approval() {
        return Json.asMap(raw.get("approval"));
    }

    /** children */
    public java.util.List<Object> children() {
        return Json.asList(raw.get("children"));
    }

    /** the job that submitted this one, if any */
    public String parent() {
        return Json.asString(raw.get("parent"));
    }

    /** logs */
    public java.util.Map<String, Object> logs() {
        return Json.asMap(raw.get("logs"));
    }

    public boolean settled() {
        return "settled".equals(state());
    }

    public int exitCode() {
        String state = state();
        if ("settled".equals(state)) return code() == null ? 3 : code().intValue();
        if ("cancelled".equals(state) || "interrupted".equals(state)) return 4;
        if ("awaiting_approval".equals(state)) return 6;
        return 5;
    }

    public static final String SCHEMA_ID = "https://orthrus.cc/schemas/wb/result/v1";
}
