package io.sirius.bridge;

import net.minecraft.client.Minecraft;
import net.neoforged.api.distmarker.Dist;
import net.neoforged.fml.loading.FMLEnvironment;
import net.neoforged.fml.loading.FMLPaths;
import net.neoforged.fml.common.Mod;
import net.neoforged.neoforge.client.event.ClientTickEvent;
import net.neoforged.neoforge.common.NeoForge;
import net.neoforged.neoforge.event.GameShuttingDownEvent;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.nio.file.Path;
import java.security.SecureRandom;

/**
 * Sirius Bridge - the "eyes and hands" of the Sirius AI companion on the real
 * Minecraft client (spec 8.2).
 *
 * <p>M1-B scope: runs a WebSocket server on {@code 127.0.0.1:<port>} (default
 * 8765, see {@code config/sirius_bridge.toml}) with a token handshake
 * ({@code hello}/{@code hello_ack}), capability negotiation
 * ({@code capabilities/list} from the frozen schema resources), a frame
 * dispatch skeleton ({@code request}/{@code task}, JSON-RPC style error codes)
 * and an audit log at {@code logs/sirius_bridge.log}. Real tool
 * implementations (screenshot/getStats/world.query/...) land in M1-C via
 * {@link ToolRegistry#register(String, ToolRegistry.Handler)}.
 *
 * <p>Lifecycle: the mod constructor only registers event listeners - the
 * server starts on the first client tick after the loading overlay is gone
 * (title screen reached), so mod/resource loading is never blocked, and shuts
 * down gracefully on {@link GameShuttingDownEvent}.
 */
@Mod(SiriusBridge.MOD_ID)
public class SiriusBridge {

    public static final String MOD_ID = "sirius_bridge";
    public static final Logger LOGGER = LoggerFactory.getLogger(MOD_ID);

    /** The bridge server once started (client side only; null before that). */
    private BridgeServer server;

    public SiriusBridge() {
        if (FMLEnvironment.dist != Dist.CLIENT) {
            LOGGER.info("sirius-bridge is client-only; nothing to do on dist {}", FMLEnvironment.dist);
            return;
        }
        NeoForge.EVENT_BUS.addListener(this::onClientTick);
        NeoForge.EVENT_BUS.addListener(this::onGameShuttingDown);
        LOGGER.info("sirius-bridge loaded (M1-B: WebSocket server + hello/token + capabilities + dispatch skeleton)");
    }

    /** Starts the server once the client finished its initial loading. */
    private void onClientTick(ClientTickEvent.Post event) {
        if (server != null) {
            return; // already started (listener stays registered but is a no-op)
        }
        Minecraft client = Minecraft.getInstance();
        if (client.getOverlay() != null) {
            return; // still in the initial loading overlay
        }
        start();
    }

    private void onGameShuttingDown(GameShuttingDownEvent event) {
        if (server != null) {
            server.shutdown();
            server = null;
        }
    }

    private void start() {
        try {
            Path gameDir = FMLPaths.GAMEDIR.get();
            Path configFile = FMLPaths.CONFIGDIR.get().resolve("sirius_bridge.toml");
            BridgeConfig config = BridgeConfig.load(configFile, new SecureRandom());
            if (!config.notes.isEmpty()) {
                LOGGER.info("sirius-bridge: config: {}", config.notes);
            }
            AuditLog audit = AuditLog.create(gameDir);
            server = new BridgeServer(config, audit);
            server.startAsync();
            LOGGER.info("sirius-bridge: connect with ws://127.0.0.1:{} (token: config/sirius_bridge.toml "
                    + "or logs/sirius_bridge.log)", config.port);
        } catch (Exception e) {
            LOGGER.error("sirius-bridge: failed to start the WebSocket server", e);
        }
    }

    /** Exposed for tests/diagnostics; may be null before the client finished loading. */
    public BridgeServer server() {
        return server;
    }
}
