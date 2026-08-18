package io.sirius.bridge;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;

import java.util.List;

/**
 * Frozen-contract logic for the three M1-C perception tools: parameter
 * validation (mirroring the JSON schemas in {@code sirius-brain/schema/tools})
 * and response assembly. Pure Gson + JDK only - no Minecraft classes - so the
 * whole thing is covered by the in-process smoke test ({@code SmokeMain}).
 *
 * <p>Validation failures throw {@link InvalidParams}; the tool shell maps
 * that to a {@code -32602} response.
 */
public final class ToolContracts {

    private ToolContracts() {
    }

    /** A schema violation in tool params; message goes into the -32602 response. */
    public static final class InvalidParams extends Exception {
        InvalidParams(String message) {
            super(message);
        }
    }

    // ------------------------------------------------------------------ params

    /** Validated {@code screenshot} params. {@code bbox} is [x,y,w,h] or null. */
    public record ScreenshotParams(String tier, int[] bbox, int quality) {
    }

    /** Default JPEG quality when the caller does not send one. */
    public static final int DEFAULT_QUALITY = 80;

    /**
     * Validates {@code screenshot} params: {@code tier} must be
     * "full"/"crop"; {@code bbox} (when present) an array of 4 finite numbers
     * with positive w/h (required for tier "crop"); {@code quality} an
     * integer 0..100 (default 80).
     */
    public static ScreenshotParams screenshotParams(JsonObject params) throws InvalidParams {
        JsonElement tierElement = params.get("tier");
        if (tierElement == null || !tierElement.isJsonPrimitive() || !tierElement.getAsJsonPrimitive().isString()) {
            throw new InvalidParams("screenshot requires string tier: \"full\"|\"crop\"");
        }
        String tier = tierElement.getAsString();
        if (!"full".equals(tier) && !"crop".equals(tier)) {
            throw new InvalidParams("screenshot tier must be \"full\" or \"crop\", got: " + tier);
        }

        int[] bbox = null;
        JsonElement bboxElement = params.get("bbox");
        if (bboxElement != null && !bboxElement.isJsonNull()) {
            if (!bboxElement.isJsonArray() || bboxElement.getAsJsonArray().size() != 4) {
                throw new InvalidParams("bbox must be an array [x, y, w, h] of 4 numbers");
            }
            bbox = new int[4];
            for (int i = 0; i < 4; i++) {
                JsonElement e = bboxElement.getAsJsonArray().get(i);
                if (e == null || !e.isJsonPrimitive() || !e.getAsJsonPrimitive().isNumber()) {
                    throw new InvalidParams("bbox[" + i + "] must be a number");
                }
                double v = e.getAsDouble();
                if (!Double.isFinite(v)) {
                    throw new InvalidParams("bbox[" + i + "] must be finite");
                }
                bbox[i] = (int) Math.round(v);
            }
            if (bbox[2] <= 0 || bbox[3] <= 0) {
                throw new InvalidParams("bbox w/h must be positive, got: ["
                        + bbox[0] + "," + bbox[1] + "," + bbox[2] + "," + bbox[3] + "]");
            }
        }
        if ("crop".equals(tier) && bbox == null) {
            throw new InvalidParams("bbox [x, y, w, h] is required when tier is \"crop\"");
        }

        int quality = DEFAULT_QUALITY;
        JsonElement qualityElement = params.get("quality");
        if (qualityElement != null && !qualityElement.isJsonNull()) {
            if (!qualityElement.isJsonPrimitive() || !qualityElement.getAsJsonPrimitive().isNumber()) {
                throw new InvalidParams("quality must be an integer 0..100");
            }
            double q = qualityElement.getAsDouble();
            if (q != Math.floor(q)) {
                throw new InvalidParams("quality must be an integer 0..100, got: " + q);
            }
            if (q < 0 || q > 100) {
                throw new InvalidParams("quality must be within 0..100, got: " + (int) q);
            }
            quality = (int) q;
        }
        return new ScreenshotParams(tier, bbox, quality);
    }

