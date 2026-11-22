package cc.orthrus.wb.drive;

import com.google.gson.Gson;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;

import java.io.File;
import java.io.IOException;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardCopyOption;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.Iterator;
import java.util.List;
import java.util.Map;

import net.minecraft.client.Minecraft;
import net.minecraft.client.gui.GuiScreen;
import net.minecraft.client.settings.KeyBinding;
import net.minecraftforge.fml.common.Mod;
import net.minecraftforge.fml.common.ObfuscationReflectionHelper;
import net.minecraftforge.fml.common.eventhandler.SubscribeEvent;
import net.minecraftforge.fml.common.gameevent.TickEvent;
import net.minecraftforge.fml.relauncher.Side;

/**
 * Requests a session writes to an inbox, answered on the game's own thread. A request is a JSON file {@code
 * <id>.json} in {@code wb.driver.inbox} (default {@code <game>/wb-inbox}; write it under another name and rename it
 * in); the answer is {@code <id>.json} in {@code wb.driver.outbox} (default {@code <game>/wb-outbox}): {@code {"id",
 * "ok": true, "result"}} or {@code {"id", "ok": false, "error"}}. Requests are read every few ticks, in name order.
 *
 * <ul>
 *   <li>{@code {"action": "status"}}: the screen, whether a world is loaded, the player's place, frame rate, heap
 *   <li>{@code {"action": "screenshot", "name": "a.png"}}: the current frame, as Capture saves them
 *   <li>{@code {"action": "chat", "text": "/time set day"}}: a chat line or command from the player
 *   <li>{@code {"action": "key", "name": "key.jump", "ticks": 5}}: holds a key binding through the game's input
 *   <li>{@code {"action": "click", "x": 100, "y": 80, "button": 0}}: a click on the open screen, in its coordinates
 *   <li>{@code {"action": "close"}}: closes the open screen
 *   <li>{@code {"action": "quit"}}: stops the game
 * </ul>
 */
@Mod.EventBusSubscriber(modid = DriveAgent.ID, value = Side.CLIENT)
public final class Driver {
    static final boolean ON = Boolean.getBoolean("wb.driver");
    private static final int EVERY = 5;
    private static final Gson GSON = new Gson();
    private static final Map<KeyBinding, Integer> HELD = new HashMap<>();
    private static Method mouseClicked;
    private static Path inbox, outbox;
    private static long ticks;

    private Driver() {}

    @SubscribeEvent
    public static void tick(TickEvent.ClientTickEvent event) {
        if (!ON || event.phase != TickEvent.Phase.END) {
            return;
        }
        ticks++;
        release();
        if (ticks % EVERY != 0) {
            return;
        }
        Minecraft mc = Minecraft.getMinecraft();
        try {
            if (inbox == null) {
                inbox = folder("wb.driver.inbox", new File(mc.gameDir, "wb-inbox"));
                outbox = folder("wb.driver.outbox", new File(mc.gameDir, "wb-outbox"));
                DriveAgent.LOG.info("WB DRIVE inbox {} outbox {}", inbox, outbox);
            }
            for (Path request : pending()) {
                answer(mc, request);
            }
        } catch (IOException error) {
            DriveAgent.LOG.error("WB DRIVE cannot read its inbox: {}", error.toString());
        }
    }

    private static Path folder(String property, File fallback) throws IOException {
        String given = System.getProperty(property);
        Path path = given != null ? Paths.get(given) : fallback.toPath();
        Files.createDirectories(path);
        return path;
    }

    private static List<Path> pending() throws IOException {
        List<Path> found = new ArrayList<>();
        try (DirectoryStream<Path> stream = Files.newDirectoryStream(inbox, "*.json")) {
            for (Path path : stream) {
                found.add(path);
            }
        }
        found.sort(null);
        return found;
    }

    private static void answer(Minecraft mc, Path request) throws IOException {
        String file = request.getFileName().toString();
        String id = file.substring(0, file.length() - ".json".length());
        JsonObject reply = new JsonObject();
        reply.addProperty("id", id);
        try {
            JsonObject body = JsonParser.parseString(new String(Files.readAllBytes(request), StandardCharsets.UTF_8))
                    .getAsJsonObject();
            reply.add("result", handle(mc, body));
            reply.addProperty("ok", true);
        } catch (RuntimeException failure) {
            reply.addProperty("ok", false);
            reply.addProperty("error", failure.getClass().getSimpleName() + ": " + failure.getMessage());
        }
        Path part = outbox.resolve(id + ".json.part");
        Files.write(part, GSON.toJson(reply).getBytes(StandardCharsets.UTF_8));
        Files.move(part, outbox.resolve(id + ".json"), StandardCopyOption.REPLACE_EXISTING,
                StandardCopyOption.ATOMIC_MOVE);
        Files.deleteIfExists(request);
        DriveAgent.LOG.info("WB DRIVE answered {} ok={}", id, reply.get("ok").getAsBoolean());
    }

