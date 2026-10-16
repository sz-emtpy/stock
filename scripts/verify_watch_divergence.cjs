// 无头 Chrome CDP 验证：成功率面板、关注圈刷新、K线背驰段。
//
// 跑法：先确保 http://127.0.0.1:8787 已起服务，再执行：
//   node scripts/verify_watch_divergence.cjs
//
// Chrome 必须带 --no-sandbox（本机沙箱下不起进程）。Node 22 自带全局
// WebSocket，可直接连 CDP，不必装 puppeteer。

'use strict';

const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const CDP_PORT = 9333;
const BASE = 'http://127.0.0.1:8787';
const SHOT_DIR = path.join(__dirname, '..', 'docs');

const sleep = ms => new Promise(r => setTimeout(r, ms));

function rpc(ws, method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = Math.floor(Math.random() * 1e9);
    const onMsg = ev => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.id !== id) return;
      ws.removeEventListener('message', onMsg);
      if (msg.error) reject(new Error(method + ': ' + JSON.stringify(msg.error)));
      else resolve(msg.result);
    };
    ws.addEventListener('message', onMsg);
    ws.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(ws, expression) {
  const r = await rpc(ws, 'Runtime.evaluate', {
    expression, returnByValue: true, awaitPromise: true,
  });
  if (r.exceptionDetails) {
    throw new Error('JS 异常: ' + JSON.stringify(r.exceptionDetails.exception && r.exceptionDetails.exception.description || r.exceptionDetails.text));
  }
  return r.result.value;
}

async function waitFor(ws, expr, timeout = 20000, label = '') {
  const start = Date.now();
  for (;;) {
    try {
      const v = await evaluate(ws, expr);
      if (v) return v;
    } catch (e) { /* 页面上元素还没就绪，继续等 */ }
    if (Date.now() - start > timeout) throw new Error('等待超时: ' + (label || expr));
    await sleep(300);
  }
}

