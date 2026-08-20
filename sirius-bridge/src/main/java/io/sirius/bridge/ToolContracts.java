package io.sirius.bridge;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.PriorityQueue;
import java.util.Set;

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

    /**
     * Validated {@code world.query} params. {@code filter} is the M3.5 v1.1
     * extension: {@code null} = absent (v1.0 behaviour); otherwise 1..
     * {@link #MAX_FILTER_ENTRIES} already-NORMALIZED entries (missing
     * namespace got {@code minecraft:} prefixed here, {@code #} tag prefix
     * kept, e.g. {@code ["minecraft:spruce_log", "#minecraft:logs"]}).
     */
    public record WorldQueryParams(String type, double range, List<String> filter) {
    }

    /** Default scan radius (blocks) when the caller does not send one. */
    public static final double DEFAULT_RANGE = 16;

    /** Hard cap on the scan radius - protects the response from exploding. */
    public static final double MAX_RANGE = 64;

    /** Max entries in a {@code world.query} filter array (v1.1). */
    public static final int MAX_FILTER_ENTRIES = 16;

    /** Max length of one {@code world.query} filter entry, in chars (v1.1). */
    public static final int MAX_FILTER_ENTRY_CHARS = 128;

    /**
     * Shape of one filter entry: an optional {@code #} tag marker, then a
     * registry location - a non-empty path of {@code [a-z0-9_.-/]} optionally
     * preceded by a {@code [a-z0-9_.-]+:} namespace. Mirrors the charset
     * {@code ResourceLocation} accepts, so anything that survives here can be
     * resolved in-game without a runtime exception; UPPERCASE and blank
     * entries are rejected up front with a clear message instead.
     */
    private static final java.util.regex.Pattern FILTER_ENTRY =
            java.util.regex.Pattern.compile("[a-z0-9_.-]+(?::[a-z0-9_./-]+)?");

    /**
     * Validates {@code world.query} params: type "blocks"/"entities", 0 < range <= 64 (default 16);
     * optional {@code filter} array (v1.1) of 1..16 strings, each 1..128 chars
     * in registry-id or {@code #tag} form. Entries are returned NORMALIZED:
     * a missing namespace gets {@code minecraft:} prefixed ({@code spruce_log}
     * -> {@code minecraft:spruce_log}, {@code #logs} -> {@code #minecraft:logs}).
     * {@code #tag} entries are only legal for {@code type:"blocks"} - the
     * entities branch matches entity-type registry ids only.
     */
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

        // v1.1 filter: null/absent keeps the v1.0 behaviour; otherwise the
        // entries are validated and normalized here so the scan side never
        // has to re-parse strings on the render thread.
        List<String> filter = null;
        JsonElement filterElement = params.get("filter");
        if (filterElement != null && !filterElement.isJsonNull()) {
            if (!filterElement.isJsonArray()) {
                throw new InvalidParams("filter must be an array of strings (registry ids like"
                        + " \"spruce_log\" or tags like \"#minecraft:logs\"), 1.." + MAX_FILTER_ENTRIES + " entries");
            }
            JsonArray entries = filterElement.getAsJsonArray();
            if (entries.size() < 1) {
                throw new InvalidParams("filter must contain at least 1 entry"
                        + " (omit filter entirely for the unfiltered v1.0 behaviour)");
            }
            if (entries.size() > MAX_FILTER_ENTRIES) {
                throw new InvalidParams("filter must contain at most " + MAX_FILTER_ENTRIES
                        + " entries, got: " + entries.size());
            }
            filter = new ArrayList<>(entries.size());
            for (JsonElement entry : entries) {
                if (entry == null || !entry.isJsonPrimitive() || !entry.getAsJsonPrimitive().isString()) {
                    throw new InvalidParams("filter entries must be strings (registry ids like"
                            + " \"spruce_log\" or tags like \"#minecraft:logs\")");
                }
                String raw = entry.getAsString();
                if (raw.length() < 1) {
                    throw new InvalidParams("filter entries must not be empty");
                }
                if (raw.length() > MAX_FILTER_ENTRY_CHARS) {
                    throw new InvalidParams("filter entries must be at most " + MAX_FILTER_ENTRY_CHARS
                            + " chars, got: " + raw.length());
                }
                boolean tag = raw.startsWith("#");
                String id = tag ? raw.substring(1) : raw;
                if (id.isEmpty() || !FILTER_ENTRY.matcher(id).matches()) {
                    throw new InvalidParams("filter entry \"" + raw + "\" is not a valid registry id or #tag"
                            + " (expected lowercase [a-z0-9_.-] with optional namespace, e.g."
                            + " \"spruce_log\", \"minecraft:spruce_log\", \"#minecraft:logs\")");
                }
                if (tag && "entities".equals(type)) {
                    // EntityType-tag matching is a deliberate non-goal for v1.1
                    // (collect_block needs block tags only); reject loudly
                    // instead of silently returning nothing.
                    throw new InvalidParams("filter #tag entries are not supported for type \"entities\""
                            + " (use entity type ids like \"minecraft:zombie\")");
                }
                // Namespace completion: a bare path lives in the minecraft
                // namespace, exactly like commands do (#logs -> minecraft:logs).
                filter.add((tag ? "#" : "") + (id.indexOf(':') >= 0 ? id : "minecraft:" + id));
            }
        }
        return new WorldQueryParams(type, range, filter);
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
     * Max entries returned for a FILTERED blocks scan (v1.1). A filter narrows
     * the world down to "targets", and the caller (e.g. collect_block) wants
     * the NEAREST ones - 32 slots is plenty for one decision round while
     * keeping the response small even inside a forest of tag matches.
     */
    public static final int FILTERED_BLOCKS_CAP = 32;

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
     * Tag membership test for the filtered block scan: is the block TYPE named
     * {@code blockName} ("minecraft:oak_log") inside tag {@code tagId}
     * ("minecraft:logs")? Name-keyed (not position-keyed) because block tags
     * live on the registry holder, i.e. every state of one block type answers
     * identically - letting the implementation memoize per distinct name and
     * reuse the name the probe already resolved. Implemented over
     * {@code BlockState.is(TagKey)} in PerceptionTools; may be {@code null}
     * when the filter contains no {@code #tag} entries (never invoked then).
     */
    @FunctionalInterface
    public interface TagProbe {
        boolean blockHasTag(String blockName, String tagId);
    }

    /**
     * A parsed block filter (v1.1): explicit registry ids + {@code #tag} ids
     * (both already namespace-normalized by {@link #worldQueryParams}). A
     * block matches when its registry name is in {@code ids} OR it is a
     * member of any tag in {@code tagIds}.
     *
     * @param ids    full registry ids, e.g. {@code minecraft:spruce_log}
     * @param tagIds tag ids without the {@code #}, e.g. {@code minecraft:logs}
     */
    public record BlockFilter(Set<String> ids, List<String> tagIds) {

        /** Splits normalized filter entries into id set + tag list. */
        public static BlockFilter parse(List<String> normalizedEntries) {
            Set<String> ids = new LinkedHashSet<>();
            List<String> tags = new ArrayList<>();
            for (String entry : normalizedEntries) {
                if (entry.startsWith("#")) {
                    tags.add(entry.substring(1));
                } else {
                    ids.add(entry);
                }
            }
            return new BlockFilter(ids, List.copyOf(tags));
        }
    }

    /** One match of a filtered scan, ordered by {@code distSq} to the player. */
    private record BlockHit(double distSq, int x, int y, int z, String name) {
    }

    /**
     * Cubic scan around ({@code cx,cy,cz}) with radius {@code range} blocks,
     * listing non-air blocks as {@code {x,y,z,block}}. Stops at
     * {@link #BLOCKS_CAP} entries and flags {@code truncated:true}.
     *
     * <p>v1.0 path - byte-for-byte the pre-filter behaviour; kept separate so
     * the default call cannot drift.
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

    /**
     * FILTERED cubic scan (v1.1): same cube as the v1.0 scan around the
     * player's exact position ({@code floor(px)..}, radius {@code ceil(range)}),
     * but only blocks matching {@code filter} are reported, ordered by squared
     * distance to the player (block CENTER {@code x+0.5,y+0.5,z+0.5} vs
     * {@code px,py,pz} - the same point lookAt aims at), ascending. Cap
     * {@link #FILTERED_BLOCKS_CAP}; more matches than that -> the NEAREST ones
     * win and {@code truncated:true}.
     *
     * <p><b>Why the whole cube is still probed:</b> scan order is x-then-y-then-z,
     * not distance order, so breaking early at 32 matches (like the unfiltered
     * path does at 512) would keep an arbitrary spatial corner, not the nearest
     * blocks - the very bug this extension exists to avoid. Instead a bounded
     * max-heap (root = farthest kept) holds at most 32 hits: once full, a new
     * match only enters when strictly nearer than the root, evicting it. Every
     * position still gets {@code probe.blockAt} called (we cannot know a far
     * position is a dropped MATCH without probing it, and truncated must only
     * flag dropped matches), but bookkeeping per position stays O(1) and the
     * response is bounded regardless of how many blocks match.
     */
    public static JsonObject scanBlocks(double px, double py, double pz, double range,
                                        BlockProbe probe, BlockFilter filter, TagProbe tagProbe) {
        int cx = (int) Math.floor(px);
        int cy = (int) Math.floor(py);
        int cz = (int) Math.floor(pz);
        int r = (int) Math.ceil(range);
        // max-heap on distSq: the root is the farthest of the kept matches,
        // i.e. the eviction candidate once the heap reaches the cap.
        PriorityQueue<BlockHit> nearest = new PriorityQueue<>(
                Comparator.comparingDouble(BlockHit::distSq).reversed());
        boolean truncated = false;
        for (int x = cx - r; x <= cx + r; x++) {
            for (int y = cy - r; y <= cy + r; y++) {
                for (int z = cz - r; z <= cz + r; z++) {
                    String name = probe.blockAt(x, y, z);
                    if (name == null) {
                        continue; // air / unloaded
                    }
                    boolean match = filter.ids().contains(name);
                    if (!match && !filter.tagIds().isEmpty()) {
                        for (String tagId : filter.tagIds()) {
                            if (tagProbe.blockHasTag(name, tagId)) {
                                match = true;
                                break;
                            }
                        }
                    }
                    if (!match) {
                        continue;
                    }
                    double dx = x + 0.5 - px;
                    double dy = y + 0.5 - py;
                    double dz = z + 0.5 - pz;
                    BlockHit hit = new BlockHit(dx * dx + dy * dy + dz * dz, x, y, z, name);
                    if (nearest.size() >= FILTERED_BLOCKS_CAP) {
                        truncated = true; // this match or the evicted one is dropped
                        if (hit.distSq() >= nearest.peek().distSq()) {
                            continue; // not nearer than the 32 kept - stays dropped
                        }
                        nearest.poll();
                    }
                    nearest.add(hit);
                }
            }
        }

        // Ascending distance; ties broken by (x,y,z) so the order is
        // deterministic (symmetric positions share distSq all the time).
        List<BlockHit> hits = new ArrayList<>(nearest);
        hits.sort(Comparator.comparingDouble(BlockHit::distSq)
                .thenComparingInt(BlockHit::x)
                .thenComparingInt(BlockHit::y)
                .thenComparingInt(BlockHit::z));
        JsonArray blocks = new JsonArray();
        for (BlockHit hit : hits) {
            JsonObject block = new JsonObject();
            block.addProperty("x", hit.x());
            block.addProperty("y", hit.y());
            block.addProperty("z", hit.z());
            block.addProperty("block", hit.name());
            blocks.add(block);
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
     * {@link #ENTITIES_CAP} entries. {@code typeFilter} (v1.1, may be null =
     * keep every entity) holds normalized entity-type registry ids
     * ({@code minecraft:zombie}); an entity passes only when its
     * {@code EntityType.getKey} id is in the set.
     *
     * <p>{@code truncated} (new in v1.1 - previously the cap silently dropped
     * the tail) is true when at least one further in-range (and type-matching)
     * entity had to be dropped beyond the cap. Result order stays the
     * caller's fact order (render-list order); unlike blocks, entity lists
     * are small and the nearest-first contract is not part of this branch.
     */
    public static JsonObject filterEntities(List<EntityFact> facts, double cx, double cy, double cz,
                                            double range, List<String> typeFilter) {
        double maxDistSq = range * range;
        Set<String> wantedTypes = typeFilter == null ? null : new LinkedHashSet<>(typeFilter);
        JsonArray entities = new JsonArray();
        boolean truncated = false;
        for (EntityFact fact : facts) {
            if (wantedTypes != null && !wantedTypes.contains(fact.type())) {
                continue;
            }
            double dx = fact.x() - cx;
            double dy = fact.y() - cy;
            double dz = fact.z() - cz;
            if (dx * dx + dy * dy + dz * dz > maxDistSq) {
                continue;
            }
            if (entities.size() >= ENTITIES_CAP) {
                truncated = true; // one more in-range match than the cap allows
                break;
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
        result.addProperty("truncated", truncated);
        return result;
    }
}