    private static JsonElement handle(Minecraft mc, JsonObject body) {
        String action = text(body, "action", null);
        if (action == null) {
            throw new IllegalArgumentException("a request names its action");
        }
        JsonObject result = new JsonObject();
        switch (action) {
            case "status":
                Runtime runtime = Runtime.getRuntime();
                result.addProperty("screen", mc.currentScreen == null ? null : mc.currentScreen.getClass().getName());
                result.addProperty("world", mc.world != null);
                result.addProperty("fps", Minecraft.getDebugFPS());
                result.addProperty("heap_mb", (runtime.totalMemory() - runtime.freeMemory()) >> 20);
                result.addProperty("ticks", ticks);
                if (mc.player != null) {
                    JsonObject player = new JsonObject();
                    player.addProperty("x", mc.player.posX);
                    player.addProperty("y", mc.player.posY);
                    player.addProperty("z", mc.player.posZ);
                    player.addProperty("yaw", mc.player.rotationYaw);
                    player.addProperty("pitch", mc.player.rotationPitch);
                    result.add("player", player);
                }
                return result;
            case "screenshot":
                String name = text(body, "name", "wb-shot-" + System.currentTimeMillis() + ".png");
                if (!name.matches("[A-Za-z0-9._-]{1,100}\\.png")) {
                    throw new IllegalArgumentException("a screenshot name is letters, digits and . _ - ending .png");
                }
                result.addProperty("path", Capture.save(mc, name).getAbsolutePath());
                return result;
            case "chat":
                if (mc.player == null) {
                    throw new IllegalStateException("chat needs a world");
                }
                mc.player.sendChatMessage(text(body, "text", ""));
                return result;
            case "key":
                KeyBinding binding = binding(mc, text(body, "name", ""));
                int hold = body.has("ticks") ? Math.max(1, body.get("ticks").getAsInt()) : 1;
                KeyBinding.setKeyBindState(binding.getKeyCode(), true);
                KeyBinding.onTick(binding.getKeyCode());
                HELD.put(binding, hold);
                result.addProperty("held_ticks", hold);
                return result;
            case "click":
                GuiScreen screen = mc.currentScreen;
                if (screen == null) {
                    throw new IllegalStateException("no screen is open");
                }
                click(screen, body.get("x").getAsInt(), body.get("y").getAsInt(),
                        body.has("button") ? body.get("button").getAsInt() : 0);
                result.addProperty("screen", screen.getClass().getName());
                return result;
            case "close":
                mc.displayGuiScreen(null);
                return result;
            case "quit":
                mc.shutdown();
                return result;
            default:
                throw new IllegalArgumentException("no action " + action
                        + " (status, screenshot, chat, key, click, close, quit)");
        }
    }

    private static String text(JsonObject body, String key, String fallback) {
        return body.has(key) && !body.get(key).isJsonNull() ? body.get(key).getAsString() : fallback;
    }

    private static KeyBinding binding(Minecraft mc, String name) {
        for (KeyBinding binding : mc.gameSettings.keyBindings) {
            if (binding.getKeyDescription().equals(name)) {
                return binding;
            }
        }
        throw new IllegalArgumentException("no key binding " + name + " (key.jump, key.forward, ...)");
    }

    private static void release() {
        for (Iterator<Map.Entry<KeyBinding, Integer>> it = HELD.entrySet().iterator(); it.hasNext(); ) {
            Map.Entry<KeyBinding, Integer> held = it.next();
            if (held.getValue() <= 1) {
                KeyBinding.setKeyBindState(held.getKey().getKeyCode(), false);
                it.remove();
            } else {
                held.setValue(held.getValue() - 1);
            }
        }
    }

    private static void click(GuiScreen screen, int x, int y, int button) {
        try {
            if (mouseClicked == null) { // GuiScreen.mouseClicked, by its SRG name: the same in development and play
                mouseClicked = ObfuscationReflectionHelper.findMethod(
                        GuiScreen.class, "func_73864_a", void.class, int.class, int.class, int.class);
            }
            mouseClicked.invoke(screen, x, y, button);
        } catch (ReflectiveOperationException failure) {
            throw new IllegalStateException("cannot click: " + failure, failure);
        }
    }
}
