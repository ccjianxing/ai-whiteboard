/**
 * 网页截图（CDP 版）——比命令行 --screenshot 快得多，而且能截整页
 *
 * 为什么不用 `msedge --screenshot`：
 *   它必须等页面的 load 事件（包含所有图片、广告、统计脚本），外网慢站动辄 20~40 秒，
 *   再加上 55 秒超时，用户看到的就是"一直是截图中 / 截图超时"。
 *
 * 这里用 CDP：
 *   - 等 DOMContentLoaded 就够（可选再等 load，但最多等 maxWait 秒，谁先到算谁）
 *   - captureBeyondViewport 可以一次截下整页高度（用户要的"看全貌"）
 *   - 无论成功失败都杀整棵进程树，不留残留
 *
 * 用法：node 截图.js <url> <输出png> [宽] [高] [最多等秒]
 */
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const sleep = ms => new Promise(r => setTimeout(r, ms));

function findBrowser() {
  // 服务端已经找到浏览器了（可能装在别处），允许它直接指定，避免两边判断不一致
  if (process.env.WB_EDGE && fs.existsSync(process.env.WB_EDGE)) return process.env.WB_EDGE;
  return ['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
          'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
          'C:/Program Files/Google/Chrome/Application/chrome.exe',
          'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
          '/usr/bin/chromium', '/usr/bin/google-chrome'].find(p => fs.existsSync(p));
}

(async () => {
  const [url, out, wArg, hArg, waitArg] = process.argv.slice(2);
  if (!url || !out) { console.error('用法: node 截图.js <url> <out.png> [宽] [高] [最多等秒]'); process.exit(2); }
  const W = Math.max(600, Math.min(2400, parseInt(wArg, 10) || 1440));
  const H = Math.max(400, Math.min(8000, parseInt(hArg, 10) || 900));
  const MAXWAIT = Math.max(2, Math.min(40, parseInt(waitArg, 10) || 12));   // 最多等多少秒
  const exe = findBrowser();
  if (!exe) { console.error('没找到 Edge/Chrome'); process.exit(3); }

  const port = 9400 + Math.floor(Math.random() * 300);
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'wbshot-'));
  const proc = spawn(exe, ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--hide-scrollbars', '--force-device-scale-factor=1', '--disable-extensions',
    '--disable-background-networking', '--disable-sync', '--no-pings',
    // 内网系统 / 自签证书的站点不加这两条会直接卡在证书告警页，截出来是空白
    '--ignore-certificate-errors', '--allow-running-insecure-content',
    `--user-data-dir=${profile}`, `--remote-debugging-port=${port}`, 'about:blank'],
    { stdio: 'ignore', detached: false });

  const cleanup = () => {
    try {
      if (process.platform === 'win32') {
        spawn('taskkill', ['/PID', String(proc.pid), '/T', '/F'], { stdio: 'ignore' });
      } else { proc.kill('SIGKILL'); }
    } catch (e) { }
    // 浏览器可能还占着 profile 里的文件，稍等一下再删；删不掉也不影响出图
    setTimeout(() => { try { fs.rmSync(profile, { recursive: true, force: true }); } catch (e) { } }, 400);
  };
  // 顺手把以前没清干净的临时 profile 收掉
  try {
    for (const d of fs.readdirSync(os.tmpdir())) {
      if (d.indexOf('wbshot-') === 0) {
        const fp = path.join(os.tmpdir(), d);
        try { if (Date.now() - fs.statSync(fp).mtimeMs > 60000) fs.rmSync(fp, { recursive: true, force: true }); } catch (e) { }
      }
    }
  } catch (e) { }
  process.on('exit', cleanup);

  const fail = (msg) => { console.error(msg); cleanup(); process.exit(1); };

  // 等调试端口
  let ver = null;
  for (let i = 0; i < 60; i++) {
    try { const r = await fetch(`http://127.0.0.1:${port}/json/version`); if (r.ok) { ver = await r.json(); break; } } catch (e) { }
    await sleep(150);
  }
  if (!ver) fail('浏览器调试端口没起来');

  let ws, id = 0;
  const pend = new Map();
  try {
    const t = await (await fetch(`http://127.0.0.1:${port}/json/new?${encodeURIComponent('about:blank')}`, { method: 'PUT' })).json();
    ws = new WebSocket(t.webSocketDebuggerUrl);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error('ws 连接失败')); setTimeout(rej, 8000); });
  } catch (e) { fail('连不上页面调试通道: ' + e.message); }

  const send = (method, params) => new Promise((res) => {
    const i = ++id; pend.set(i, res);
    ws.send(JSON.stringify({ id: i, method, params: params || {} }));
  });
  let loaded = false;
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); return; }
    if (m.method === 'Page.loadEventFired') loaded = true;
    // 页面里只要有 alert/confirm/prompt，无头浏览器就没人去点，整页会卡住、截图永远完不成。
    // 这里一律关掉（拒绝），保证"给任意页面截图"都不会挂在对话框上。
    if (m.method === 'Page.javascriptDialogOpening') {
      try { send('Page.handleJavaScriptDialog', { accept: false }); } catch (e) { }
    }
  };

  await send('Page.enable');
  await send('Runtime.enable');
  await send('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: 1, mobile: false });

  const t0 = Date.now();
  try {
    await send('Page.navigate', { url });
  } catch (e) { fail('导航失败: ' + e.message); }

  // 等到 load 事件，或最多等 MAXWAIT 秒（慢站不再无限拖）
  const deadline = Date.now() + MAXWAIT * 1000;
  while (!loaded && Date.now() < deadline) await sleep(120);
  await sleep(350);   // 再给渲染一点时间，避免截到半张

  // 量出整页高度，一次截全（用户要的"看全貌"）
  let fullH = H;
  try {
    const r = await send('Runtime.evaluate', {
      expression: '(function(){var b=document.body,e=document.documentElement;return Math.max(b?b.scrollHeight:0,e?e.scrollHeight:0,window.innerHeight||0);})()',
      returnByValue: true
    });
    const v = r && r.result && r.result.result ? r.result.result.value : 0;
    // 整页高度以"用户选的那个高度"为上限：GitHub 整页能到 8000px+，一张图 2MB 多，
    // 贴到画板上又大又慢。用户选"长图"就给到长图，选"首屏"就只截首屏。
    if (v && isFinite(v)) fullH = Math.max(400, Math.min(Math.max(H, 600), Math.round(v)));
  } catch (e) { }
  await send('Emulation.setDeviceMetricsOverride', { width: W, height: fullH, deviceScaleFactor: 1, mobile: false });
  await sleep(250);

  let shot = null;
  try {
    shot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
  } catch (e) { }
  const data = shot && shot.result && shot.result.data;
  if (!data) fail('截图数据为空');

  fs.writeFileSync(out, Buffer.from(data, 'base64'));
  const ms = Date.now() - t0;
  console.log(JSON.stringify({ ok: true, ms, loaded, width: W, height: fullH, bytes: fs.statSync(out).size }));
  cleanup();
  process.exit(0);
})();
