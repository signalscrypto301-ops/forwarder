import { Client, LocalAuth, MessageMedia } from "whatsapp-web.js";
import express from "express";
import bodyParser from "body-parser";
import "dotenv/config";
import fs from "fs";
import multer from "multer";
import path from "path";

const app = express();
const clientId: string = process.env.CLIENT_ID || "user";
app.use(bodyParser.json());
const sessions: Record<string, Client> = {};
let isListening = false;

// Setup multer for media upload
const storage = multer.diskStorage({
    destination: "uploads/",
    filename: (req, file, cb) => {
        cb(null, Date.now() + "-" + file.originalname);
    },
});
const upload = multer({ storage });

// ---------- Clean Stale Chromium Locks (including dangling symlinks) ----------

function cleanProfileLocks(id: string) {
    const authPath = path.join(".wwebjs_auth", `session-${id}`);
    for (const lockfile of ["SingletonLock", "SingletonSocket", "SingletonCookie"]) {
        const p = path.join(authPath, lockfile);
        try {
            fs.unlinkSync(p);
            console.log(`?? Cleared stale lock: ${lockfile} for ${id}`);
        } catch (_) {}
    }
}

// ---------- Error & Crash Handler for Puppeteer ----------

function handleClientCrash(id: string, error: any) {
    const errStr = String(error?.stack || error?.message || error);
    if (
        errStr.includes("detached Frame") ||
        errStr.includes("Session closed") ||
        errStr.includes("Target closed") ||
        errStr.includes("Execution context was destroyed") ||
        errStr.includes("Protocol error")
    ) {
        console.warn(`?? Detected Puppeteer crash for client ${id}: ${errStr}. Auto-recovering session...`);
        try {
            if (sessions[id]) {
                sessions[id].destroy().catch(() => {});
            }
        } catch (_) {}
        delete sessions[id];
        setTimeout(() => {
            console.log(`?? Reinitializing client ${id}...`);
            createOrRestartSession(id);
        }, 3000);
    }
}

// ---------- Session Management with Reconnect Logic ----------

function createOrRestartSession(id: string) {
    cleanProfileLocks(id);

    if (sessions[id]) {
        try {
            sessions[id].destroy().catch(() => {});
        } catch (_) {}
        delete sessions[id];
    }

    const client = new Client({
        authStrategy: new LocalAuth({ clientId: id }),
        puppeteer: {
            args: [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-accelerated-2d-canvas",
                "--no-first-run",
                "--no-zygote",
                "--disable-gpu"
            ],
            executablePath: process.env.CHROME_PATH
        },
    });

    client.on("ready", () => {
        console.log(`? Client ${id} is ready`);
    });

    client.on("disconnected", (reason) => {
        console.warn(`?? Client ${id} disconnected: ${reason}`);
        delete sessions[id];
        setTimeout(() => {
            console.log(`?? Reinitializing client ${id}...`);
            createOrRestartSession(id);
        }, 3000);
    });

    client.on("auth_failure", (msg) => {
        console.error(`? Auth failure for ${id}:`, msg);
        const authPath = path.join(".wwebjs_auth", `session-${id}`);
        if (fs.existsSync(authPath)) {
            fs.rmSync(authPath, { recursive: true, force: true });
        }
        delete sessions[id];
    });

    client.on("qr", (qr) => {
        console.log(`?? QR generated for ${id}`);
    });

    client.on("change_state", (state) => {
        console.log(`?? Client ${id} state: ${state}`);
    });

    client.initialize().catch((err) => {
        console.error(`Error initializing client ${id}:`, err);
        handleClientCrash(id, err);
    });

    sessions[id] = client;
}

function startSession(id: string) {
    createOrRestartSession(id);
}

// ---------- Express Routes ----------

app.get("/health", (req, res) => {
    const ready = Boolean(sessions[clientId] && sessions[clientId].info);
    res.status(ready ? 200 : 503).json({
        status: ready ? "ready" : "initializing",
        clientId,
        sessionsCount: Object.keys(sessions).length,
        timestamp: new Date().toISOString()
    });
});

app.post("/createsession", (req, res) => {
    const targetId = req.body.clientId || clientId;
    console.log("Creating session for clientId:", targetId);

    if (sessions[targetId] && sessions[targetId].info) {
        return res.status(400).json({ message: "Session already exists" });
    }

    createOrRestartSession(targetId);

    const qrEventListener = (qr: string) => {
        res.json({ qrcode: qr });
        sessions[targetId]?.removeListener("qr", qrEventListener);
    };

    sessions[targetId]?.on("qr", qrEventListener);
});

app.post("/startlistening", async (req, res) => {
    const targetId = req.body.clientId || clientId;

    if (!(sessions[targetId] && sessions[targetId].info)) {
        return res.status(400).json({ message: "Session is not authorized" });
    }

    if (isListening) {
        return res.json({ message: "Already listening to messages." });
    }

    isListening = true;
    const client = sessions[targetId];

    try {
        client.on("message", async (msg) => {
            console.log("?? message received", msg.body);
        });
        res.json({ message: "Listening to messages" });
    } catch (error) {
        console.error("Error starting to listen:", error);
        handleClientCrash(targetId, error);
        res.status(500).json({ message: "Failed to start listening" });
    }
});