    /** Validated {@code world.query} params. */
    public record WorldQueryParams(String type, double range) {
    }

    /** Default scan radius (blocks) when the caller does not send one. */
    public static final double DEFAULT_RANGE = 16;

    /** Hard cap on the scan radius - protects the response from exploding. */
    public static final double MAX_RANGE = 64;

    /** Validated {@code world.query} params: type "blocks"/"entities", 0 < range <= 64 (default 16). */
    public static WorldQueryParams worldQueryParams(JsonObject params) throws InvalidParams {
        JsonElement typeElement = params.get("type");
        if (typeElement == null || !typeElement.isJsonPrimitive() || !typeElement.getAsJsonPrimitive().isString()) {
            throw new InvalidParams("world.query requires string type: \"blocks\"|\"entities\"");
        }
        String type = typeElement.getAsString();
        if (!"blocks".equals(type) && !"entities".equals(type)) {
            throw new InvalidParams("world.query type must be \"blocks\" or \"entities\", got: " + type);
        }

        double range = DEFAULT_RANGE;
        JsonElement rangeElement = params.get("range");
        if (rangeElement != null && !rangeElement.isJsonNull()) {
            if (!rangeElement.isJsonPrimitive() || !rangeElement.getAsJsonPrimitive().isNumber()) {
                throw new InvalidParams("range must be a positive number of blocks");
            }
            range = rangeElement.getAsDouble();
            if (!Double.isFinite(range)) {
                throw new InvalidParams("range must be finite");
            }
            if (range <= 0) {
                throw new InvalidParams("range must be > 0, got: " + range);
            }
            if (range > MAX_RANGE) {
                throw new InvalidParams("range must be <= " + (int) MAX_RANGE + " blocks, got: " + range);
            }
        }
        return new WorldQueryParams(type, range);
    }

    // ------------------------------------------------------------------ results

    /** {@code {"in_game": false}} - the shared "not in a world" answer (not an error). */
    public static JsonObject notInGame() {
        JsonObject result = new JsonObject();
        result.addProperty("in_game", false);
        return result;
    }

    /**
     * {@code screenshot} result:
     * {@code {"image_b64","format":"jpeg","width","height","taken_at","quality","downscaled"}}.
     */
    public static JsonObject screenshotResult(String imageBase64, int width, int height,
                                               long takenAtMs, int quality, boolean downscaled) {
        JsonObject result = new JsonObject();
        result.addProperty("image_b64", imageBase64);
        result.addProperty("format", "jpeg");
        result.addProperty("width", width);
        result.addProperty("height", height);
        result.addProperty("taken_at", takenAtMs);
        result.addProperty("quality", quality);
        result.addProperty("downscaled", downscaled);
        return result;
    }

    /** One active potion effect. */
    public record EffectFact(String id, int duration, int amplifier) {
    }

    /** Player stats extracted on the main thread (see PerceptionTools). */
    public record StatsSnapshot(float health, int food, float saturation, int air,
                                int xpLevel, float xpProgress,
                                double x, double y, double z,
                                String dimension, String gameMode,
                                List<EffectFact> effects, boolean alive) {
    }

    /** {@code getStats} result (in-game shape; see {@link #notInGame()} for the other one). */
    public static JsonObject statsResult(StatsSnapshot s) {
        JsonObject position = new JsonObject();
        position.addProperty("x", s.x());
        position.addProperty("y", s.y());
        position.addProperty("z", s.z());

        JsonArray effects = new JsonArray();
        for (EffectFact effect : s.effects()) {
            JsonObject e = new JsonObject();
            e.addProperty("id", effect.id());
            e.addProperty("duration", effect.duration());
            e.addProperty("amplifier", effect.amplifier());
            effects.add(e);
        }

        JsonObject result = new JsonObject();
        result.addProperty("in_game", true);
        result.addProperty("health", s.health());
        result.addProperty("food", s.food());
        result.addProperty("saturation", s.saturation());
        result.addProperty("air", s.air());
        result.addProperty("xp_level", s.xpLevel());
        result.addProperty("xp_progress", s.xpProgress());
        result.add("position", position);
        result.addProperty("dimension", s.dimension());
        result.addProperty("game_mode", s.gameMode());
        result.add("effects", effects);
        result.addProperty("alive", s.alive());
        return result;
    }

