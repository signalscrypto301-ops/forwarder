import makeWASocket, {
    DisconnectReason,
    useMultiFileAuthState,
    fetchLatestBaileysVersion,
    makeCacheableSignalKeyStore,
    WASocket,
    AnyMessageContent,
    Browsers,
} from "@whiskeysockets/baileys";
import { Boom } from "@hapi/boom";
import pino from "pino";
import express from "express";
import bodyParser from "body-parser";
import "dotenv/config";
import fs from "fs";
import multer from "multer";
import path from "path";
import mime from "mime-types";
import sharp from "sharp";
import { HttpsProxyAgent } from "https-proxy-agent";
import { SocksProxyAgent } from "socks-proxy-agent";
import axios from "axios";

const app = express();
const clientId: string = process.env.CLIENT_ID || "user";
const apiSecret: string | undefined = process.env.API_SECRET;

app.use(bodyParser.json());

// ---------- API Authentication Middleware ----------
if (apiSecret) {
    app.use((req, res, next) => {
        if (req.path === "/health") {
            return next();
        }
        const clientKey = req.headers["x-api-key"] || req.query.apiKey;
        if (clientKey !== apiSecret) {
            return res.status(401).json({ message: "Unauthorized: Invalid or missing API key" });
        }
        next();
    });
}

// ---------- Session Data Structure ----------
interface SessionEntry {
    sock?: WASocket;
    isReady: boolean;
    currentQR?: string;
    qrListeners: Array<(qr: string) => void>;
    readyListeners: Array<() => void>;
    authFailureListeners: Array<(err: any) => void>;
    isRestarting: boolean;
    restartTimer?: NodeJS.Timeout;
    isListening: boolean;
    phoneNumber?: string;
    pushName?: string;
    connectedAt?: string;
    hasProxy?: boolean;
    proxyHost?: string;
    proxyExitIp?: string;
    proxyCountry?: string;
    proxyCountryCode?: string;
    proxyLatencyMs?: number;
    proxyStatus?: "healthy" | "failed" | "direct";
    qrTimestamp?: number;
}

const sessions: Record<string, SessionEntry> = {};

// Configured multi-account slots (up to 4 concurrent accounts)
const CONFIGURED_ACCOUNTS = [
    { id: "user", index: 1, label: "Account 1" },
    { id: "account2", index: 2, label: "Account 2" },
    { id: "account3", index: 3, label: "Account 3" },
    { id: "account4", index: 4, label: "Account 4" },
];

// ---------- Real-Time Disconnect & Auth Failure Events Queue ----------
interface DisconnectEvent {
    eventId: string;
    clientId: string;
    index: number;
    label: string;
    phone: string | null;
    reason: string;
    statusCode: number;
    timestamp: string;
}

const disconnectEvents: DisconnectEvent[] = [];
const lastKnownPhones: Record<string, string> = {};
const lastDisconnectAlertTime: Record<string, number> = {};

function recordDisconnectEvent(safeId: string, statusCode: number = 401, reason: string = "Session Logged Out or Banned"): DisconnectEvent {
    const acc = CONFIGURED_ACCOUNTS.find((a) => a.id === safeId) || { id: safeId, index: 1, label: safeId };
    const phone = lastKnownPhones[safeId] || sessions[safeId]?.phoneNumber || null;
    const now = Date.now();

    const event: DisconnectEvent = {
        eventId: `evt_${now}_${safeId}`,
        clientId: safeId,
        index: acc.index,
        label: acc.label,
        phone,
        reason,
        statusCode,
        timestamp: new Date().toISOString(),
    };

    // Debounce duplicate events for same account within 60s in the queue
    const lastAlert = lastDisconnectAlertTime[safeId] || 0;
    if (now - lastAlert > 60000) {
        lastDisconnectAlertTime[safeId] = now;
        disconnectEvents.push(event);
        if (disconnectEvents.length > 50) {
            disconnectEvents.shift();
        }
        console.warn(`[${safeId}] 🚨 Recorded disconnect event for ${acc.label} (${phone || "no phone"}): ${reason} (code: ${statusCode})`);
    }

    return event;
}

// ---------- Client ID Sanitization & Aliasing ----------
function sanitizeClientId(rawId: any): string {
    const raw = String(rawId || clientId || "user").trim();
    if (raw === "1" || raw.toLowerCase() === "account1") return "user";
    if (raw === "2" || raw.toLowerCase() === "account2") return "account2";
    if (raw === "3" || raw.toLowerCase() === "account3") return "account3";
    if (raw === "4" || raw.toLowerCase() === "account4") return "account4";
    const clean = raw.replace(/[^a-zA-Z0-9_-]/g, "");
    return clean || "user";
}

// Format raw chat ID to valid WhatsApp JID
function formatJid(rawId: string): string {
    const clean = String(rawId || "").trim();
    if (clean.includes("@")) {
        return clean;
    }
    if (clean.length > 15) {
        return `${clean}@g.us`;
    }
    return `${clean}@s.whatsapp.net`;
}

// ---------- Dedicated Proxy Resolution per Account Slot ----------
function getAccountProxy(safeId: string): string | undefined {
    let slotIndex: number | undefined;
    if (safeId === "user" || safeId === "account1") slotIndex = 1;
    else if (safeId === "account2") slotIndex = 2;
    else if (safeId === "account3") slotIndex = 3;
    else if (safeId === "account4") slotIndex = 4;

    const upper = safeId.toUpperCase();
    if (slotIndex && process.env[`ACCOUNT_${slotIndex}_PROXY`]) {
        return process.env[`ACCOUNT_${slotIndex}_PROXY`];
    }
    if (process.env[`ACCOUNT_${upper}_PROXY`]) {
        return process.env[`ACCOUNT_${upper}_PROXY`];
    }
    if (process.env[`PROXY_${upper}`]) {
        return process.env[`PROXY_${upper}`];
    }

    // Check proxies.json configuration file if present
    const candidateFiles = [
        path.resolve("proxies.json"),
        path.resolve(".baileys_auth", "proxies.json"),
    ];
    for (const pFile of candidateFiles) {
        try {
            if (fs.existsSync(pFile)) {
                const map = JSON.parse(fs.readFileSync(pFile, "utf-8"));
                if (map && typeof map === "object") {
                    if (map[safeId]) return map[safeId];
                    if (slotIndex && map[`account${slotIndex}`]) return map[`account${slotIndex}`];
                    if (slotIndex && map[String(slotIndex)]) return map[String(slotIndex)];
                }
            }
        } catch (_) {}
    }

    return process.env.WHATSAPP_PROXY || process.env.GLOBAL_PROXY || undefined;
}

function maskProxyUrl(proxyUrl: string): string {
    try {
        const u = new URL(proxyUrl);
        return `${u.protocol}//${u.host}`;
    } catch (_) {
        return proxyUrl.replace(/:[^:@]+@/, ":***@");
    }
}

function createProxyAgent(proxyUrl: string): any {
    const clean = proxyUrl.trim();
    if (clean.startsWith("socks")) {
        return new SocksProxyAgent(clean);
    }
    return new HttpsProxyAgent(clean);
}

interface ProxyProbeResult {
    ok: boolean;
    maskedHost: string;
    protocol: string;
    exitIp?: string;
    country?: string;
    countryCode?: string;
    latencyMs?: number;
    error?: string;
}

async function probeProxy(proxyUrl: string): Promise<ProxyProbeResult> {
    const maskedHost = maskProxyUrl(proxyUrl);
    const protocol = proxyUrl.split(":")[0];
    const start = Date.now();
    try {
        const agent = createProxyAgent(proxyUrl);
        const res = await axios.get("http://ip-api.com/json?fields=status,country,countryCode,query", {
            httpAgent: agent,
            httpsAgent: agent,
            timeout: 5000,
        });
        const latencyMs = Date.now() - start;
        if (res.data && res.data.status === "success") {
            return {
                ok: true,
                maskedHost,
                protocol,
                exitIp: res.data.query,
                country: res.data.country,
                countryCode: res.data.countryCode,
                latencyMs,
            };
        }
        return {
            ok: true,
            maskedHost,
            protocol,
            exitIp: res.data?.query || "Active",
            country: res.data?.country || "Online",
            latencyMs,
        };
    } catch (err: any) {
        try {
            const agent = createProxyAgent(proxyUrl);
            const fallbackRes = await axios.get("https://api.ipify.org?format=json", {
                httpAgent: agent,
                httpsAgent: agent,
                timeout: 4000,
            });
            const latencyMs = Date.now() - start;
            return {
                ok: true,
                maskedHost,
                protocol,
                exitIp: fallbackRes.data?.ip,
                country: "Online",
                latencyMs,
            };
        } catch (fallbackErr: any) {
            return {
                ok: false,
                maskedHost,
                protocol,
                latencyMs: Date.now() - start,
                error: err.message || "Proxy connection failed",
            };
        }
    }
}

