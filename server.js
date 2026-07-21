const express = require('express');
const cors = require('cors');
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');
const https = require('https');

// --- Config ---
const PORT = process.env.PORT || 3001;
const ALLOWED_ORIGIN = process.env.ALLOWED_ORIGIN || '*';

const BASE_DIR = path.resolve(__dirname);
const BOTS_DIR = path.join(BASE_DIR, 'bots');
const DATA_DIR = path.join(BASE_DIR, 'data');
const BOTS_DB = path.join(DATA_DIR, 'bots.json');

// In-memory map of running bot processes { botId: { process, stdoutFile, stderrFile, logBuffer, startTime } }
const runningBots = {};

// --- Bootstrap ---
[BOTS_DIR, DATA_DIR].forEach(dir => {
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
});

function loadBots() {
  try { return JSON.parse(fs.readFileSync(BOTS_DB, 'utf8')); }
  catch { return []; }
}

function saveBots(bots) {
  fs.writeFileSync(BOTS_DB, JSON.stringify(bots, null, 2), 'utf8');
}

// --- Express setup ---
const app = express();
app.use(cors({ origin: ALLOWED_ORIGIN }));
app.use(express.json());

// Simple health check
app.get('/health', (req, res) => res.json({ status: 'ok' }));

// ==================== DEPLOY ====================
app.post('/api/bots/deploy', async (req, res) => {
  const { botId, apiId, apiHash, phone, sudoUser, botToken, name } = req.body;
  if (!botId || !apiId || !apiHash || !phone || !sudoUser) {
    return res.status(400).json({ success: false, error: 'Missing required fields: botId, apiId, apiHash, phone, sudoUser' });
  }
  const bots = loadBots();
  if (bots.find(b => b.id === botId)) {
    return res.status(409).json({ success: false, error: 'Bot ID already exists' });
  }

  const botDir = path.join(BOTS_DIR, botId);
  if (fs.existsSync(botDir)) fs.rmSync(botDir, { recursive: true, force: true });
  fs.mkdirSync(botDir, { recursive: true });

  // Download app.py
  const appPy = path.join(botDir, 'app.py');
  try {
    await downloadFile('https://raw.githubusercontent.com/FuriousGamer414/telegram-python-userbot/main/app.py', appPy);
  } catch (e) {
    fs.rmSync(botDir, { recursive: true, force: true });
    return res.status(500).json({ success: false, error: 'Failed to download app.py: ' + e.message });
  }

  // Write .env
  const envContent = `API_ID=${apiId}
API_HASH=${apiHash}
SUDO_USER=${sudoUser}
BOT_TOKEN=${botToken || ''}
PHONE=${phone}
`;
  fs.writeFileSync(path.join(botDir, '.env'), envContent);

  // Write requirements.txt
  fs.writeFileSync(path.join(botDir, 'requirements.txt'), `telethon\npython-dotenv\naiohttp\nyt-dlp\nhachoir\npython-dateutil\nPillow\ngtts\n`);

  // Create venv & install deps
  try {
    await runCmd('python3', ['-m', 'venv', 'venv'], botDir);
    const pip = path.join(botDir, 'venv', 'bin', 'pip');
    await runCmd(pip, ['install', '-r', 'requirements.txt'], botDir);
  } catch (e) {
    fs.rmSync(botDir, { recursive: true, force: true });
    return res.status(500).json({ success: false, error: 'Failed to install Python deps: ' + e.message });
  }

  // Save to DB
  const botEntry = {
    id: botId, name: name || botId, apiId, phone, sudoUser, botToken: botToken || '',
    status: 'deployed', pid: null, createdAt: new Date().toISOString(), updatedAt: new Date().toISOString()
  };
  bots.push(botEntry);
  saveBots(bots);

  res.json({ success: true, message: 'Bot deployed. Use /api/bots/:id/start to run it.', bot: botEntry });
});

// ==================== LIST ====================
app.get('/api/bots', (req, res) => {
  const bots = loadBots();
  const enriched = bots.map(b => {
    const running = runningBots[b.id];
    const status = running ? 'running' : b.status;
    const uptime = running ? Math.floor((Date.now() - running.startTime) / 1000) : 0;
    return { ...b, status, uptime };
  });
  res.json(enriched);
});

