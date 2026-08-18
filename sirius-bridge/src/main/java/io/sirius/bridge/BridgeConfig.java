package io.sirius.bridge;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.SecureRandom;

/**
 * sirius-bridge configuration, stored as a flat {@code config/sirius_bridge.toml}.
 *
 * <p>Deliberately a tiny hand-rolled TOML subset (flat {@code key = "value"} /
 * {@code key = 123} lines, {@code #} comments) instead of the NeoForge config
 * system: only two keys exist, and the token must be (re)generated and
 * persisted on first launch regardless of config UIs.
 *
 * <p>On first launch a random token is generated, written to the file and
 * reported back so the caller can print it to the audit log.
 */
public final class BridgeConfig {

    public static final int DEFAULT_PORT = 8765;

    /** Resolved settings. */
    public final int port;
    public final String token;
    /** True when the token was freshly generated this launch (first run or token removed). */
    public final boolean tokenGenerated;
    /** Human-readable notes gathered while loading (logged by the caller, never fatal). */
    public final String notes;

    private BridgeConfig(int port, String token, boolean tokenGenerated, String notes) {
        this.port = port;
        this.token = token;
        this.tokenGenerated = tokenGenerated;
        this.notes = notes;
    }

    /**
     * Loads the config from {@code file}, creating/repairing it as needed.
     * Never throws: any problem degrades to defaults plus a note.
     */
    public static BridgeConfig load(Path file, SecureRandom random) {
        int port = DEFAULT_PORT;
        String token = null;
        StringBuilder notes = new StringBuilder();

        if (Files.exists(file)) {
            try {
                for (String rawLine : Files.readAllLines(file, StandardCharsets.UTF_8)) {
                    String line = rawLine.trim();
                    if (line.isEmpty() || line.startsWith("#")) {
                        continue;
                    }
                    int eq = line.indexOf('=');
                    if (eq < 0) {
                        continue;
                    }
                    String key = line.substring(0, eq).trim();
                    String value = unquote(line.substring(eq + 1).trim());
                    switch (key) {
                        case "port" -> {
                            try {
                                port = Integer.parseInt(value);
                                if (port < 1 || port > 65535) {
                                    notes.append("invalid port ").append(value)
                                            .append(", using default ").append(DEFAULT_PORT).append("; ");
                                    port = DEFAULT_PORT;
                                }
                            } catch (NumberFormatException e) {
                                notes.append("port not a number: ").append(value).append("; ");
                            }
                        }
                        case "token" -> {
                            if (!value.isEmpty()) {
                                token = value;
                            }
                        }
                        default -> {
                        }
                    }
                }
            } catch (IOException e) {
                notes.append("config unreadable (").append(e.getMessage()).append("); ");
            }
        } else {
            notes.append("config created; ");
        }

        boolean generated = false;
        if (token == null) {
            token = generateToken(random);
            generated = true;
            notes.append("token generated; ");
        }

        BridgeConfig config = new BridgeConfig(port, token, generated, notes.toString().trim());
        config.save(file);
        return config;
    }

    private void save(Path file) {
        String content = """
                # sirius-bridge configuration
                # The WebSocket server ALWAYS binds to 127.0.0.1 (loopback) only -
                # it is not reachable from the network.
                #
                # port : TCP port to listen on (default 8765).
                # token: shared secret for the hello handshake. To rotate it, delete
                #        the line (or set token = "") and restart the game; the new
                #        token is printed to logs/sirius_bridge.log on startup.
                port = %d
                token = "%s"
                """.formatted(port, token);
        try {
            Files.createDirectories(file.getParent());
            Files.writeString(file, content, StandardCharsets.UTF_8);
        } catch (IOException e) {
            SiriusBridge.LOGGER.error("Failed to write config {}: {}", file, e.toString());
        }
    }

    /** 64-char hex token from 32 random bytes. */
    private static String generateToken(SecureRandom random) {
        byte[] bytes = new byte[32];
        random.nextBytes(bytes);
        StringBuilder hex = new StringBuilder(bytes.length * 2);
        for (byte b : bytes) {
            hex.append(Character.forDigit((b >> 4) & 0xF, 16)).append(Character.forDigit(b & 0xF, 16));
        }
        return hex.toString();
    }

    /** Strips one pair of matching single/double quotes, if present. */
    private static String unquote(String value) {
        if (value.length() >= 2
                && ((value.startsWith("\"") && value.endsWith("\""))
                || (value.startsWith("'") && value.endsWith("'")))) {
            return value.substring(1, value.length() - 1);
        }
        return value;
    }
}