// ---------- Newsletter & MEX Support ----------
let executeWMexQueryFn: any = null;
try {
    const mex = require("@whiskeysockets/baileys/lib/Socket/mex.js");
    executeWMexQueryFn = mex.executeWMexQuery;
} catch (e) {
    console.warn("Notice: mex.executeWMexQuery loader:", e);
}

interface KnownNewsletter {
    id: string;
    name: string;
    subscribersCount?: number;
    updatedAt: number;
}

const KNOWN_NEWSLETTERS_FILE = path.resolve(".baileys_auth", "known_newsletters.json");
const knownNewslettersMap = new Map<string, KnownNewsletter>();

function loadKnownNewsletters() {
    try {
        if (fs.existsSync(KNOWN_NEWSLETTERS_FILE)) {
            const raw = fs.readFileSync(KNOWN_NEWSLETTERS_FILE, "utf-8");
            const arr = JSON.parse(raw);
            if (Array.isArray(arr)) {
                for (const item of arr) {
                    if (item && item.id) {
                        knownNewslettersMap.set(item.id, item);
                    }
                }
                console.log(`[Newsletters] Loaded ${knownNewslettersMap.size} known newsletters from disk.`);
            }
        }
    } catch (e) {
        console.warn("Error reading known_newsletters.json:", e);
    }
}

let saveNewslettersTimer: NodeJS.Timeout | null = null;

function saveKnownNewsletters() {
    if (saveNewslettersTimer) return;
    saveNewslettersTimer = setTimeout(async () => {
        saveNewslettersTimer = null;
        try {
            const dir = path.dirname(KNOWN_NEWSLETTERS_FILE);
            if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
            const list = Array.from(knownNewslettersMap.values());
            await fs.promises.writeFile(KNOWN_NEWSLETTERS_FILE, JSON.stringify(list, null, 2), "utf-8");
        } catch (e) {
            console.warn("Error writing known_newsletters.json:", e);
        }
    }, 4000);
}

function registerKnownNewsletter(id: string, name?: string, count?: number) {
    if (!id) return;
    let cleanId = String(id).trim();
    if (/^\d{15,}$/.test(cleanId)) {
        cleanId = `${cleanId}@newsletter`;
    }
    if (!cleanId.endsWith("@newsletter")) return;

    const existing = knownNewslettersMap.get(cleanId);
    const validName = (name && name.trim() && name.trim() !== cleanId) ? name.trim() : (existing?.name || cleanId);
    const validCount = typeof count === "number" && count > 0 ? count : existing?.subscribersCount;

    knownNewslettersMap.set(cleanId, {
        id: cleanId,
        name: validName,
        subscribersCount: validCount,
        updatedAt: Date.now(),
    });
    saveKnownNewsletters();
}

loadKnownNewsletters();

// Setup multer for media upload with sanitization and size limits
const storage = multer.diskStorage({
    destination: "uploads/",
    filename: (req, file, cb) => {
        const safeName = file.originalname.replace(/[^a-zA-Z0-9._-]/g, "_");
        cb(null, `${Date.now()}-${safeName}`);
    },
});
const upload = multer({
    storage,
    limits: { fileSize: 105 * 1024 * 1024 }, // 105 MB ceiling
});

// Periodic uploads folder cleanup
function cleanupUploadsFolder() {
    const uploadDir = path.resolve("uploads");
    if (!fs.existsSync(uploadDir)) return;
    const now = Date.now();
    const MAX_AGE = 10 * 60 * 1000; // 10 minutes
    try {
        const files = fs.readdirSync(uploadDir);
        for (const f of files) {
            const fp = path.join(uploadDir, f);
            try {
                const stat = fs.statSync(fp);
                if (now - stat.mtimeMs > MAX_AGE) {
                    fs.unlinkSync(fp);
                    console.log(`Cleaned up stale upload: ${f}`);
                }
            } catch (_) {}
        }
    } catch (err) {
        console.error("Error cleaning uploads folder:", err);
    }
}

// ---------- Centralized Session Lifecycle with Mutex ----------

function scheduleSessionRestart(safeId: string, reason: string) {
    const entry = sessions[safeId];
    if (!entry) return;

    if (entry.isRestarting) {
        console.warn(`[${safeId}] Restart already pending (${reason}). Skipping duplicate.`);
        return;
    }

    entry.isRestarting = true;
    if (entry.restartTimer) {
        clearTimeout(entry.restartTimer);
    }

    entry.restartTimer = setTimeout(async () => {
        try {
            console.log(`[${safeId}] Reconnecting session (reason: ${reason})...`);
            await createOrRestartSession(safeId);
        } catch (err) {
            console.error(`[${safeId}] Error during reconnect:`, err);
        } finally {
            setTimeout(() => {
                if (sessions[safeId]) {
                    sessions[safeId].isRestarting = false;
                }
            }, 5000);
        }
    }, 3000);
}

async function destroySession(safeId: string, removeAuthFiles = false) {
    const entry = sessions[safeId];
    if (entry) {
        if (entry.restartTimer) {
            clearTimeout(entry.restartTimer);
        }
        if (entry.sock) {
            try {
                entry.sock.end(undefined);
            } catch (_) {}
            entry.sock = undefined;
        }
        delete sessions[safeId];
    }

    if (removeAuthFiles) {
        const authPath = path.resolve(".baileys_auth", `session-${safeId}`);
        if (fs.existsSync(authPath)) {
            try {
                fs.rmSync(authPath, { recursive: true, force: true });
                console.log(`[${safeId}] Removed auth credentials directory.`);
            } catch (err) {
                console.error(`[${safeId}] Failed to remove auth directory:`, err);
            }
        }
    }
}

