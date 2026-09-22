package net.greenleaf.limbowake;

import com.google.inject.Inject;
import com.velocitypowered.api.event.Subscribe;
import com.velocitypowered.api.event.connection.DisconnectEvent;
import com.velocitypowered.api.event.player.ServerConnectedEvent;
import com.velocitypowered.api.event.proxy.ProxyInitializeEvent;
import com.velocitypowered.api.proxy.Player;
import com.velocitypowered.api.proxy.ProxyServer;
import com.velocitypowered.api.proxy.server.RegisteredServer;
import com.velocitypowered.api.scheduler.ScheduledTask;

import java.util.Optional;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.TimeUnit;

/**
 * LimboWake, der nahtlose Teil des Greenleaf on-demand MC-Netzes (Weg B, 2026-08-10).
 *
 * Kontext: Velocity 4 (spricht MC 26.2 nativ) + NanoLimbo (Warteraum) + Paper (survival, on-demand).
 * try = ["survival", "limbo"]: ist survival aus, landet der Spieler AUTOMATISCH im Limbo und bleibt
 * dort VERBUNDEN ("wird gestartet…"). Das Wecken uebernimmt host-seitig mc-wake-on-join (Velocity-Logs
 * -> wake-bridge). Dieses Plugin macht den letzten, nahtlosen Schritt: sobald survival oben ist, holt
 * es die im Limbo wartenden Spieler von selbst hinueber, kein Reconnect, kein /server-Tippen.
 *
 * Bewusst rein Velocity-intern: keine HTTP-Calls, keine Tokens, keine externen Deps (nur Velocity-API).
 * LimboAutoServer (die fertige Loesung) haengt an LimboAPI, das MC 26.x nicht kann -> dieses ~1-Datei-
 * Plugin umgeht den Blocker und gehoert uns.
 */
public class LimboWake {

    private final ProxyServer proxy;
    private final ConcurrentHashMap<UUID, ScheduledTask> pending = new ConcurrentHashMap<>();

    // Konfiguration per ENV (Defaults passen zum Compose). limbo/target = Servernamen aus velocity.toml.
    private final String limboName   = env("LIMBOWAKE_LIMBO", "limbo");
    private final String targetName  = env("LIMBOWAKE_TARGET", "survival");
    private final int pollSeconds    = envInt("LIMBOWAKE_POLL_SECONDS", 3);
    private final int maxWaitSeconds = envInt("LIMBOWAKE_MAX_WAIT_SECONDS", 180);

    @Inject
    public LimboWake(ProxyServer proxy) {
        this.proxy = proxy;
    }

    @Subscribe
    public void onInit(ProxyInitializeEvent e) {
        System.out.println("[limbowake] aktiv: haelt '" + limboName + "'-Spieler und holt sie nach '"
                + targetName + "', sobald es online ist (poll=" + pollSeconds + "s, maxWait=" + maxWaitSeconds + "s)");
    }

    @Subscribe
    public void onServerConnected(ServerConnectedEvent e) {
        String server = e.getServer().getServerInfo().getName();
        if (targetName.equals(server)) {          // schon (wieder) am Ziel -> nichts zu tun
            cancel(e.getPlayer().getUniqueId());
            return;
        }
        if (!limboName.equals(server)) {          // nur aus dem Warteraum heraus umziehen
            return;
        }
        Optional<RegisteredServer> target = proxy.getServer(targetName);
        if (target.isEmpty()) {
            System.out.println("[limbowake] Zielserver '" + targetName + "' ist nicht in velocity.toml registriert");
            return;
        }
        startWatch(e.getPlayer(), target.get());
    }

    @Subscribe
    public void onDisconnect(DisconnectEvent e) {
        cancel(e.getPlayer().getUniqueId());
    }

    /** Pollt das Ziel-Backend; sobald es antwortet (online), wird der Spieler nahtlos ueberfuehrt. */
    private void startWatch(Player player, RegisteredServer target) {
        UUID id = player.getUniqueId();
        cancel(id);   // evtl. alten Watcher fuer denselben Spieler beenden
        final long deadlineNanos = System.nanoTime() + maxWaitSeconds * 1_000_000_000L;

        ScheduledTask task = proxy.getScheduler().buildTask(this, () -> {
            if (!player.isActive()) { cancel(id); return; }                 // Spieler weg
            Optional<RegisteredServer> cur = player.getCurrentServer().map(sc -> sc.getServer());
            if (cur.isPresent() && targetName.equals(cur.get().getServerInfo().getName())) {
                cancel(id); return;                                          // schon drueben
            }
            if (System.nanoTime() > deadlineNanos) {
                System.out.println("[limbowake] " + player.getUsername() + ": '" + targetName
                        + "' kam nicht binnen " + maxWaitSeconds + "s -> aufgeben (Spieler bleibt im Limbo)");
                cancel(id); return;
            }
            // Ist survival schon erreichbar? ping() schlaegt fehl, solange das Backend bootet.
            target.ping().whenComplete((ping, ex) -> {
                if (ex != null || ping == null) return;                     // noch nicht bereit -> naechster Tick
                player.createConnectionRequest(target).connect().whenComplete((res, cex) -> {
                    if (cex == null && res != null && res.isSuccessful()) {
                        System.out.println("[limbowake] " + player.getUsername()
                                + " nahtlos von '" + limboName + "' nach '" + targetName + "' verschoben");
                        cancel(id);
                    }
                    // sonst: kurzer Rennfall (Backend fast bereit) -> naechster Tick versucht es erneut
                });
            });
        }).repeat(pollSeconds, TimeUnit.SECONDS).schedule();

        pending.put(id, task);
    }

    private void cancel(UUID id) {
        ScheduledTask t = pending.remove(id);
        if (t != null) t.cancel();
    }

    private static String env(String k, String d) {
        String v = System.getenv(k);
        return (v == null || v.isEmpty()) ? d : v;
    }

    private static int envInt(String k, int d) {
        try { return Integer.parseInt(env(k, String.valueOf(d))); } catch (Exception e) { return d; }
    }
}
