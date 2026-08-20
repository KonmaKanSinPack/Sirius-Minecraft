package io.sirius.bridge;

import net.minecraft.client.Minecraft;
import net.minecraft.client.player.LocalPlayer;

import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/**
 * M3.5 v1.2 smooth-view-turn controller: advances the player's rotation
 * toward a target at a FIXED angular speed, one client tick at a time -
 * "转头像自然转头" instead of the instant {@code setYRot} snap.
 *
 * <p><b>Model:</b> exactly ONE turn is active at any time. A new turn
 * (smooth {@code lookAt}, or the aim phase of {@code dig}) SUPERSEDES the
 * active one - the old turn's waiter is released with
 * {@code converged:false} immediately; instant {@code look}/{@code lookAt}
 * supersede too (their direct rotation write would fight the ticking turn).
 * Per tick both axes step toward the target by
 * {@code speed * 50ms} degrees along the shortest signed difference
 * ({@link LookContracts#yawDelta} for yaw) - each axis moves at the same
 * fixed speed, so the axis with less distance arrives first and HOLDS while
 * the other catches up ("yaw/pitch 同步到达，先到的轴等待"). When both axes
 * sit within {@link LookContracts#TURN_CONVERGENCE_DEG}, the exact target
 * rotation is applied once - "误差 <1° 收口精确落位".
 *
 * <p><b>Threading:</b> all mutable state is confined to the client main
 * thread ({@link #onClientTick} runs from {@code SiriusBridge}'s
 * {@code ClientTickEvent.Post} listener; {@link #begin} and
 * {@link #supersedeActive} are called from inside main-thread tool tasks).
 * {@link Turn} instances are handed to WebSocket threads as read-only
 * handles: the volatile outcome flags plus a {@link CountDownLatch} let the
 * calling tool block for the outcome without touching turn state.
 *
 * <p><b>Rotation writes</b> reuse {@link LookTools#applyRotation} - the
 * vanilla {@code Entity.lookAt} statement sequence (setters + interpolation
 * fields + head rotation), applied every tick, so a LocalPlayer keeps
 * streaming PosRot packets to the server on its own schedule.
 */
final class TurnController {

    private TurnController() {
    }

    /**
     * One in-flight turn. Written by the main thread; read by the waiter.
     * {@code converged} is true ONLY when the exact target was reached;
     * supersession, world-exit and self-expiry all end with
     * {@code converged:false} so the caller can distinguish them via
     * {@link #elapsedMs()}.
     */
    static final class Turn {
        final double targetYaw;
        final double targetPitch;
        final double speedDegPerSec;
        final long startedNanos;
        private final CountDownLatch done = new CountDownLatch(1);
        volatile boolean converged;
        volatile boolean superseded;
        volatile double finalYaw;
        volatile double finalPitch;
        volatile long finishedNanos;

        Turn(double targetYaw, double targetPitch, double speedDegPerSec, long startedNanos) {
            this.targetYaw = targetYaw;
            this.targetPitch = targetPitch;
            this.speedDegPerSec = speedDegPerSec;
            this.startedNanos = startedNanos;
        }

        /** Blocks the calling (WS) thread until the turn ends or {@code timeoutMs} passes. */
        boolean await(long timeoutMs) throws InterruptedException {
            return done.await(timeoutMs, TimeUnit.MILLISECONDS);
        }

        /** True when the view reached the EXACT target (false on supersede/expiry). */
        boolean isConverged() {
            return converged;
        }

        /** True when this turn already ended (converged, superseded or expired). */
        boolean isFinished() {
            return done.getCount() == 0;
        }

        /** True when the turn's target still matches the given rotation (within eps). */
        boolean matchesTarget(double yaw, double pitch, double eps) {
            return Math.abs(LookContracts.yawDelta(targetYaw, yaw)) <= eps
                    && Math.abs(targetPitch - pitch) <= eps;
        }

        /** Turn duration in ms (tick granularity; measured start-of-begin to completion). */
        long elapsedMs() {
            long end = finishedNanos;
            return (end - startedNanos) / 1_000_000L;
        }

        /** ms since this turn began, measured against a live "now" (pre-finish). */
        long elapsedMsAlive(long now) {
            return (now - startedNanos) / 1_000_000L;
        }

        void finish(boolean converged, LocalPlayer player, long nanos) {
            this.converged = converged;
            this.finishedNanos = nanos;
            if (player != null) {
                this.finalYaw = player.getYRot();
                this.finalPitch = player.getXRot();
            }
            done.countDown();
        }
    }

    /** The single active turn; null when idle. Main-thread confined. */
    private static Turn active;

    /**
     * Starts (and supersedes into) a new turn. MUST run on the client main
     * thread (called from inside a tool's main-thread task). The returned
     * handle is safe to await from any thread.
     */
    static Turn begin(double targetYaw, double targetPitch, double speedDegPerSec) {
        supersedeActive();
        Turn turn = new Turn(targetYaw, targetPitch, speedDegPerSec, System.nanoTime());
        active = turn;
        return turn;
    }

    /**
     * Releases the active turn's waiter with {@code converged:false} and
     * clears it (a newer look superseded it, or an instant look/lookAt is
     * about to write the rotation directly). MUST run on the main thread.
     */
    static void supersedeActive() {
        Turn turn = active;
        if (turn != null) {
            turn.superseded = true;
            turn.finish(false, Minecraft.getInstance().player, System.nanoTime());
            active = null;
        }
    }

    /** The turn currently being advanced (null when idle); main thread only. */
    static Turn activeTurn() {
        return active;
    }

    /**
     * Per-tick advance; called from {@code SiriusBridge.onClientTick}. No-op
     * when idle. With no player (title screen / disconnect) the turn is
     * released unconverged.
     */
    static void onClientTick() {
        Turn turn = active;
        if (turn == null) {
            return;
        }
        LocalPlayer player = Minecraft.getInstance().player;
        long now = System.nanoTime();
        if (player == null) {
            turn.finish(false, null, now);
            active = null;
            return;
        }
        if (LookContracts.turnConverged(player.getYRot(), player.getXRot(),
                turn.targetYaw, turn.targetPitch)) {
            // 收口: land on the EXACT target rotation once, then stop.
            LookTools.applyRotation(player, turn.targetYaw, turn.targetPitch);
            turn.finish(true, player, now);
            active = null;
            return;
        }
        if (turn.elapsedMsAlive(now) > LookContracts.maxTurnMs(turn.speedDegPerSec)) {
            // Self-expiry: something else keeps writing the rotation (another
            // mod, a failed grab). Release the waiter instead of ticking forever.
            turn.finish(false, player, now);
            active = null;
            return;
        }
        double step = turn.speedDegPerSec * 0.050; // fixed 50 ms tick, fixed angular speed
        double yaw = LookContracts.approach(player.getYRot(),
                LookContracts.yawDelta(player.getYRot(), turn.targetYaw), step);
        double pitch = LookContracts.approach(player.getXRot(),
                turn.targetPitch - player.getXRot(), step);
        LookTools.applyRotation(player, yaw, pitch);
    }
}
