package io.sirius.bridge;

import com.google.gson.JsonObject;

import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Tool registry: {@code method -> handler}. The frame dispatcher looks methods
 * up here and nothing else - adding a tool (M1-C screenshot/getStats/world.query
 * and beyond) means calling {@link #register(String, Handler)} at server
 * construction, with zero changes to the dispatcher.
 *
 * <p>Handlers are registered once during server start-up (before any
 * connection exists) and only read afterwards, which makes the registry
 * trivially thread-safe under the WebSocket server threads.
 */
public final class ToolRegistry {

    /**
     * One tool implementation. Receives the parsed params object plus a
     * {@link ToolContext} and returns the complete response frame to send back
     * (use {@link Json#okResponse}/{@link Json#errorResponse} helpers).
     */
    @FunctionalInterface
    public interface Handler {
        JsonObject handle(ToolContext context, JsonObject params) throws Exception;
    }

    private final Map<String, Handler> handlers = new ConcurrentHashMap<>();

    /** Registers (or replaces) the handler for a method name. */
    public ToolRegistry register(String method, Handler handler) {
        handlers.put(method, handler);
        return this;
    }

    /** The handler for {@code method}, or {@code null} when not implemented. */
    public Handler find(String method) {
        return handlers.get(method);
    }

    /** Currently implemented method names (diagnostics only). */
    public Set<String> methods() {
        return handlers.keySet();
    }
}
