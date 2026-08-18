package io.sirius.bridge;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Formatter;
import java.util.logging.FileHandler;
import java.util.logging.Level;
import java.util.logging.LogRecord;
import java.util.logging.Logger;

/**
 * Audit log for the bridge: one line per security- or protocol-relevant event
 * (server start/stop, connect/disconnect, hello success/failure, every
 * request), written to {@code logs/sirius_bridge.log} under the game directory.
 *
 * <p>Kept separate from the game's main log so the token line and connection
 * history are easy to find. The file is appended across sessions and is the
 * place where a freshly generated token is printed (one line, at startup).
 *
 * <p>Thread-safe: events arrive on the WebSocket server threads; JUL
 * synchronizes internally.
 */
public final class AuditLog {

    private static final String LOGGER_NAME = "sirius.bridge.audit";

    private final Logger logger;
    private final FileHandler fileHandler;

    private AuditLog(Logger logger, FileHandler fileHandler) {
        this.logger = logger;
        this.fileHandler = fileHandler;
    }

    /** Creates the audit log at {@code gameDir/logs/sirius_bridge.log} (UTF-8, append). */
    public static AuditLog create(Path gameDir) {
        Logger logger = Logger.getLogger(LOGGER_NAME);
        logger.setUseParentHandlers(false); // do not mirror into the game log
        try {
            Path logsDir = gameDir.resolve("logs");
            Files.createDirectories(logsDir);
            FileHandler handler = new FileHandler(logsDir.resolve("sirius_bridge.log").toString(), true);
            handler.setEncoding(java.nio.charset.StandardCharsets.UTF_8.name());
            handler.setFormatter(new OneLineFormatter());
            logger.addHandler(handler);
            logger.setLevel(Level.INFO);
            return new AuditLog(logger, handler);
        } catch (IOException e) {
            SiriusBridge.LOGGER.error("sirius-bridge: cannot open logs/sirius_bridge.log ({}); "
                    + "audit events fall back to the game log", e.toString());
            logger.setUseParentHandlers(true);
            return new AuditLog(logger, null);
        }
    }

    /** Logs one audit line: {@code timestamp EVENT key=value ...}. */
    public void event(String event, String detail) {
        logger.log(Level.INFO, (detail == null || detail.isEmpty()) ? event : event + " " + detail);
    }

    /** Flushes and releases the underlying file handler (on server shutdown). */
    public void close() {
        if (fileHandler != null) {
            fileHandler.flush();
            logger.removeHandler(fileHandler);
            fileHandler.close();
        }
    }

    /** Compact one-line format: {@code 2026-08-18 13:00:00.123 EVENT detail}. */
    private static final class OneLineFormatter extends java.util.logging.Formatter {
        @Override
        public String format(LogRecord record) {
            return new Formatter()
                    .format("%1$tF %1$tT.%1$tL %2$s%n", record.getMillis(), formatMessage(record))
                    .toString();
        }
    }
}
