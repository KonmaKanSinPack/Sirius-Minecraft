package io.sirius.bridge;

import net.neoforged.fml.common.Mod;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Sirius Bridge - the "eyes and hands" of the Sirius AI companion on the real
 * Minecraft client. Skeleton entry point only; functionality (screenshot,
 * input injection, event push) will be added in later tasks (see
 * sirius-technical.md §8.2).
 */
@Mod(SiriusBridge.MOD_ID)
public class SiriusBridge {

    public static final String MOD_ID = "sirius_bridge";
    public static final Logger LOGGER = LoggerFactory.getLogger(MOD_ID);

    public SiriusBridge() {
        LOGGER.info("Sirius Bridge loaded (skeleton, no functionality yet)");
    }
}