async function createOrRestartSession(rawId: string): Promise<SessionEntry> {
    const safeId = sanitizeClientId(rawId);
    let entry = sessions[safeId];

    if (!entry) {
        entry = {
            isReady: false,
            qrListeners: [],
            readyListeners: [],
            authFailureListeners: [],
            isRestarting: false,
            isListening: false,
        };
        sessions[safeId] = entry;
    }

    if (entry.sock) {
        try {
            entry.sock.end(undefined);
        } catch (_) {}
        entry.sock = undefined;
    }

    entry.isReady = false;
    const authPath = path.resolve(".baileys_auth", `session-${safeId}`);
    if (!fs.existsSync(authPath)) {
        fs.mkdirSync(authPath, { recursive: true });
    }

    const logger = pino({ level: process.env.LOG_LEVEL || "silent" });
    const { state, saveCreds } = await useMultiFileAuthState(authPath);

    // Fetch version if available or fallback
    let version: [number, number, number] | undefined;
    try {
        const versionInfo = await fetchLatestBaileysVersion();
        version = versionInfo.version;
    } catch (_) {
        // Fallback to default Baileys version
    }

    const socketFactory = (typeof makeWASocket === "function" ? makeWASocket : (makeWASocket as any).default) as typeof makeWASocket;

    // Dedicated Proxy Support per Account Slot
    let agent: any = undefined;
    const proxyUrl = getAccountProxy(safeId);
    if (proxyUrl) {
        try {
            agent = createProxyAgent(proxyUrl);
            entry.hasProxy = true;
            entry.proxyHost = maskProxyUrl(proxyUrl);
            entry.proxyStatus = "healthy";

            probeProxy(proxyUrl).then((probe) => {
                if (probe.ok) {
                    entry.proxyExitIp = probe.exitIp;
                    entry.proxyCountry = probe.country;
                    entry.proxyCountryCode = probe.countryCode;
                    entry.proxyLatencyMs = probe.latencyMs;
                    entry.proxyStatus = "healthy";
                    console.log(`[${safeId}] 🌐 Proxy active: ${entry.proxyHost} -> Exit IP: ${probe.exitIp} (${probe.country || "Online"} - ${probe.latencyMs}ms)`);
                } else {
                    entry.proxyStatus = "failed";
                    console.warn(`[${safeId}] ⚠️ Proxy probe failed for ${entry.proxyHost}: ${probe.error}`);
                }
            }).catch((err) => {
                console.warn(`[${safeId}] Proxy probe background error:`, err);
            });
        } catch (proxyErr) {
            console.error(`[${safeId}] ⚠️ Failed to initialize proxy agent for ${maskProxyUrl(proxyUrl)}:`, proxyErr);
            entry.hasProxy = false;
            entry.proxyHost = undefined;
            entry.proxyStatus = "failed";
        }
    } else {
        entry.hasProxy = false;
        entry.proxyHost = undefined;
        entry.proxyStatus = "direct";
    }

    const sock = socketFactory({
        version,
        auth: {
            creds: state.creds,
            keys: makeCacheableSignalKeyStore(state.keys, logger),
        },
        agent,
        fetchAgent: agent,
        logger,
        printQRInTerminal: false,
        browser: Browsers.ubuntu("Chrome"),
        generateHighQualityLinkPreview: false,
    });

    entry.sock = sock;

    sock.ev.on("creds.update", saveCreds);

    sock.ev.on("connection.update", async (update) => {
        const { connection, lastDisconnect, qr } = update;

        if (qr) {
            console.log(`[${safeId}] QR generated`);
            entry.currentQR = qr;
            entry.qrTimestamp = Date.now();
            const listeners = [...entry.qrListeners];
            entry.qrListeners = [];
            listeners.forEach((fn) => fn(qr));
        }

        if (connection === "open") {
            console.log(`[${safeId}] WhatsApp connection open and ready!`);
            entry.isReady = true;
            entry.currentQR = undefined;
            entry.isRestarting = false;
            entry.connectedAt = new Date().toISOString();
            try {
                if (sock.user) {
                    const rawJid = sock.user.id || "";
                    const num = rawJid.split(":")[0] || rawJid.split("@")[0] || "";
                    entry.phoneNumber = num ? `+${num}` : undefined;
                    if (entry.phoneNumber) {
                        lastKnownPhones[safeId] = entry.phoneNumber;
                    }
                    entry.pushName = sock.user.name || undefined;
                    console.log(`[${safeId}] Phone: ${entry.phoneNumber || "Unknown"} (${entry.pushName || "No Name"})`);
                }
            } catch (err) {
                console.warn(`[${safeId}] Error extracting user info:`, err);
            }
            const listeners = [...entry.readyListeners];
            entry.readyListeners = [];
            listeners.forEach((fn) => fn());
        }

        if (connection === "close") {
            entry.isReady = false;
            entry.currentQR = undefined;
            const statusCode = (lastDisconnect?.error as Boom)?.output?.statusCode;
            console.warn(`[${safeId}] Connection closed with status code: ${statusCode}`);

            if (statusCode === DisconnectReason.loggedOut || statusCode === 401) {
                console.warn(`[${safeId}] 🚨 Logged out (401 Unauthorized / DisconnectReason.loggedOut). Clearing credentials...`);
                recordDisconnectEvent(safeId, 401, "Session Logged Out or Banned");
                const failureListeners = [...entry.authFailureListeners];
                entry.authFailureListeners = [];
                failureListeners.forEach((fn) => fn(new Error("Logged out from WhatsApp")));
                await destroySession(safeId, true);
            } else {
                scheduleSessionRestart(safeId, `connection_close_${statusCode}`);
            }
        }
    });

    sock.ev.on("messages.upsert", (m) => {
        if (m.messages) {
            for (const msg of m.messages) {
                const jid = msg.key?.remoteJid;
                if (jid && jid.endsWith("@newsletter")) {
                    registerKnownNewsletter(jid);
                }
                if (entry.isListening && !msg.key.fromMe) {
                    const text = msg.message?.conversation || msg.message?.extendedTextMessage?.text;
                    if (text) {
                        console.log(`[${safeId}] Incoming message: ${text.slice(0, 100)}`);
                    }
                }
            }
        }
    });

    sock.ev.on("chats.upsert" as any, (chats: any[]) => {
        if (Array.isArray(chats)) {
            for (const c of chats) {
                if (c && c.id && c.id.endsWith("@newsletter")) {
                    registerKnownNewsletter(c.id, c.name || c.subject);
                }
            }
        }
    });

    sock.ev.on("messaging-history.set" as any, ({ chats }: any) => {
        if (Array.isArray(chats)) {
            for (const c of chats) {
                if (c && c.id && c.id.endsWith("@newsletter")) {
                    registerKnownNewsletter(c.id, c.name || c.subject);
                }
            }
        }
    });

    return entry;
}

// ---------- Express Routes ----------

app.get("/health", (req, res) => {
    const safeId = sanitizeClientId(req.query.clientId || clientId);
    const entry = sessions[safeId];
    const ready = Boolean(entry && entry.isReady && entry.sock);
    const isLiveProbe = req.query.live === "1" || req.query.probe === "liveness";
    const mem = process.memoryUsage();
    res.status((ready || isLiveProbe) ? 200 : 503).json({
        status: ready ? "ready" : "initializing",
        clientId: safeId,
        hasProxy: Boolean(entry?.hasProxy),
        proxyHost: entry?.proxyHost || null,
        proxyExitIp: entry?.proxyExitIp || null,
        proxyCountry: entry?.proxyCountry || null,
        proxyCountryCode: entry?.proxyCountryCode || null,
        proxyLatencyMs: entry?.proxyLatencyMs || null,
        proxyStatus: entry?.proxyStatus || (entry?.hasProxy ? "healthy" : "direct"),
        sessionsCount: Object.keys(sessions).length,
        timestamp: new Date().toISOString(),
        memory: {
            rssBytes: mem.rss,
            rssMb: Math.round(mem.rss / (1024 * 1024)),
            heapUsedBytes: mem.heapUsed,
            heapUsedMb: Math.round(mem.heapUsed / (1024 * 1024)),
        },
    });
});

app.get("/sessions", (req, res) => {
    const accountsInfo = CONFIGURED_ACCOUNTS.map((acc) => {
        const entry = sessions[acc.id];
        const isReady = Boolean(entry && entry.isReady && entry.sock);
        const hasAuthFolder = fs.existsSync(path.resolve(".baileys_auth", `session-${acc.id}`));

        let phone = entry?.phoneNumber;
        let name = entry?.pushName;
        if (!phone && entry?.sock?.user) {
            const rawJid = entry.sock.user.id || "";
            const num = rawJid.split(":")[0] || rawJid.split("@")[0] || "";
            if (num) phone = `+${num}`;
            if (entry.sock.user.name) name = entry.sock.user.name;
        }

        let status = "not_logged_in";
        if (isReady) {
            status = "ready";
        } else if (entry && entry.currentQR) {
            status = "waiting_qr_scan";
        } else if (entry && entry.isRestarting) {
            status = "reconnecting";
        } else if (hasAuthFolder) {
            status = "disconnected";
        }

        return {
            id: acc.id,
            index: acc.index,
            label: acc.label,
            isReady,
            status,
            phone: phone || lastKnownPhones[acc.id] || null,
            name: name || null,
            connectedAt: entry?.connectedAt || null,
            hasAuthFolder,
            hasPendingQR: Boolean(entry?.currentQR),
            hasProxy: Boolean(entry?.hasProxy),
            proxyHost: entry?.proxyHost || null,
            proxyExitIp: entry?.proxyExitIp || null,
            proxyCountry: entry?.proxyCountry || null,
            proxyCountryCode: entry?.proxyCountryCode || null,
            proxyLatencyMs: entry?.proxyLatencyMs || null,
            proxyStatus: entry?.proxyStatus || (entry?.hasProxy ? "healthy" : "direct"),
        };
    });

    const readyCount = accountsInfo.filter((a) => a.isReady).length;

    res.json({
        accounts: accountsInfo,
        readyCount,
        totalConfigured: CONFIGURED_ACCOUNTS.length,
        timestamp: new Date().toISOString(),
    });
});

