// Small server: serves the show and keeps every screen/phone on the same slide.
const http = require('http');
const fs = require('fs');
const path = require('path');
const { WebSocketServer } = require('ws');

const PORT = process.env.PORT || 3000;
// works whether index.html sits in /public or next to this file
const PUB = fs.existsSync(path.join(__dirname, 'public', 'index.html')) ? path.join(__dirname, 'public') : __dirname;

let current = 0;                                 // current slide index
let vol = 0.3;                                   // music/recitation volume (0–1), starts quiet

const TYPES = { '.html': 'text/html; charset=utf-8', '.mp4': 'video/mp4', '.js': 'text/javascript', '.css': 'text/css', '.png': 'image/png', '.jpg': 'image/jpeg' };
const server = http.createServer((req, res) => {
  const url = decodeURIComponent(req.url.split('?')[0].split('#')[0]);
  const file = path.join(PUB, url === '/' ? 'index.html' : url);
  if (!file.startsWith(PUB)) { res.writeHead(403); return res.end(); }
  fs.stat(file, (err, st) => {
    if (err || !st.isFile()) { res.writeHead(404); return res.end('Not found'); }
    const type = TYPES[path.extname(file).toLowerCase()] || 'application/octet-stream';
    const range = req.headers.range;
    // video needs byte ranges so browsers (Safari above all) can stream and seek it
    if (range && /^bytes=/.test(range)) {
      let [a, b] = range.replace('bytes=', '').split('-');
      let start = parseInt(a, 10), end = b ? parseInt(b, 10) : st.size - 1;
      if (isNaN(start)) { start = st.size - end; end = st.size - 1; }
      if (start >= st.size || end >= st.size || start > end) { res.writeHead(416, { 'Content-Range': `bytes */${st.size}` }); return res.end(); }
      res.writeHead(206, { 'Content-Type': type, 'Content-Range': `bytes ${start}-${end}/${st.size}`, 'Accept-Ranges': 'bytes', 'Content-Length': end - start + 1, 'Cache-Control': 'no-cache' });
      return fs.createReadStream(file, { start, end }).pipe(res);
    }
    res.writeHead(200, { 'Content-Type': type, 'Content-Length': st.size, 'Accept-Ranges': 'bytes', 'Cache-Control': 'no-cache' });
    fs.createReadStream(file).pipe(res);
  });
});

const wss = new WebSocketServer({ server, path: '/ws' });
const screensCount = () => [...wss.clients].filter(c => c.role === 'screen' && c.readyState === 1).length;
const broadcast = () => {
  const msg = JSON.stringify({ t: 'state', i: current, vol, screens: screensCount() });
  wss.clients.forEach(c => { if (c.readyState === 1) c.send(msg); });
};

wss.on('connection', ws => {
  ws.role = 'idle';
  ws.isAlive = true;
  ws.on('pong', () => { ws.isAlive = true; });
  ws.on('message', raw => {
    let m; try { m = JSON.parse(raw); } catch { return; }
    if (m.t === 'hello') {
      ws.role = ['screen', 'remote', 'idle'].includes(m.role) ? m.role : 'idle';
      broadcast();
    } else if (m.t === 'nav' && Number.isInteger(m.i) && m.i >= 0 && m.i < 100) {
      current = m.i;
      broadcast();
    } else if (m.t === 'vol' && typeof m.v === 'number') {
      vol = Math.max(0.05, Math.min(1, m.v));
      broadcast();
    } else if (m.t === 'audio' && (m.a === 'play' || m.a === 'stop')) {
      const msg = JSON.stringify({ t: 'audio', a: m.a });
      wss.clients.forEach(c => { if (c !== ws && c.readyState === 1 && c.role === 'screen') c.send(msg); });
    }
  });
  ws.on('close', broadcast);
  ws.send(JSON.stringify({ t: 'state', i: current, vol, screens: screensCount() }));
});

// keep connections alive on hosts that close idle sockets
setInterval(() => wss.clients.forEach(ws => {
  if (!ws.isAlive) return ws.terminate();
  ws.isAlive = false; ws.ping();
}), 25000);

server.listen(PORT, () => console.log('Show running on port ' + PORT));