    // ------------------------------------------------------------------ world.query

    /** Max entries returned for a blocks scan. */
    public static final int BLOCKS_CAP = 512;

    /** Max entries returned for an entities query. */
    public static final int ENTITIES_CAP = 128;

    /**
     * Block access for {@link #scanBlocks(int, int, int, double, BlockProbe)}:
     * returns the block's registry name ("minecraft:stone") or {@code null}
     * for air / unloaded chunk. Implemented over {@code ClientLevel.getBlockState}
     * in PerceptionTools.
     */
    @FunctionalInterface
    public interface BlockProbe {
        String blockAt(int x, int y, int z);
    }

    /**
     * Cubic scan around ({@code cx,cy,cz}) with radius {@code range} blocks,
     * listing non-air blocks as {@code {x,y,z,block}}. Stops at
     * {@link #BLOCKS_CAP} entries and flags {@code truncated:true}.
     */
    public static JsonObject scanBlocks(int cx, int cy, int cz, double range, BlockProbe probe) {
        int r = (int) Math.ceil(range);
        JsonArray blocks = new JsonArray();
        boolean truncated = false;
        scan:
        for (int x = cx - r; x <= cx + r; x++) {
            for (int y = cy - r; y <= cy + r; y++) {
                for (int z = cz - r; z <= cz + r; z++) {
                    String name = probe.blockAt(x, y, z);
                    if (name == null) {
                        continue; // air / unloaded
                    }
                    if (blocks.size() >= BLOCKS_CAP) {
                        truncated = true;
                        break scan;
                    }
                    JsonObject block = new JsonObject();
                    block.addProperty("x", x);
                    block.addProperty("y", y);
                    block.addProperty("z", z);
                    block.addProperty("block", name);
                    blocks.add(block);
                }
            }
        }

        JsonObject result = new JsonObject();
        result.add("blocks", blocks);
        result.addProperty("count", blocks.size());
        result.addProperty("truncated", truncated);
        return result;
    }

    /** Entity data extracted on the main thread; {@code health} is NaN when unknown. */
    public record EntityFact(String uuid, String name, String type,
                             double x, double y, double z, float health) {
    }

    /**
     * Filters {@code facts} to those within {@code range} blocks of
     * ({@code cx,cy,cz}) (squared-3D-distance) and caps at
     * {@link #ENTITIES_CAP} entries.
     */
    public static JsonObject filterEntities(List<EntityFact> facts, double cx, double cy, double cz, double range) {
        double maxDistSq = range * range;
        JsonArray entities = new JsonArray();
        for (EntityFact fact : facts) {
            if (entities.size() >= ENTITIES_CAP) {
                break;
            }
            double dx = fact.x() - cx;
            double dy = fact.y() - cy;
            double dz = fact.z() - cz;
            if (dx * dx + dy * dy + dz * dz > maxDistSq) {
                continue;
            }
            JsonObject position = new JsonObject();
            position.addProperty("x", fact.x());
            position.addProperty("y", fact.y());
            position.addProperty("z", fact.z());

            JsonObject entity = new JsonObject();
            entity.addProperty("uuid", fact.uuid());
            entity.addProperty("name", fact.name());
            entity.addProperty("type", fact.type());
            entity.add("position", position);
            if (!Float.isNaN(fact.health())) {
                entity.addProperty("health", fact.health());
            }
            entities.add(entity);
        }

        JsonObject result = new JsonObject();
        result.add("entities", entities);
        result.addProperty("count", entities.size());
        return result;
    }
}
