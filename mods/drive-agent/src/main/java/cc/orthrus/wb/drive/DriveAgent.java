package cc.orthrus.wb.drive;

import net.minecraftforge.fml.common.Mod;
import net.minecraftforge.fml.common.event.FMLInitializationEvent;

import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

/**
 * The Workbench drive agent: views captured when they settle ({@link Capture}) and requests a session writes to an
 * inbox answered in the game ({@link Driver}). Client only, accepted by any server, and idle unless {@code
 * wb.capture} or {@code wb.driver} is set.
 */
@Mod(modid = DriveAgent.ID, useMetadata = true, clientSideOnly = true, acceptableRemoteVersions = "*")
public final class DriveAgent {
    public static final String ID = "wbdrive";
    static final Logger LOG = LogManager.getLogger("WbDrive");

    @Mod.EventHandler
    public void init(FMLInitializationEvent event) {
        LOG.info(
                "WB DRIVE agent: capture={} tour={} settle={} ticks, driver={}",
                Capture.ON,
                Capture.TOUR,
                Capture.SETTLE_TICKS,
                Driver.ON);
    }
}
