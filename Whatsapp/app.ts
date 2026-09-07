import makeWASocket, {
    DisconnectReason,
    useMultiFileAuthState,
    fetchLatestBaileysVersion,
    makeCacheableSignalKeyStore,
    WASocket,
    AnyMessageContent,
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
}

const sessions: Record<string, SessionEntry> = {};

// ---------- Client ID Sanitization ----------
function sanitizeClientId(rawId: any): string {
    const id = String(rawId || clientId || "user").trim();
    const clean = id.replace(/[^a-zA-Z0-9_-]/g, "");
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

    const sock = socketFactory({
        version,
        auth: {
            creds: state.creds,
            keys: makeCacheableSignalKeyStore(state.keys, logger),
        },
        logger,
        printQRInTerminal: false,
        browser: ["Forwarder WhatsApp", "Chrome", "1.0.0"],
        generateHighQualityLinkPreview: false,
    });

    entry.sock = sock;

    sock.ev.on("creds.update", saveCreds);

    sock.ev.on("connection.update", async (update) => {
        const { connection, lastDisconnect, qr } = update;

        if (qr) {
            console.log(`[${safeId}] QR generated`);
            entry.currentQR = qr;
            const listeners = [...entry.qrListeners];
            entry.qrListeners = [];
            listeners.forEach((fn) => fn(qr));
        }

        if (connection === "open") {
            console.log(`[${safeId}] WhatsApp connection open and ready!`);
            entry.isReady = true;
            entry.currentQR = undefined;
            entry.isRestarting = false;
            const listeners = [...entry.readyListeners];
            entry.readyListeners = [];
            listeners.forEach((fn) => fn());
        }

        if (connection === "close") {
            entry.isReady = false;
            entry.currentQR = undefined;
            const statusCode = (lastDisconnect?.error as Boom)?.output?.statusCode;
            console.warn(`[${safeId}] Connection closed with status code: ${statusCode}`);

            if (statusCode === DisconnectReason.loggedOut) {
                console.warn(`[${safeId}] Logged out. Clearing credentials...`);
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
        if (entry.isListening && m.messages) {
            for (const msg of m.messages) {
                if (!msg.key.fromMe) {
                    const text = msg.message?.conversation || msg.message?.extendedTextMessage?.text;
                    if (text) {
                        console.log(`[${safeId}] Incoming message: ${text.slice(0, 100)}`);
                    }
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
    const mem = process.memoryUsage();
    res.status(ready ? 200 : 503).json({
        status: ready ? "ready" : "initializing",
        clientId: safeId,
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

app.post("/createsession", async (req, res) => {
    const targetId = sanitizeClientId(req.body.clientId || clientId);
    console.log("Creating session for clientId:", targetId);

    const existing = sessions[targetId];
    if (existing && existing.isReady) {
        return res.json({ message: "Session already exists and is ready", ready: true });
    }

    let entry = existing;
    if (!entry || !entry.sock) {
        entry = await createOrRestartSession(targetId);
    }

    if (entry.currentQR) {
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

app.post("/startlistening", async (req, res) => {
    const targetId = sanitizeClientId(req.body.clientId || clientId);
    const entry = sessions[targetId];

    if (!(entry && entry.isReady && entry.sock)) {
        return res.status(400).json({ message: "Session is not authorized" });
    }

    if (entry.isListening) {
        return res.json({ message: "Already listening to messages." });
    }

    entry.isListening = true;
    res.json({ message: "Listening to messages" });
});

app.post("/getChatId", async (req, res) => {
    const targetId = sanitizeClientId(req.body.clientId || clientId);
    const { chatName } = req.body;

    if (!chatName) {
        return res.status(400).json({ message: "chatName is required" });
    }

    const entry = sessions[targetId];
    if (!(entry && entry.isReady && entry.sock)) {
        return res.status(400).json({ message: "Session is not authorized" });
    }

    try {
        const sock = entry.sock;
        const searchName = String(chatName).trim();
        const searchLower = searchName.toLowerCase();

        // 1. Check if input is already a direct JID
        if (searchName.endsWith("@g.us") || searchName.endsWith("@newsletter") || searchName.endsWith("@s.whatsapp.net")) {
            return res.json({
                groupId: searchName,
                name: searchName,
                isGroup: searchName.endsWith("@g.us"),
                isChannel: searchName.endsWith("@newsletter"),
            });
        }

        // 2. Search participating groups
        const groups = await sock.groupFetchAllParticipating();
        for (const [id, meta] of Object.entries(groups)) {
            if (meta.subject && meta.subject.trim().toLowerCase() === searchLower) {
                return res.json({
                    groupId: id,
                    name: meta.subject,
                    isGroup: true,
                });
            }
        }

        // 3. Search newsletters if supported
        if (typeof (sock as any).newsletterSubscribed === "function") {
            try {
                const newsletters = await (sock as any).newsletterSubscribed();
                if (Array.isArray(newsletters)) {
                    for (const nl of newsletters) {
                        const name = nl?.thread_metadata?.name?.text || nl?.name;
                        const id = nl?.id || nl?.jid;
                        if (name && id && name.trim().toLowerCase() === searchLower) {
                            return res.json({
                                groupId: id,
                                name: name,
                                isChannel: true,
                            });
                        }
                    }
                }
            } catch (nlErr) {
                console.warn(`[${targetId}] Newsletter fetch warning:`, nlErr);
            }
        }

        return res.status(404).json({ message: "Group or channel not found" });
    } catch (error: any) {
        console.error(`[${targetId}] Error in getChatId:`, error);
        res.status(500).json({
            message: "Internal server error",
            error: error?.message || String(error),
        });
    }
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

    // 2. Fetch subscribed newsletters / channels
    if (typeof (sock as any).newsletterSubscribed === "function") {
        try {
            const newsletters = await (sock as any).newsletterSubscribed();
            if (Array.isArray(newsletters)) {
                for (const nl of newsletters) {
                    const name = nl?.thread_metadata?.name?.text || nl?.name || nl?.id;
                    const id = nl?.id || nl?.jid;
                    if (id) {
                        chats.push({
                            id,
                            name: name || id,
                            type: "newsletter",
                            participantsCount: nl?.thread_metadata?.subscribers_count,
                        });
                    }
                }
            }
        } catch (nlErr) {
            console.warn(`[${targetId}] Error fetching newsletters:`, nlErr);
        }
    }

    chats.sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" }));

    chatDiscoveryCache[targetId] = {
        timestamp: now,
        chats,
    };

    return chats;
}

app.get("/groups", async (req, res) => {
    const targetId = sanitizeClientId((req.query.clientId as string) || clientId);
    const entry = sessions[targetId];
    if (!(entry && entry.isReady && entry.sock)) {
        return res.status(400).json({ message: "Session is not authorized", chats: [], total: 0 });
    }

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
    const entry = sessions[targetId];
    if (!(entry && entry.isReady && entry.sock)) {
        return res.status(400).json({
            message: "Session is not authorized",
            totalAudience: 0,
            groupsCount: 0,
            groupMembers: 0,
            newslettersCount: 0,
            newsletterSubscribers: 0,
            destinations: [],
        });
    }

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
    const entry = sessions[targetId];
    if (!(entry && entry.isReady && entry.sock)) {
        return res.status(400).json({ message: "Session is not authorized", chats: [], total: 0 });
    }

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


// Helper function to build Baileys media payload
function buildMediaPayload(filePath: string, originalName: string | undefined, caption?: string): AnyMessageContent {
    const buffer = fs.readFileSync(filePath);
    const mimeType = (mime.lookup(originalName || filePath) || "application/octet-stream") as string;

    if (mimeType.startsWith("image/")) {
        return {
            image: buffer,
            caption: caption || undefined,
            mimetype: mimeType,
        };
    } else if (mimeType.startsWith("video/")) {
        return {
            video: buffer,
            caption: caption || undefined,
            mimetype: mimeType,
        };
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

// Socket Transmission Queue to enforce minimum inter-frame spacing (350ms)
const sessionQueues: Record<string, Promise<any>> = {};

function enqueueSocketSend<T>(sessionId: string, fn: () => Promise<T>): Promise<T> {
    const prev = sessionQueues[sessionId] || Promise.resolve();
    const next = prev.then(async () => {
        try {
            return await fn();
        } finally {
            await new Promise((resolve) => setTimeout(resolve, 350));
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
        const entry = sessions[targetId];
        if (!(entry && entry.isReady && entry.sock)) {
            return res.status(400).json({ message: "Session is not authorized" });
        }

        const jid = formatJid(groupId);
        if (file) {
            const payload = buildMediaPayload(file.path, file.originalname, caption);
            await enqueueSocketSend(targetId, () => entry.sock!.sendMessage(jid, payload));
        } else if (caption) {
            await enqueueSocketSend(targetId, () => entry.sock!.sendMessage(jid, { text: String(caption) }));
        } else {
            return res.status(400).json({ message: "No media or caption provided" });
        }

        res.json({ message: "Message sent successfully" });
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

    const entry = sessions[targetId];
    if (!(entry && entry.isReady && entry.sock)) {
        return res.status(400).json({ message: "Session is not authorized" });
    }

    if (!groupId || !text) {
        return res.status(400).json({ message: "groupId and text are required" });
    }

    try {
        const jid = formatJid(groupId);
        await enqueueSocketSend(targetId, () => entry.sock!.sendMessage(jid, { text: String(text) }));
        res.json({ message: "Text message sent successfully" });
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
        const entry = sessions[targetId];
        if (!(entry && entry.isReady && entry.sock)) {
            return res.status(400).json({ message: "Session is not authorized" });
        }

        if (!file) {
            return res.status(400).json({ message: "No media file provided" });
        }

        const jid = formatJid(groupId);
        const payload = buildMediaPayload(file.path, file.originalname, caption);
        await enqueueSocketSend(targetId, () => entry.sock!.sendMessage(jid, payload));

        res.json({ message: "Media message sent successfully" });
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