// ==================== START ====================
app.post('/api/bots/:id/start', (req, res) => {
  const { id } = req.params;
  const bots = loadBots();
  const bot = bots.find(b => b.id === id);
  if (!bot) return res.status(404).json({ success: false, error: 'Bot not found' });

  if (runningBots[id]) {
    return res.json({ success: true, status: 'already running', needsOtp: false });
  }

  const botDir = path.join(BOTS_DIR, id);
  if (!fs.existsSync(botDir)) return res.status(404).json({ success: false, error: 'Bot directory not found' });

  const python = path.join(botDir, 'venv', 'bin', 'python3');
  const appPy = path.join(botDir, 'app.py');
  const logFile = path.join(botDir, 'bot.log');

  if (!fs.existsSync(python)) return res.status(500).json({ success: false, error: 'Virtual environment not found. Re-deploy.' });

  // Start the Python process with PTY-like behavior
  const proc = spawn(python, [appPy], {
    cwd: botDir,
    env: { ...process.env, ...require('dotenv').config({ path: path.join(botDir, '.env') }).parsed },
    stdio: ['pipe', 'pipe', 'pipe']
  });

  const logStream = fs.createWriteStream(logFile, { flags: 'a' });
  let logBuffer = '';
  const startTime = Date.now();

  proc.stdout.on('data', (data) => {
    const text = data.toString();
    logBuffer += text;
    logStream.write(text);
  });

  proc.stderr.on('data', (data) => {
    const text = data.toString();
    logBuffer += text;
    logStream.write(text);
  });

  proc.on('exit', (code) => {
    logStream.end();
    delete runningBots[id];
    bot.status = 'stopped';
    bot.pid = null;
    saveBots(bots);
  });

  runningBots[id] = { process: proc, logBuffer, logStream, startTime, botDir };
  bot.status = 'starting';
  bot.pid = proc.pid;
  saveBots(bots);

  // Check if it needs OTP after a moment
  setTimeout(() => {
    const needsOtp = logBuffer.includes('Please enter your phone') || logBuffer.includes('Please enter the code');
    // Send the phone number if the prompt shows
    if (logBuffer.includes('Please enter your phone')) {
      proc.stdin.write(bot.phone + '\n');
    }
  }, 3000);

  res.json({ success: true, status: 'starting' });
});

// ==================== OTP ====================
app.post('/api/bots/:id/otp', (req, res) => {
  const { id } = req.params;
  const { otp } = req.body;
  if (!otp) return res.status(400).json({ success: false, error: 'OTP code required' });

  const runner = runningBots[id];
  if (!runner) return res.status(404).json({ success: false, error: 'Bot not running' });

  runner.process.stdin.write(otp + '\n');

  // Update status to running after OTP
  setTimeout(() => {
    const bots = loadBots();
    const bot = bots.find(b => b.id === id);
    if (bot) { bot.status = 'running'; saveBots(bots); }
  }, 5000);

  res.json({ success: true, status: 'running', message: 'OTP sent' });
});

// ==================== STOP ====================
app.post('/api/bots/:id/stop', (req, res) => {
  const { id } = req.params;
  const runner = runningBots[id];
  if (runner) {
    try { runner.process.kill('SIGTERM'); } catch {}
    try { runner.process.kill('SIGKILL'); } catch {}
    runner.logStream.end();
    delete runningBots[id];
  }
  const bots = loadBots();
  const bot = bots.find(b => b.id === id);
  if (bot) { bot.status = 'stopped'; bot.pid = null; saveBots(bots); }
  res.json({ success: true, status: 'stopped' });
});

// ==================== LOGS ====================
app.get('/api/bots/:id/logs', (req, res) => {
  const { id } = req.params;
  const botDir = path.join(BOTS_DIR, id);
  const logFile = path.join(botDir, 'bot.log');

  let logs = '';
  try { logs = fs.readFileSync(logFile, 'utf8'); } catch { logs = 'No logs yet.'; }

  const maxLines = parseInt(req.query.lines) || 200;
  const lines = logs.split('\n').slice(-maxLines).join('\n');
  res.json({ logs: lines });
});

// ==================== DELETE ====================
app.delete('/api/bots/:id', (req, res) => {
  const { id } = req.params;
  // Stop if running
  const runner = runningBots[id];
  if (runner) {
    try { runner.process.kill('SIGTERM'); } catch {}
    try { runner.process.kill('SIGKILL'); } catch {}
    runner.logStream.end();
    delete runningBots[id];
  }
  // Remove directory
  const botDir = path.join(BOTS_DIR, id);
  try { fs.rmSync(botDir, { recursive: true, force: true }); } catch {}
  // Remove from DB
  const bots = loadBots();
  const idx = bots.findIndex(b => b.id === id);
  if (idx !== -1) bots.splice(idx, 1);
  saveBots(bots);
  res.json({ success: true });
});

// ==================== STATS ====================
app.get('/api/stats', (req, res) => {
  const bots = loadBots();
  const totalBots = bots.length;
  const runningBotsCount = Object.keys(runningBots).length;
  res.json({ totalBots, runningBots: runningBotsCount, stoppedBots: totalBots - runningBotsCount });
});

// ==================== HELPERS ====================
function downloadFile(url, dest) {
  return new Promise((resolve, reject) => {
    const file = fs.createWriteStream(dest);
    https.get(url, (response) => {
      if (response.statusCode !== 200) {
        reject(new Error(`HTTP ${response.statusCode}`));
        return;
      }
      response.pipe(file);
      file.on('finish', () => { file.close(); resolve(); });
    }).on('error', (err) => { fs.unlink(dest, () => {}); reject(err); });
  });
}

function runCmd(cmd, args, cwd) {
  return new Promise((resolve, reject) => {
    const proc = spawn(cmd, args, { cwd, stdio: ['ignore', 'pipe', 'pipe'] });
    let out = '', err = '';
    proc.stdout.on('data', d => out += d.toString());
    proc.stderr.on('data', d => err += d.toString());
    proc.on('close', (code) => {
      if (code === 0) resolve(out);
      else reject(new Error(`Exit code ${code}: ${err.slice(0, 200)}`));
    });
    proc.on('error', reject);
  });
}

// ==================== START ====================
app.listen(PORT, '0.0.0.0', () => {
  console.log(`🚀 Bot Runner API running on http://0.0.0.0:${PORT}`);
});