app.post("/getChatId", async (req, res) => {
    const targetId = req.body.clientId || clientId;
    const { chatName } = req.body;
    if (!(sessions[targetId] && sessions[targetId].info)) {
        return res.status(400).json({ message: "Session is not authorized" });
    }

    try {
        const channels = await sessions[targetId].getChannels();
        const channel = channels.find((chat: any) => chat.name === chatName);
        if (channel) {
            return res.json({ groupId: channel.id._serialized });
        } else {
            return res.status(404).json({ message: "Group not found" });
        }
    } catch (error) {
        console.error("Error in getChatId:", error);
        handleClientCrash(targetId, error);
        res.status(500).json({ message: "Internal server error" });
    }
});

app.post("/sendToGroup", upload.single("media"), async (req, res) => {
    const targetId = req.body.clientId || clientId;
    const { groupId, caption } = req.body;
    const file = req.file;

    try {
        if (!(sessions[targetId] && sessions[targetId].info)) {
            return res.status(400).json({ message: "Session is not authorized" });
        }

        const client = sessions[targetId];
        if (file) {
            const media = MessageMedia.fromFilePath(file.path);
            if (file.originalname) {
                media.filename = file.originalname;
            }
            await client.sendMessage(groupId, media, { caption });
        } else if (caption) {
            await client.sendMessage(groupId, caption);
        } else {
            return res.status(400).json({ message: "No media or caption provided" });
        }

        res.json({ message: "Message sent successfully" });
    } catch (error) {
        console.error("Error sending message:", error);
        handleClientCrash(targetId, error);
        res.status(500).json({ message: "Failed to send message" });
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
    const targetId = req.body.clientId || clientId;
    const { groupId, text } = req.body;

    if (!(sessions[targetId] && sessions[targetId].info)) {
        return res.status(400).json({ message: "Session is not authorized" });
    }

    try {
        await sessions[targetId].sendMessage(groupId, text);
        res.json({ message: "Text message sent successfully" });
    } catch (error) {
        console.error("Error sending text message:", error);
        handleClientCrash(targetId, error);
        res.status(500).json({ message: "Failed to send text message" });
    }
});

app.post("/sendMedia", upload.single("media"), async (req, res) => {
    const targetId = req.body.clientId || clientId;
    const { groupId, caption } = req.body;
    const file = req.file;

    try {
        if (!(sessions[targetId] && sessions[targetId].info)) {
            return res.status(400).json({ message: "Session is not authorized" });
        }

        if (!file) {
            return res.status(400).json({ message: "No media file provided" });
        }

        const media = MessageMedia.fromFilePath(file.path);
        if (file.originalname) {
            media.filename = file.originalname;
        }
        await sessions[targetId].sendMessage(groupId, media, { caption });
        res.json({ message: "Media message sent successfully" });
    } catch (error) {
        console.error("Error sending media message:", error);
        handleClientCrash(targetId, error);
        res.status(500).json({ message: "Failed to send media message" });
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
    const targetId = req.body.clientId || clientId;

    if (!(sessions[targetId] && sessions[targetId].info)) {
        const authPath = path.join(".wwebjs_auth", `session-${targetId}`);
        if (fs.existsSync(authPath)) {
            fs.rmSync(authPath, { recursive: true, force: true });
        }
        return res.status(400).json({ message: "Session not found, cleaned up files." });
    }

    try {
        await sessions[targetId].logout();
        const authPath = path.join(".wwebjs_auth", `session-${targetId}`);
        if (fs.existsSync(authPath)) {
            fs.rmSync(authPath, { recursive: true, force: true });
        }
        delete sessions[targetId];
        res.json({ message: "Logged out successfully" });
    } catch (error) {
        console.error("Logout error:", error);
        res.status(500).json({ message: "Logout failed" });
    }
});

// ---------- Session Bootstrap on Restart ----------

if (!fs.existsSync(".wwebjs_auth")) {
    fs.mkdirSync(".wwebjs_auth");
}

if (!fs.existsSync("uploads")) {
    fs.mkdirSync("uploads");
}

function initializeAllSessions() {
    const sessionFolder = ".wwebjs_auth";
    setTimeout(() => {
        try {
            const folderContents = fs.readdirSync(sessionFolder);
            for (const folderName of folderContents) {
                if (folderName.startsWith("session-")) {
                    const id = folderName.replace("session-", "");
                    startSession(id);
                }
            }
        } catch (error) {
            console.error("Error initializing sessions:", error);
        }
    }, 1000);
}

initializeAllSessions();

app.listen(process.env.PORT || 5426, () => {
    console.log("?? Server is ready on port " + (process.env.PORT || 5426));
});