app.get("/proxy-test", async (req, res) => {
    const safeId = sanitizeClientId(req.query.clientId || clientId);
    const proxyUrl = getAccountProxy(safeId);
    if (!proxyUrl) {
        return res.json({
            account: safeId,
            proxyConfigured: false,
            message: `No proxy configured for account slot '${safeId}'. Operating directly via host IP.`,
            status: "direct",
            timestamp: new Date().toISOString(),
        });
    }

    const probe = await probeProxy(proxyUrl);
    res.json({
        account: safeId,
        proxyConfigured: true,
        maskedHost: probe.maskedHost,
        protocol: probe.protocol,
        status: probe.ok ? "healthy" : "failed",
        exitIp: probe.exitIp || null,
        country: probe.country || null,
        countryCode: probe.countryCode || null,
        latencyMs: probe.latencyMs || null,
        error: probe.error || null,
        timestamp: new Date().toISOString(),
    });
});

// ---------- Real-Time Disconnect Events API for Push Alerts ----------
app.get("/disconnect-events", (req, res) => {
    const since = req.query.since ? String(req.query.since) : undefined;
    const ack = req.query.ack === "true" || req.query.ack === "1";

    let events = [...disconnectEvents];
    if (since) {
        events = events.filter((e) => e.timestamp > since);
    }

    if (ack) {
        disconnectEvents.length = 0;
    }

    res.json({
        events,
        unhandledCount: disconnectEvents.length,
        timestamp: new Date().toISOString(),
    });
});

app.post("/disconnect-events/ack", (req, res) => {
    const eventIds: string[] = Array.isArray(req.body.eventIds) ? req.body.eventIds : [];
    if (eventIds.length > 0) {
        const idSet = new Set(eventIds);
        for (let i = disconnectEvents.length - 1; i >= 0; i--) {
            if (idSet.has(disconnectEvents[i].eventId)) {
                disconnectEvents.splice(i, 1);
            }
        }
    } else {
        disconnectEvents.length = 0;
    }
    res.json({
        success: true,
        remainingCount: disconnectEvents.length,
    });
});

app.post("/cleanup", async (req, res) => {
    const uploadDir = path.resolve("uploads");
    let filesPurged = 0;
    let bytesReclaimed = 0;
    if (fs.existsSync(uploadDir)) {
        try {
            const files = fs.readdirSync(uploadDir);
            for (const f of files) {
                const fp = path.join(uploadDir, f);
                try {
                    const stat = fs.statSync(fp);
                    if (stat.isFile()) {
                        bytesReclaimed += stat.size;
                        fs.unlinkSync(fp);
                        filesPurged++;
                    }
                } catch (_) {}
            }
        } catch (err) {
            console.error("[AutoHealer] WhatsApp /cleanup error:", err);
        }
    }
    if ((global as any).gc) {
        try {
            (global as any).gc();
        } catch (_) {}
    }
    const mem = process.memoryUsage();
    return res.json({
        success: true,
        filesPurged,
        bytesReclaimed,
        memory: {
            rssMb: Math.round(mem.rss / (1024 * 1024)),
            heapUsedMb: Math.round(mem.heapUsed / (1024 * 1024)),
        },
    });
});

app.post("/createsession", async (req, res) => {
    const targetId = sanitizeClientId(req.body.clientId || clientId);
    console.log("Creating session for clientId:", targetId);

    const existing = sessions[targetId];
    if (existing && existing.isReady) {
        return res.json({ message: "Session already exists and is ready", ready: true });
    }

    let entry = existing;
    const isQrFresh = Boolean(entry?.currentQR && entry?.qrTimestamp && (Date.now() - entry.qrTimestamp < 20000));

    if (!entry || !entry.sock || (!entry.isReady && !isQrFresh)) {
        console.log(`[${targetId}] Starting fresh socket to generate login QR code...`);
        entry = await createOrRestartSession(targetId);
    } else if (isQrFresh && entry.currentQR) {
        console.log(`[${targetId}] Returning active fresh QR code`);
        return res.json({ qrcode: entry.currentQR });
    }

    let responded = false;
    let timeoutId: NodeJS.Timeout;

    const cleanup = () => {
        if (!entry) return;
        entry.qrListeners = entry.qrListeners.filter((fn) => fn !== onQr);
        entry.readyListeners = entry.readyListeners.filter((fn) => fn !== onReady);
        entry.authFailureListeners = entry.authFailureListeners.filter((fn) => fn !== onAuthFail);
    };

    const onQr = (qr: string) => {
        if (responded) return;
        responded = true;
        cleanup();
        clearTimeout(timeoutId);
        res.json({ qrcode: qr });
    };

    const onReady = () => {
        if (responded) return;
        responded = true;
        cleanup();
        clearTimeout(timeoutId);
        res.json({ message: "Session is ready", ready: true });
    };

    const onAuthFail = (err: any) => {
        if (responded) return;
        responded = true;
        cleanup();
        clearTimeout(timeoutId);
        res.status(401).json({ message: "Authentication failure", error: String(err) });
    };

    timeoutId = setTimeout(() => {
        if (responded) return;
        responded = true;
        cleanup();
        res.status(202).json({
            message: "Session initialization started in background. Check /health for status.",
            status: "initializing",
        });
    }, 25000);

    entry.qrListeners.push(onQr);
    entry.readyListeners.push(onReady);
    entry.authFailureListeners.push(onAuthFail);
});

function checkSessionReadiness(targetId: string, res: express.Response): SessionEntry | null {
    const entry = sessions[targetId];
    if (entry && entry.isReady && entry.sock) {
        return entry;
    }
    const hasAuthFolder = fs.existsSync(path.resolve(".baileys_auth", `session-${targetId}`));
    if (entry?.isRestarting || (entry && !entry.isReady && entry.sock) || hasAuthFolder) {
        res.status(503).json({
            message: "Session is not ready or reconnecting",
            status: "reconnecting",
            clientId: targetId,
        });
        return null;
    }
    res.status(401).json({
        message: "Session is not authorized",
        status: "not_logged_in",
        clientId: targetId,
    });
    return null;
}

app.post("/startlistening", async (req, res) => {
    const targetId = sanitizeClientId(req.body.clientId || clientId);
    const entry = checkSessionReadiness(targetId, res);
    if (!entry) return;

    if (entry.isListening) {
        return res.json({ message: "Already listening to messages." });
    }

    entry.isListening = true;
    res.json({ message: "Listening to messages" });
});

interface ChatItem {
    id: string;
    name: string;
    type: "group" | "newsletter";
    participantsCount?: number;
}

interface ChatCacheEntry {
    timestamp: number;
    chats: ChatItem[];
}

const chatDiscoveryCache: Record<string, ChatCacheEntry> = {};

async function fetchSubscribedNewsletters(sock: any, targetId: string): Promise<ChatItem[]> {
    const list: ChatItem[] = [];
    const seenIds = new Set<string>();

    // 1. Try MEX xwa2_newsletter_subscribed via Baileys executeWMexQuery
    if (executeWMexQueryFn && typeof sock.query === "function" && typeof sock.generateMessageTag === "function") {
        try {
            const res = await executeWMexQueryFn(
                {},
                "6388546374527196",
                "xwa2_newsletter_subscribed",
                sock.query,
                sock.generateMessageTag
            );
            if (Array.isArray(res)) {
                for (const nl of res) {
                    const id = nl?.id || nl?.jid;
                    if (id && !seenIds.has(id)) {
                        seenIds.add(id);
                        const name = nl?.thread_metadata?.name?.text || nl?.name || nl?.thread_metadata?.name || id;
                        const count = parseInt(nl?.thread_metadata?.subscribers_count || nl?.subscribers || "0", 10);
                        const item: ChatItem = {
                            id,
                            name: String(name || id),
                            type: "newsletter",
                            participantsCount: count || undefined,
                        };
                        list.push(item);
                        registerKnownNewsletter(id, item.name, item.participantsCount);
                    }
                }
            }
        } catch (mexErr: any) {
            console.warn(`[${targetId}] MEX xwa2_newsletter_subscribed notice:`, mexErr?.message || mexErr);
        }
    }

    // 2. Also merge all known/persisted newsletters from disk
    for (const [id, item] of knownNewslettersMap.entries()) {
        if (!seenIds.has(id)) {
            seenIds.add(id);
            list.push({
                id,
                name: item.name || id,
                type: "newsletter",
                participantsCount: item.subscribersCount,
            });
        }
    }

    // 3. For any newsletter whose name is unknown or matches JID, resolve via sock.newsletterMetadata
    for (const item of list) {
        if ((!item.name || item.name === item.id) && typeof (sock as any).newsletterMetadata === "function") {
            try {
                const meta = await (sock as any).newsletterMetadata("jid", item.id);
                if (meta) {
                    const resolvedName = meta.name || (meta as any).thread_metadata?.name?.text;
                    if (resolvedName) {
                        item.name = resolvedName;
                        registerKnownNewsletter(item.id, resolvedName, meta.subscribers);
                    }
                }
            } catch (_) {}
        }
    }

    return list;
}

