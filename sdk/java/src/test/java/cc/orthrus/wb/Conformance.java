package cc.orthrus.wb;

import java.time.Duration;
import java.util.Map;

/** The shared conformance checks against a live daemon (tests/test_sdks.py starts one and sets WB_URL, WB_TOKEN). */
public final class Conformance {
    static void check(boolean condition, String what) {
        if (!condition) throw new AssertionError(what);
    }

    public static void main(String[] args) {
        Client wb = Client.discover();
        Envelope echo = wb.submit("selftest.echo", Map.of("text", "java", "times", 2)).result(Duration.ofSeconds(60));
        check("wb/result/v1".equals(echo.schema()), "schema");
        check("ok".equals(echo.outcome()) && echo.exitCode() == 0, "echo ok: " + echo.summary());
        check("javajava".equals(echo.data().get("text")), "typed data");
        Envelope finding = wb.submit("selftest.fail", Map.of("outcome", "finding")).result(Duration.ofSeconds(60));
        check(finding.code() == 1L, "finding code");
        Envelope usage = wb.submit("selftest.echo", Map.of("times", 0)).result(Duration.ofSeconds(60));
        check("usage".equals(usage.outcome()), "usage");
        Job spend = wb.submit("selftest.spend", Map.of("price", 0));
        String quote = null;
        try {
            spend.result(Duration.ofSeconds(60));
            check(false, "expected ApprovalRequired");
        } catch (ApprovalRequired pending) {
            check(pending.prompt().get("question") != null, "prompt");
            quote = String.valueOf(pending.quote().get("id"));
        }
        spend.approve(quote, "relayed");
        check("ok".equals(spend.result(Duration.ofSeconds(60)).outcome()), "approved spend settles ok");
        try {
            wb.submit("selftest.nothing", Map.of());
            check(false, "expected a 404");
        } catch (WbException missing) {
            check(missing.status() == 404, "unknown command is 404");
        }
        System.out.println("java conformance: ok");
    }
}