(async () => {
  const profile = fs.mkdtempSync('/tmp/chanpicker-cdp-');
  const chrome = spawn(CHROME, [
    '--headless=new', `--remote-debugging-port=${CDP_PORT}`, '--no-sandbox',
    '--disable-gpu', '--disable-dev-shm-usage', '--window-size=1600,1200',
    `--user-data-dir=${profile}`, 'about:blank',
  ], { stdio: 'ignore' });

  const cleanup = () => { try { chrome.kill('SIGKILL'); } catch {} };
  process.on('exit', cleanup);

  let ws;
  const results = [];
  const check = (name, ok, detail = '') => {
    results.push({ name, ok, detail });
    console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  — ' + detail : ''}`);
  };

  try {
    // 等 CDP 端口就绪
    let target = null;
    for (let i = 0; i < 40 && !target; i++) {
      await sleep(250);
      try {
        const res = await fetch(`http://127.0.0.1:${CDP_PORT}/json/list`);
        const list = await res.json();
        target = list.find(t => t.type === 'page');
      } catch {}
    }
    if (!target) throw new Error('CDP 端口未就绪');
    ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((res, rej) => {
      ws.addEventListener('open', res);
      ws.addEventListener('error', () => rej(new Error('WebSocket 连接失败')));
    });
    await rpc(ws, 'Runtime.enable');
    await rpc(ws, 'Page.enable');

    const errors = [];
    ws.addEventListener('message', ev => {
      let m; try { m = JSON.parse(ev.data); } catch { return; }
      if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') {
        errors.push((m.params.args || []).map(a => a.value || a.description || '').join(' '));
      }
      if (m.method === 'Runtime.exceptionThrown') {
        errors.push(m.params.exceptionDetails.exception && m.params.exceptionDetails.exception.description || 'uncaught');
      }
    });

    await rpc(ws, 'Page.navigate', { url: BASE });
    await waitFor(ws, `document.querySelector('nav button.active') !== null`, 15000, '页面初始化');

    // 1) 默认页签有数据（历史 bug：默认页空白）
    const marketVisible = await evaluate(ws, `getComputedStyle(document.querySelector('#tab-market')).display`);
    check('默认页签为市场扫描且可见', marketVisible === 'block', 'display=' + marketVisible);
    const firstRows = await waitFor(ws,
      `document.querySelectorAll('#table-first tbody tr').length`, 20000, '一买候选表有行');
    check('一买候选表有数据', firstRows > 0, `${firstRows} 行`);

    // 2) 成功率面板
    const rateText = await waitFor(ws, `($('success-kv').textContent || '').trim().length > 10`, 15000, '成功率面板渲染');
    check('历史买点成功率面板已渲染', Boolean(rateText));
    const rateItems = await evaluate(ws, `Array.from(document.querySelectorAll('#success-kv .item')).map(e => e.querySelector('.k').textContent + '=' + e.querySelector('.v').textContent)`);
    check('成功率含一买/二买/关注圈三项', rateItems.length >= 3, JSON.stringify(rateItems));
    const firstRate = rateItems.find(x => x.startsWith('一买成功率')) || '';
    check('一买成功率是百分数', /\d+(\.\d+)?%|—/.test(firstRate), firstRate);
    const waitNote = await evaluate(ws, `$('wait-bars-note').textContent`);
    check('考验窗口文案由后端口径驱动', /^\d+$/.test(waitNote), `wait_bars=${waitNote}`);

    // 3) 关注圈：刷新按钮 + 出圈计数
    await evaluate(ws, `switchTab('watch')`);
    await waitFor(ws, `document.querySelectorAll('#table-watch tbody tr').length > 0`, 15000, '关注圈表有行');
    const btnExists = await evaluate(ws, `!!$('btn-watch-refresh')`);
    check('关注圈有「重评关注圈」按钮', btnExists);
    const watchMeta = await evaluate(ws, `$('watch-meta').textContent`);
    check('关注圈显示进出圈计数', /在圈 \d+ 只/.test(watchMeta) && /已出圈 \d+ 只/.test(watchMeta), watchMeta);
    const noClosed = await evaluate(ws,
      `!Array.from(document.querySelectorAll('#table-watch tbody tr')).some(tr => /一买失败|二买失败/.test(tr.textContent))`);
    check('关注圈已不含一买/二买失败记录', noClosed);
    const stateTags = await evaluate(ws,
      `Array.from(new Set(Array.from(document.querySelectorAll('#table-watch .tag')).map(t => t.textContent)))`);
    check('状态标签均已中文化', stateTags.every(t => /^[一-龥]+$/.test(t)), JSON.stringify(stateTags));

    // 4) 点一次刷新（本地重评，应秒回）
    const t0 = Date.now();
    await evaluate(ws, `$('btn-watch-refresh').click()`);
    const refreshed = await waitFor(ws,
      `!$('btn-watch-refresh').disabled && $('btn-watch-refresh').textContent === '重评关注圈'`,
      60000, '重评完成');
    check('重评按钮可点击并恢复可用', refreshed, `耗时 ${((Date.now() - t0) / 1000).toFixed(1)}s`);
    const afterRefresh = await evaluate(ws, `$('watch-meta').textContent`);
    check('重评后进出圈计数已刷新', /在圈 \d+ 只/.test(afterRefresh), afterRefresh);

    // 5) K线背驰段
    await evaluate(ws, `$('symbol-input').value = 'sz002916'; switchTab('stock'); runAnalyze()`);
    await waitFor(ws, `!!(state.analysis && state.analysis.bars && state.analysis.bars.length)`, 25000, '个股分析完成');
    const chartSeries = await waitFor(ws, `state.chart ? state.chart.getOption().series.map(s => s.name) : []`, 10000, '图表序列');
    const names = Array.isArray(chartSeries) ? chartSeries : [];
    check('图表含「背驰前段」序列', names.includes('背驰前段'), JSON.stringify(names));
    check('图表含「背驰段」序列', names.includes('背驰段'));

    const legend = await evaluate(ws, `state.chart.getOption().legend[0].data`);
    check('图例可开关背驰段', legend.includes('背驰前段') && legend.includes('背驰段'), JSON.stringify(legend));

    // 默认只画最近一个信号的两段
    const defaultCount = await evaluate(ws, `state.chart.getOption().series.filter(s => s.name === '背驰段' || s.name === '背驰前段').length`);
    check('默认只描最近一个背驰段', defaultCount === 2, `共 ${defaultCount} 条线`);

    // 勾上「显示全部」
    await evaluate(ws, `$('divergence-all').checked = true; $('divergence-all').dispatchEvent(new Event('change'))`);
    await sleep(600);
    const allCount = await evaluate(ws, `state.chart.getOption().series.filter(s => s.name === '背驰段' || s.name === '背驰前段').length`);
    check('勾选后描出全部背驰段', allCount > 2, `共 ${allCount} 条线`);

    // 端点带力度标签
    const labelText = await evaluate(ws, `JSON.stringify(state.chart.getOption().series.filter(s => s.name === '背驰段').map(s => s.data.map(d => d.label && d.label.formatter)))`);
    check('背驰段端点带力度标签', /力度/.test(labelText), labelText.slice(0, 160));

    // 老数据（无坐标）不应崩图：直接喂一份删掉 segment 的 payload
    const survives = await evaluate(ws, `(() => {
      try {
        const clone = JSON.parse(JSON.stringify(state.analysis));
        clone.analysis.signals.forEach(s => { delete s.meta.previous_segment; delete s.meta.current_segment; });
        renderChart(clone);
        return state.chart.getOption().series.filter(s => s.name === '背驰段').length === 0;
      } catch (e) { return 'EXC:' + e.message; }
    })()`);
    check('老数据无背驰段坐标时图表不崩', survives === true, String(survives));
    await evaluate(ws, `renderChart(state.analysis)`);

    // 6) 信号明细不含坐标对象噪音
    const metaText = await evaluate(ws, `$('signal-detail').style.display === 'none' ? '' : $('signal-meta').textContent`);
    check('信号明细未出现 previous_segment 原对象', !/previous_segment/.test(metaText));

    await evaluate(ws, `switchTab('market')`);
    await sleep(400);
    const shot = path.join(SHOT_DIR, 'verify-success-rate.png');
    const img = await rpc(ws, 'Page.captureScreenshot', { format: 'png' });
    fs.writeFileSync(shot, Buffer.from(img.data, 'base64'));
    console.log(`截图: ${shot}`);

    check('控制台无错误', errors.length === 0, errors.slice(0, 3).join(' | '));

  } catch (e) {
    console.log('ERROR  ' + e.message);
    results.push({ name: '脚本执行', ok: false, detail: e.message });
  } finally {
    cleanup();
  }

  const failed = results.filter(r => !r.ok);
  console.log(`\n合计 ${results.length} 项，失败 ${failed.length} 项`);
  process.exit(failed.length ? 1 : 0);
})();