async function discoverChats(entry: SessionEntry, targetId: string): Promise<ChatItem[]> {
    const now = Date.now();
    const cached = chatDiscoveryCache[targetId];
    if (cached && now - cached.timestamp < 30000) {
        return cached.chats;
    }

    const sock = entry.sock!;
    const chats: ChatItem[] = [];

    // 1. Fetch participating groups
    try {
        const groups = await sock.groupFetchAllParticipating();
        for (const [id, meta] of Object.entries(groups)) {
            chats.push({
                id,
                name: meta.subject || id,
                type: "group",
                participantsCount: meta.participants?.length || meta.size,
            });
        }
    } catch (grpErr) {
        console.warn(`[${targetId}] Error fetching groups:`, grpErr);
    }

    // 2. Fetch subscribed and known newsletters / channels
    try {
        const newsletters = await fetchSubscribedNewsletters(sock, targetId);
        for (const nl of newsletters) {
            if (!chats.some((c) => c.id === nl.id)) {
                chats.push(nl);
            }
        }
    } catch (nlErr) {
        console.warn(`[${targetId}] Error fetching newsletters:`, nlErr);
    }

    chats.sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" }));

    chatDiscoveryCache[targetId] = {
        timestamp: now,
        chats,
    };

    return chats;
}

app.post("/registerNewsletters", (req, res) => {
    const { newsletters } = req.body;
    if (Array.isArray(newsletters)) {
        for (const item of newsletters) {
            const id = typeof item === "string" ? item : item?.id;
            const name = typeof item === "object" ? item?.name : undefined;
            if (id) {
                registerKnownNewsletter(id, name);
            }
        }
    }
    res.json({ message: "Registered", count: knownNewslettersMap.size });
});

