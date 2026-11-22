package cc.orthrus.wb.drive;

import java.io.File;

import net.minecraft.client.Minecraft;
import net.minecraft.util.ScreenShotHelper;
import net.minecraftforge.client.event.RenderWorldLastEvent;
import net.minecraftforge.fml.common.Mod;
import net.minecraftforge.fml.common.eventhandler.SubscribeEvent;
import net.minecraftforge.fml.common.gameevent.TickEvent;
import net.minecraftforge.fml.relauncher.Side;

/**
 * Screenshots of settled views, never changing the world. One capture waits for the first settle; a tour ({@code
 * wb.capture.tour}) captures once at every new viewpoint, after the player has held still for the settle time, so a
 * session can step through viewpoints by teleporting. Each capture logs marker lines a session reads:
 *
 * <pre>
 * WB VIEW n fps=.. avgFps=.. renders=.. entities=.. heap=..MB
 * WB CAPTURE n &lt;path&gt;
 * WB HEAP AFTER GC ..MB
 * </pre>
 *
 * Properties: {@code wb.capture} (on), {@code wb.capture.tour}, {@code wb.capture.settleTicks} (400, at least 20),
 * {@code wb.capture.dir} (the folder whose {@code screenshots/} gets them; default the game folder).
 */
@Mod.EventBusSubscriber(modid = DriveAgent.ID, value = Side.CLIENT)
public final class Capture {
    static final boolean ON = Boolean.getBoolean("wb.capture");
    static final boolean TOUR = Boolean.getBoolean("wb.capture.tour");
    static final int SETTLE_TICKS = Math.max(20, Integer.getInteger("wb.capture.settleTicks", 400));
    private static final double MOVED = 0.05;    // blocks a tick: anything more is moving
    private static final double NEW_VIEW = 8;    // blocks from the last capture: a new viewpoint

    private static boolean captured, due;
    private static int still, views, frames, settleFrames;
    private static long settleStart;
    private static double lastX = Double.NaN, lastY, lastZ, shotX = Double.NaN, shotY, shotZ;

    private Capture() {}

    @SubscribeEvent
    public static void tick(TickEvent.ClientTickEvent event) {
        if (!ON || event.phase != TickEvent.Phase.END) {
            return;
        }
        Minecraft mc = Minecraft.getMinecraft();
        if (mc.player == null || mc.world == null) {
            return;
        }
        double x = mc.player.posX, y = mc.player.posY, z = mc.player.posZ;
        boolean moved = Double.isNaN(lastX)
                || Math.abs(x - lastX) + Math.abs(y - lastY) + Math.abs(z - lastZ) > MOVED;
        still = moved ? 0 : still + 1;
        if (still == SETTLE_TICKS / 4) { // the frame rate counts from a quarter of the settle on
            settleStart = System.nanoTime();
            settleFrames = frames;
        }
        lastX = x;
        lastY = y;
        lastZ = z;
        if (TOUR) {
            boolean fresh = Double.isNaN(shotX)
                    || Math.abs(x - shotX) + Math.abs(z - shotZ) > NEW_VIEW
                    || Math.abs(y - shotY) > NEW_VIEW;
            if (fresh && still >= SETTLE_TICKS) {
                shotX = x;
                shotY = y;
                shotZ = z;
                due = true;
            }
        } else if (!captured && still >= SETTLE_TICKS) {
            captured = true;
            due = true;
        }
    }

    @SubscribeEvent
    public static void render(RenderWorldLastEvent event) {
        frames++;
        if (!due) {
            return;
        }
        due = false;
        int view = views++;
        Minecraft mc = Minecraft.getMinecraft();
        Runtime runtime = Runtime.getRuntime();
        double seconds = Math.max(1e-6, (System.nanoTime() - settleStart) / 1e9);
        DriveAgent.LOG.info(
                "WB VIEW {} fps={} avgFps={} renders={} entities={} heap={}MB",
                view,
                Minecraft.getDebugFPS(),
                Math.round((frames - settleFrames) / seconds),
                mc.renderGlobal.getDebugInfoRenders(),
                mc.renderGlobal.getDebugInfoEntities(),
                (runtime.totalMemory() - runtime.freeMemory()) >> 20);
        String name = String.format("wb-view-%02d-%d.png", view, System.currentTimeMillis());
        DriveAgent.LOG.info("WB CAPTURE {} {}", view, save(mc, name).getAbsolutePath());
        System.gc(); // the heap the view keeps, without garbage left from loading it
        DriveAgent.LOG.info("WB HEAP AFTER GC {}MB", (runtime.totalMemory() - runtime.freeMemory()) >> 20);
    }

    /** A screenshot of the current frame, in the capture folder's screenshots/. */
    static File save(Minecraft mc, String name) {
        String dir = System.getProperty("wb.capture.dir");
        File base = dir != null ? new File(dir) : mc.gameDir;
        ScreenShotHelper.saveScreenshot(base, name, mc.displayWidth, mc.displayHeight, mc.getFramebuffer());
        return new File(new File(base, "screenshots"), name);
    }
}
