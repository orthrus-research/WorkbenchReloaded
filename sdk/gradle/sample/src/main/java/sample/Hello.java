package sample;

/** The entry point the sample's WbRun tasks run: prints its arguments, or exits 3 when given {@code fail}. */
public final class Hello {
    private Hello() {
    }

    public static void main(String[] args) {
        if (args.length > 0 && args[0].equals("fail")) {
            System.out.println("failing as asked");
            System.exit(3);
        }
        System.out.println("hello " + String.join(" ", args));
    }
}