app.post("/getChatId", async (req, res) => {
    const targetId = sanitizeClientId(req.body.clientId || clientId);
    const { chatName } = req.body;

    if (!chatName) {
        return res.status(400).json({ message: "chatName is required" });
    }

    const entry = checkSessionReadiness(targetId, res);
    if (!entry) return;

    try {
        const sock = entry.sock;
        const rawInput = String(chatName).trim();
        // Clean wrapping brackets e.g. <FOREX>, quotes "FOREX", backticks `FOREX`
        const cleanQuery = rawInput.replace(/^[<"'`\s]+|[>"'`\s]+$/g, "").trim();
        const searchLower = cleanQuery.toLowerCase();

        // 1. Direct JID check (e.g. 120363...@newsletter, 120363...@g.us)
        if (cleanQuery.endsWith("@g.us") || cleanQuery.endsWith("@newsletter") || cleanQuery.endsWith("@s.whatsapp.net")) {
            let name = cleanQuery;
            if (cleanQuery.endsWith("@newsletter") && typeof (sock as any).newsletterMetadata === "function") {
                try {
                    const meta = await (sock as any).newsletterMetadata("jid", cleanQuery);
                    if (meta) {
                        name = meta.name || (meta as any).thread_metadata?.name?.text || name;
                        registerKnownNewsletter(cleanQuery, name, meta.subscribers);
                    }
                } catch (_) {}
            }
            return res.json({
                groupId: cleanQuery,
                name,
                isGroup: cleanQuery.endsWith("@g.us"),
                isChannel: cleanQuery.endsWith("@newsletter"),
            });
        }

        // 2. Direct pure digit JID check (e.g. 120363420111598085)
        if (/^\d{15,}$/.test(cleanQuery)) {
            const asNl = `${cleanQuery}@newsletter`;
            const asGrp = `${cleanQuery}@g.us`;
            if (knownNewslettersMap.has(asNl)) {
                const kn = knownNewslettersMap.get(asNl)!;
                return res.json({
                    groupId: asNl,
                    name: kn.name || asNl,
                    isChannel: true,
                    isGroup: false,
                });
            }
            if (typeof (sock as any).newsletterMetadata === "function") {
                try {
                    const meta = await (sock as any).newsletterMetadata("jid", asNl);
                    if (meta && meta.id) {
                        const name = meta.name || (meta as any).thread_metadata?.name?.text || asNl;
                        registerKnownNewsletter(asNl, name, meta.subscribers);
                        return res.json({
                            groupId: asNl,
                            name,
                            isChannel: true,
                            isGroup: false,
                        });
                    }
                } catch (_) {}
            }
        }

        // 3. WhatsApp Channel Invite Link check (e.g. https://whatsapp.com/channel/0029VaXXXXX or whatsapp.com/channel/CODE)
        const channelLinkMatch = cleanQuery.match(/(?:whatsapp\.com\/channel\/|^)([a-zA-Z0-9_-]{15,30})$/i);
        if (channelLinkMatch && typeof (sock as any).newsletterMetadata === "function") {
            const inviteCode = channelLinkMatch[1];
            try {
                const meta = await (sock as any).newsletterMetadata("invite", inviteCode);
                if (meta && meta.id) {
                    const name = meta.name || (meta as any).thread_metadata?.name?.text || meta.id;
                    registerKnownNewsletter(meta.id, name, meta.subscribers);
                    return res.json({
                        groupId: meta.id,
                        name,
                        isChannel: true,
                        isGroup: false,
                        subscribers: meta.subscribers,
                    });
                }
            } catch (invErr: any) {
                console.warn(`[${targetId}] Newsletter invite resolution notice:`, invErr?.message || invErr);
            }
        }

        // 4. Fetch all participating groups and newsletters
        const allChats = await discoverChats(entry, targetId);

        // 5. Intelligent Ranking / Fuzzy Matching:
        const queryWords = searchLower.split(/\s+/).filter(Boolean);

        interface ScoredMatch {
            chat: ChatItem;
            score: number;
        }

        const scored: ScoredMatch[] = [];

        for (const chat of allChats) {
            const nameLower = (chat.name || "").toLowerCase().trim();
            const idLower = (chat.id || "").toLowerCase().trim();

            let score = 0;
            if (nameLower === searchLower || idLower === searchLower) {
                score = 100; // Exact match
            } else if (nameLower.startsWith(searchLower)) {
                score = 80;  // Prefix match
            } else if (nameLower.includes(searchLower)) {
                score = 60;  // Substring match
            } else if (queryWords.length > 1 && queryWords.every((w) => nameLower.includes(w))) {
                score = 50;  // All words present (e.g. "bitcoin forex" in "bitcoin crypto forex gold")
            } else if (queryWords.some((w) => nameLower.includes(w) && w.length >= 3)) {
                const matchingWords = queryWords.filter((w) => nameLower.includes(w) && w.length >= 3);
                score = 20 + (matchingWords.length / queryWords.length) * 20;
            }

            if (score > 0) {
                scored.push({ chat, score });
            }
        }

        scored.sort((a, b) => b.score - a.score);

        if (scored.length > 0) {
            const best = scored[0].chat;
            const matches = scored.slice(0, 5).map((s) => ({
                groupId: s.chat.id,
                name: s.chat.name,
                isChannel: s.chat.type === "newsletter",
                isGroup: s.chat.type === "group",
            }));

            return res.json({
                groupId: best.id,
                name: best.name,
                isGroup: best.type === "group",
                isChannel: best.type === "newsletter",
                matches: matches.length > 1 ? matches : undefined,
            });
        }

        // 6. Last resort: Try as an invite code if alphanumeric and reasonable length
        if (/^[a-zA-Z0-9_-]{15,30}$/.test(cleanQuery) && typeof (sock as any).newsletterMetadata === "function") {
            try {
                const meta = await (sock as any).newsletterMetadata("invite", cleanQuery);
                if (meta && meta.id) {
                    const name = meta.name || (meta as any).thread_metadata?.name?.text || meta.id;
                    registerKnownNewsletter(meta.id, name, meta.subscribers);
                    return res.json({
                        groupId: meta.id,
                        name,
                        isChannel: true,
                        isGroup: false,
                        subscribers: meta.subscribers,
                    });
                }
            } catch (_) {}
        }

        return res.status(404).json({ message: `Group or channel not found for "${cleanQuery}"` });
    } catch (error: any) {
        console.error(`[${targetId}] Error in getChatId:`, error);
        res.status(500).json({
            message: "Internal server error",
            error: error?.message || String(error),
        });
    }
});

app.get("/groups", async (req, res) => {
    const targetId = sanitizeClientId((req.query.clientId as string) || clientId);
    const entry = checkSessionReadiness(targetId, res);
    if (!entry) return;

    try {
        const chats = await discoverChats(entry, targetId);
        const search = req.query.search ? String(req.query.search).toLowerCase().trim() : "";
        const filtered = search
            ? chats.filter((c) => c.name.toLowerCase().includes(search) || c.id.toLowerCase().includes(search))
            : chats;

        res.json({
            total: filtered.length,
            chats: filtered,
        });
    } catch (error: any) {
        console.error(`[${targetId}] Error discovering chats:`, error);
        res.status(500).json({
            message: "Failed to discover chats",
            error: error?.message || String(error),
            chats: [],
            total: 0,
        });
    }
});

app.get("/audience-stats", async (req, res) => {
    const targetId = sanitizeClientId((req.query.clientId as string) || clientId);
    const entry = checkSessionReadiness(targetId, res);
    if (!entry) return;

    try {
        const chats = await discoverChats(entry, targetId);
        let groupsCount = 0;
        let groupMembers = 0;
        let newslettersCount = 0;
        let newsletterSubscribers = 0;

        const destinations = chats.map((c) => {
            const count = c.participantsCount || 0;
            if (c.type === "group") {
                groupsCount++;
                groupMembers += count;
            } else if (c.type === "newsletter") {
                newslettersCount++;
                newsletterSubscribers += count;
            }
            return {
                id: c.id,
                name: c.name,
                type: c.type,
                count,
            };
        });

        destinations.sort((a, b) => b.count - a.count);

        res.json({
            totalAudience: groupMembers + newsletterSubscribers,
            groupsCount,
            groupMembers,
            newslettersCount,
            newsletterSubscribers,
            destinations,
        });
    } catch (error: any) {
        console.error(`[${targetId}] Error computing audience stats:`, error);
        res.status(500).json({
            message: "Failed to compute audience stats",
            error: error?.message || String(error),
            totalAudience: 0,
            groupsCount: 0,
            groupMembers: 0,
            newslettersCount: 0,
            newsletterSubscribers: 0,
            destinations: [],
        });
    }
});

app.post("/getGroups", async (req, res) => {
    const targetId = sanitizeClientId(req.body.clientId || (req.query.clientId as string) || clientId);
    const entry = checkSessionReadiness(targetId, res);
    if (!entry) return;

    try {
        const chats = await discoverChats(entry, targetId);
        const search = req.body.search ? String(req.body.search).toLowerCase().trim() : "";
        const filtered = search
            ? chats.filter((c) => c.name.toLowerCase().includes(search) || c.id.toLowerCase().includes(search))
            : chats;

        res.json({
            total: filtered.length,
            chats: filtered,
        });
    } catch (error: any) {
        console.error(`[${targetId}] Error discovering chats:`, error);
        res.status(500).json({
            message: "Failed to discover chats",
            error: error?.message || String(error),
            chats: [],
            total: 0,
        });
    }
});

app.post(["/audit-admins", "/check-permissions"], async (req, res) => {
    try {
        let requestedDestinations: string[] = Array.isArray(req.body.destinations)
            ? req.body.destinations.map((d: any) => String(d).trim()).filter(Boolean)
            : [];

        // If no destinations passed, collect from known newsletters and participating groups across ready accounts
        if (requestedDestinations.length === 0) {
            const destSet = new Set<string>();
            for (const k of knownNewslettersMap.keys()) {
                destSet.add(k);
            }
            for (const entry of Object.values(sessions)) {
                if (entry.isReady && entry.sock) {
                    try {
                        const grps = await entry.sock.groupFetchAllParticipating();
                        for (const gid of Object.keys(grps)) {
                            destSet.add(gid);
                        }
                    } catch (_) {}
                }
            }
            requestedDestinations = Array.from(destSet);
        }

        const uniqueDestinations = Array.from(new Set(requestedDestinations));

        const requestedAccounts = Array.isArray(req.body.accountIds) && req.body.accountIds.length > 0
            ? req.body.accountIds.map((a: any) => sanitizeClientId(String(a)))
            : CONFIGURED_ACCOUNTS.map((a) => a.id);

        const activeAccounts = CONFIGURED_ACCOUNTS.filter((acc) => requestedAccounts.includes(acc.id));
        const readyAccounts = activeAccounts.filter((acc) => sessions[acc.id]?.isReady && sessions[acc.id]?.sock);

        const destinationResults: Array<{
            id: string;
            name: string;
            type: "newsletter" | "group" | "unknown";
            allAdmins: boolean;
            accounts: Array<{
                clientId: string;
                index: number;
                label: string;
                phone: string | null;
                pushName?: string;
                isReady: boolean;
                isAdmin: boolean;
                role: string;
                error?: string;
            }>;
            missingAdmins: Array<{
                clientId: string;
                index: number;
                label: string;
                phone: string | null;
                role: string;
                error?: string;
            }>;
        }> = [];

        const allIssues: Array<{
            destinationId: string;
            destinationName: string;
            destinationType: string;
            account: {
                clientId: string;
                index: number;
                label: string;
                phone: string | null;
                role: string;
                error?: string;
            };
        }> = [];

        for (const jid of uniqueDestinations) {
            const isNewsletter = jid.endsWith("@newsletter");
            const isGroup = jid.endsWith("@g.us");
            let destName = knownNewslettersMap.get(jid)?.name || jid;

            const accountChecks: Array<{
                clientId: string;
                index: number;
                label: string;
                phone: string | null;
                pushName?: string;
                isReady: boolean;
                isAdmin: boolean;
                role: string;
                error?: string;
            }> = [];

            for (const acc of activeAccounts) {
                const entry = sessions[acc.id];
                const phone = entry?.phoneNumber || lastKnownPhones[acc.id] || null;
                const pushName = entry?.pushName;

                if (!entry || !entry.isReady || !entry.sock) {
                    accountChecks.push({
                        clientId: acc.id,
                        index: acc.index,
                        label: acc.label,
                        phone,
                        pushName,
                        isReady: false,
                        isAdmin: false,
                        role: "NOT_CONNECTED",
                        error: "Account session is not logged in or reconnecting",
                    });
                    continue;
                }

                const sock = entry.sock;

                if (isNewsletter) {
                    try {
                        let role = "NOT_IN_CHANNEL";
                        let isAdmin = false;

                        if (typeof (sock as any).newsletterMetadata === "function") {
                            const meta = await (sock as any).newsletterMetadata("jid", jid);
                            if (meta) {
                                const resolvedName = meta.name || (meta as any).thread_metadata?.name?.text;
                                if (resolvedName) {
                                    destName = resolvedName;
                                    registerKnownNewsletter(jid, resolvedName, meta.subscribers);
                                }

                                const viewerRole =
                                    meta.viewer_metadata?.role ||
                                    (meta as any).role ||
                                    (meta as any).viewerRole ||
                                    (meta as any).thread_metadata?.viewer_metadata?.role;

                                if (viewerRole) {
                                    const rUpper = String(viewerRole).toUpperCase();
                                    role = rUpper;
                                    isAdmin = rUpper === "ADMIN" || rUpper === "OWNER";
                                } else if (meta.owner) {
                                    const cleanPhone = phone?.replace(/\D/g, "") || "";
                                    const myJid = sock.user?.id?.split(/[:@]/)[0] || "";
                                    if ((cleanPhone && meta.owner.includes(cleanPhone)) || (myJid && meta.owner.includes(myJid))) {
                                        role = "OWNER";
                                        isAdmin = true;
                                    }
                                }
                            }
                        }

                        accountChecks.push({
                            clientId: acc.id,
                            index: acc.index,
                            label: acc.label,
                            phone,
                            pushName,
                            isReady: true,
                            isAdmin,
                            role,
                        });
                    } catch (nlErr: any) {
                        accountChecks.push({
                            clientId: acc.id,
                            index: acc.index,
                            label: acc.label,
                            phone,
                            pushName,
                            isReady: true,
                            isAdmin: false,
                            role: "NOT_IN_CHANNEL",
                            error: nlErr?.message || String(nlErr),
                        });
                    }
                } else if (isGroup) {
                    try {
                        const meta = await sock.groupMetadata(jid);
                        if (meta && meta.subject) {
                            destName = meta.subject;
                        }

                        let role = "NOT_IN_GROUP";
                        let isAdmin = false;

                        const cleanPhone = phone?.replace(/\D/g, "") || "";
                        const myJidNum = sock.user?.id?.split(/[:@]/)[0] || "";

                        const participant = meta?.participants?.find((p: any) => {
                            const pNum = p.id.split(/[:@]/)[0];
                            return (cleanPhone && pNum === cleanPhone) || (myJidNum && pNum === myJidNum);
                        });

                        if (participant) {
                            if (participant.admin === "admin" || participant.admin === "superadmin") {
                                isAdmin = true;
                                role = participant.admin === "superadmin" ? "OWNER" : "ADMIN";
                            } else {
                                isAdmin = false;
                                role = "MEMBER";
                            }
                        }

                        accountChecks.push({
                            clientId: acc.id,
                            index: acc.index,
                            label: acc.label,
                            phone,
                            pushName,
                            isReady: true,
                            isAdmin,
                            role,
                        });
                    } catch (grpErr: any) {
                        accountChecks.push({
                            clientId: acc.id,
                            index: acc.index,
                            label: acc.label,
                            phone,
                            pushName,
                            isReady: true,
                            isAdmin: false,
                            role: "NOT_IN_GROUP",
                            error: grpErr?.message || String(grpErr),
                        });
                    }
                } else {
                    accountChecks.push({
                        clientId: acc.id,
                        index: acc.index,
                        label: acc.label,
                        phone,
                        pushName,
                        isReady: true,
                        isAdmin: false,
                        role: "UNKNOWN_TYPE",
                        error: "Unknown destination format",
                    });
                }
            }

            const missingAdmins = accountChecks
                .filter((ac) => !ac.isAdmin)
                .map((ac) => ({
                    clientId: ac.clientId,
                    index: ac.index,
                    label: ac.label,
                    phone: ac.phone,
                    role: ac.role,
                    error: ac.error,
                }));

            const allAdmins = missingAdmins.length === 0;

            for (const ma of missingAdmins) {
                allIssues.push({
                    destinationId: jid,
                    destinationName: destName,
                    destinationType: isNewsletter ? "newsletter" : isGroup ? "group" : "unknown",
                    account: ma,
                });
            }

            destinationResults.push({
                id: jid,
                name: destName,
                type: isNewsletter ? "newsletter" : isGroup ? "group" : "unknown",
                allAdmins,
                accounts: accountChecks,
                missingAdmins,
            });
        }

        const allCompliant = allIssues.length === 0;

        res.json({
            allCompliant,
            totalDestinations: uniqueDestinations.length,
            totalConfiguredAccounts: activeAccounts.length,
            totalReadyAccounts: readyAccounts.length,
            issuesCount: allIssues.length,
            issues: allIssues,
            destinations: destinationResults,
            timestamp: new Date().toISOString(),
        });
    } catch (error: any) {
        console.error("Error auditing channel admins:", error);
        res.status(500).json({
            message: "Failed to audit channel admins",
            error: error?.message || String(error),
            allCompliant: false,
            issues: [],
            destinations: [],
        });
    }
});


// Helper function to build Baileys media payload with width/height/thumbnail
async function buildMediaPayload(filePath: string, originalName: string | undefined, caption?: string): Promise<AnyMessageContent> {
    const buffer = await fs.promises.readFile(filePath);
    const mimeType = (mime.lookup(originalName || filePath) || "application/octet-stream") as string;

    if (mimeType.startsWith("image/")) {
        let width: number | undefined;
        let height: number | undefined;
        let jpegThumbnail: Buffer | undefined;

        try {
            const meta = await sharp(buffer).metadata();
            width = meta.width;
            height = meta.height;
            // Generate low-res thumbnail matching WhatsApp standard (32-64px thumbnail)
            jpegThumbnail = await sharp(buffer)
                .resize(64, 64, { fit: "inside" })
                .jpeg({ quality: 50 })
                .toBuffer();
        } catch (err) {
            console.warn("Notice: could not extract image dimensions/thumbnail via sharp:", err);
        }

        return {
            image: buffer,
            caption: caption || undefined,
            mimetype: mimeType,
            width,
            height,
            jpegThumbnail,
        } as any;
    } else if (mimeType.startsWith("video/")) {
        throw new Error("Video forwarding is banned");
    } else if (mimeType.startsWith("audio/")) {

        const isVoice = mimeType.includes("ogg") || mimeType.includes("opus") || (originalName && originalName.endsWith(".ogg"));
        return {
            audio: buffer,
            mimetype: mimeType,
            ptt: Boolean(isVoice),
        };
    } else {
        return {
            document: buffer,
            mimetype: mimeType,
            fileName: originalName || path.basename(filePath),
            caption: caption || undefined,
        };
    }
}

// Helper function to simulate organic human typing/recording presence
async function simulateTypingPresence(
    targetId: string,
    sock: WASocket,
    jid: string,
    options?: { isMedia?: boolean; textForDelay?: string }
): Promise<void> {
    // 1. Newsletters do not support presence updates
    if (jid.endsWith("@newsletter")) return;
    // 2. Can be explicitly disabled via env var if high speed needed
    if (process.env.SIMULATE_TYPING === "false") return;

    try {
        const presence = options?.isMedia ? "recording" : "composing";
        await sock.sendPresenceUpdate(presence, jid);

        const textLen = (options?.textForDelay || "").length;
        // Dynamic human delay:
        // - Media: 1200ms - 2400ms
        // - Text: scaled with character length (1000ms floor, ~14ms/char up to 3800ms ceiling) + organic jitter
        const delayMs = options?.isMedia
            ? 1200 + Math.floor(Math.random() * 1200)
            : Math.min(Math.max(1000, textLen * 14), 3800) + Math.floor(Math.random() * 500);

        await new Promise((resolve) => setTimeout(resolve, delayMs));
        await sock.sendPresenceUpdate("paused", jid);
    } catch (presenceErr) {
        // Presence updates may fail if group permissions restrict it or during momentary reconnection.
        // Never let presence update errors block message dispatch!
        console.warn(`[${targetId}] ⚠️ Presence simulation notice for ${jid}:`, presenceErr);
    }
}

// Socket Transmission Queue with organic inter-frame jitter (300ms - 750ms)
const sessionQueues: Record<string, Promise<any>> = {};

function enqueueSocketSend<T>(sessionId: string, fn: () => Promise<T>): Promise<T> {
    const prev = sessionQueues[sessionId] || Promise.resolve();
    const next = prev.then(async () => {
        try {
            return await fn();
        } finally {
            const jitterDelay = 300 + Math.floor(Math.random() * 450);
            await new Promise((resolve) => setTimeout(resolve, jitterDelay));
        }
    });
    sessionQueues[sessionId] = next.catch(() => {});
    return next;
}

app.post("/sendToGroup", upload.single("media"), async (req, res) => {
    const targetId = sanitizeClientId(req.body.clientId || clientId);
    const { groupId, caption } = req.body;
    const file = req.file;

    try {
        const entry = checkSessionReadiness(targetId, res);
        if (!entry) return;

        const jid = formatJid(groupId);
        let sendResult: any = undefined;
        if (file) {
            const mimeType = (mime.lookup(file.originalname || file.path) || file.mimetype || "") as string;
            const isVideoExt = /\.(mp4|mov|avi|mkv|webm|flv|wmv|m4v|3gp|ts|m4p|mpg|mpeg)$/i.test(file.originalname || file.path);
            if (mimeType.startsWith("video/") || isVideoExt) {
                console.warn(`[${targetId}] 🚫 Video forwarding is banned. Rejected: ${file.originalname}`);
                return res.status(403).json({ message: "Video forwarding is banned" });
            }
            const payload = await buildMediaPayload(file.path, file.originalname, caption);
            sendResult = await enqueueSocketSend(targetId, async () => {
                await simulateTypingPresence(targetId, entry.sock!, jid, { isMedia: true, textForDelay: caption });
                return await entry.sock!.sendMessage(jid, payload);
            });
        } else if (caption) {
            sendResult = await enqueueSocketSend(targetId, async () => {
                await simulateTypingPresence(targetId, entry.sock!, jid, { textForDelay: String(caption) });
                return await entry.sock!.sendMessage(jid, { text: String(caption) });
            });
        } else {
            return res.status(400).json({ message: "No media or caption provided" });
        }

        if (jid.endsWith("@newsletter")) {
            registerKnownNewsletter(jid);
        }

        res.json({
            message: "Message sent successfully",
            messageId: sendResult?.key?.id,
            key: sendResult?.key,
        });
    } catch (error: any) {
        console.error(`[${targetId}] Error sending message:`, error);
        res.status(500).json({
            message: "Failed to send message",
            error: error?.message || String(error),
        });
    } finally {
        if (file && fs.existsSync(file.path)) {
            try {
                fs.unlinkSync(file.path);
            } catch (err) {
                console.error("Error cleaning up upload file:", err);
            }
        }
    }
});

app.post("/sendText", async (req, res) => {
    const targetId = sanitizeClientId(req.body.clientId || clientId);
    const { groupId, text } = req.body;

    const entry = checkSessionReadiness(targetId, res);
    if (!entry) return;

    if (!groupId || !text) {
        return res.status(400).json({ message: "groupId and text are required" });
    }

    try {
        const jid = formatJid(groupId);
        const sendResult = await enqueueSocketSend(targetId, async () => {
            await simulateTypingPresence(targetId, entry.sock!, jid, { textForDelay: String(text) });
            return await entry.sock!.sendMessage(jid, { text: String(text) });
        });
        if (jid.endsWith("@newsletter")) {
            registerKnownNewsletter(jid);
        }
        res.json({
            message: "Text message sent successfully",
            messageId: sendResult?.key?.id,
            key: sendResult?.key,
        });
    } catch (error: any) {
        console.error(`[${targetId}] Error sending text message:`, error);
        res.status(500).json({
            message: "Failed to send text message",
            error: error?.message || String(error),
        });
    }
});

app.post("/sendMedia", upload.single("media"), async (req, res) => {
    const targetId = sanitizeClientId(req.body.clientId || clientId);
    const { groupId, caption } = req.body;
    const file = req.file;

    try {
        const entry = checkSessionReadiness(targetId, res);
        if (!entry) return;

        if (!file) {
            return res.status(400).json({ message: "No media file provided" });
        }

        const mimeType = (mime.lookup(file.originalname || file.path) || file.mimetype || "") as string;
        const isVideoExt = /\.(mp4|mov|avi|mkv|webm|flv|wmv|m4v|3gp|ts|m4p|mpg|mpeg)$/i.test(file.originalname || file.path);
        if (mimeType.startsWith("video/") || isVideoExt) {
            console.warn(`[${targetId}] 🚫 Video forwarding is banned. Rejected: ${file.originalname}`);
            return res.status(403).json({ message: "Video forwarding is banned" });
        }

        const jid = formatJid(groupId);

        const payload = await buildMediaPayload(file.path, file.originalname, caption);
        const sendResult = await enqueueSocketSend(targetId, async () => {
            await simulateTypingPresence(targetId, entry.sock!, jid, { isMedia: true, textForDelay: caption });
            return await entry.sock!.sendMessage(jid, payload);
        });

        if (jid.endsWith("@newsletter")) {
            registerKnownNewsletter(jid);
        }

        res.json({
            message: "Media message sent successfully",
            messageId: sendResult?.key?.id,
            key: sendResult?.key,
        });
    } catch (error: any) {
        console.error(`[${targetId}] Error sending media message:`, error);
        res.status(500).json({
            message: "Failed to send media message",
            error: error?.message || String(error),
        });
    } finally {
        if (file && fs.existsSync(file.path)) {
            try {
                fs.unlinkSync(file.path);
            } catch (err) {
                console.error("Error cleaning up upload file:", err);
            }
        }
    }
});

app.post("/editMessage", async (req, res) => {
    const targetId = sanitizeClientId(req.body.clientId || clientId);
    const { groupId, text, key, messageId } = req.body;

    const entry = checkSessionReadiness(targetId, res);
    if (!entry) return;

    if (!groupId || !text || (!key && !messageId)) {
        return res.status(400).json({ message: "groupId, text, and key (or messageId) are required" });
    }

    try {
        const jid = formatJid(groupId);
        let messageKey: any = key;
        if (typeof key === "string") {
            try {
                messageKey = JSON.parse(key);
            } catch (_) {
                messageKey = { remoteJid: jid, id: key, fromMe: true };
            }
        }
        if (!messageKey && messageId) {
            messageKey = { remoteJid: jid, id: messageId, fromMe: true };
        }

        const editResult = await enqueueSocketSend(targetId, async () => {
            return await entry.sock!.sendMessage(jid, {
                text: String(text),
                edit: messageKey,
            });
        });

        res.json({
            message: "Message edited successfully",
            messageId: editResult?.key?.id || messageKey?.id,
            key: editResult?.key || messageKey,
        });
    } catch (error: any) {
        console.error(`[${targetId}] Error editing message in ${groupId}:`, error);
        res.status(500).json({
            message: "Failed to edit message",
            error: error?.message || String(error),
        });
    }
});

app.post("/deleteMessage", async (req, res) => {
    const targetId = sanitizeClientId(req.body.clientId || clientId);
    const { groupId, key, messageId } = req.body;

    const entry = checkSessionReadiness(targetId, res);
    if (!entry) return;

    if (!groupId || (!key && !messageId)) {
        return res.status(400).json({ message: "groupId and key (or messageId) are required" });
    }

    try {
        const jid = formatJid(groupId);
        let messageKey: any = key;
        if (typeof key === "string") {
            try {
                messageKey = JSON.parse(key);
            } catch (_) {
                messageKey = { remoteJid: jid, id: key, fromMe: true };
            }
        }
        if (!messageKey && messageId) {
            messageKey = { remoteJid: jid, id: messageId, fromMe: true };
        }

        await enqueueSocketSend(targetId, async () => {
            return await entry.sock!.sendMessage(jid, {
                delete: messageKey,
            });
        });

        res.json({
            message: "Message deleted successfully",
            messageId: messageKey?.id,
        });
    } catch (error: any) {
        console.error(`[${targetId}] Error deleting message in ${groupId}:`, error);
        res.status(500).json({
            message: "Failed to delete message",
            error: error?.message || String(error),
        });
    }
});

app.post("/logout", async (req, res) => {
    const targetId = sanitizeClientId(req.body.clientId || clientId);
    const entry = sessions[targetId];

    if (!(entry && entry.sock)) {
        await destroySession(targetId, true);
        return res.json({ message: "Session not found, cleaned up files." });
    }

    try {
        try {
            await entry.sock.logout();
        } catch (_) {}
        await destroySession(targetId, true);
        res.json({ message: "Logged out successfully" });
    } catch (error: any) {
        console.error(`[${targetId}] Logout error:`, error);
        await destroySession(targetId, true);
        res.status(500).json({
            message: "Logout failed",
            error: error?.message || String(error),
        });
    }
});

// Global error handler for unhandled express errors
app.use((err: any, req: express.Request, res: express.Response, next: express.NextFunction) => {
    if (req.file && fs.existsSync(req.file.path)) {
        try {
            fs.unlinkSync(req.file.path);
        } catch (_) {}
    }
    console.error("Unhandled express error:", err);
    res.status(err.status || 500).json({
        message: err.message || "Internal server error",
        error: String(err),
    });
});

// ---------- Session Bootstrap & Maintenance on Startup ----------

if (!fs.existsSync(".baileys_auth")) {
    fs.mkdirSync(".baileys_auth", { recursive: true });
}

if (!fs.existsSync("uploads")) {
    fs.mkdirSync("uploads", { recursive: true });
}

cleanupUploadsFolder();
setInterval(cleanupUploadsFolder, 15 * 60 * 1000);

function initializeAllSessions() {
    const sessionFolder = ".baileys_auth";
    setTimeout(() => {
        try {
            if (!fs.existsSync(sessionFolder)) return;
            const folderContents = fs.readdirSync(sessionFolder);
            for (const folderName of folderContents) {
                if (folderName.startsWith("session-")) {
                    const id = sanitizeClientId(folderName.replace("session-", ""));
                    createOrRestartSession(id).catch((err) => {
                        console.error(`[${id}] Error auto-starting session:`, err);
                    });
                }
            }
        } catch (error) {
            console.error("Error auto-starting sessions:", error);
        }
    }, 1000);
}

initializeAllSessions();

const port = process.env.PORT || 5426;
app.listen(port, () => {
    console.log(`Server is ready on port ${port}`);
});
