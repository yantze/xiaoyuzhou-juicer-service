'use strict';
const $ = id => document.getElementById(id);
let csrf = '', connected = false, pollTimer, qrDeadline, result, qrGeneration = 0;
function message(text, error = false) { $('message').hidden = !text; $('message').textContent = text; $('message').className = 'message' + (error ? ' error' : ''); }
async function api(path, body) {
  const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST', headers: body === undefined ? {} : {'Content-Type':'application/json','X-CSRF-Token':csrf}, body: body === undefined ? undefined : JSON.stringify(body)});
  let data; try { data = await response.json(); } catch { throw new Error('服务暂不可用，请稍后重试。'); }
  if (!response.ok) { const error = new Error(data.error || '请求失败，请稍后重试。'); error.code = data.code; throw error; }
  return data;
}
async function session() {
  const data = await api('/api/session'); csrf = data.csrf; connected = data.connected;
  $('connection').textContent = connected ? '已连接' : '未连接'; $('connection').className = 'status' + (connected ? ' connected' : '');
  $('account-description').textContent = connected ? `${data.user?.nickname || '你的小宇宙账号'}已连接，可以获取逐字稿。` : '用你的小宇宙 App 扫码，并在手机上确认登录。';
  $('connect').disabled = false; $('connect').textContent = connected ? '重新扫码连接 ↗' : '生成登录二维码 ↗'; $('logout').hidden = !connected; $('fetch').disabled = !connected;
  return data;
}
function stopPolling() { clearTimeout(pollTimer); qrGeneration++; }
async function startQR() {
  stopPolling(); const generation = qrGeneration; $('connect').disabled = true; message('正在创建登录二维码…');
  try {
    const data = await api('/api/auth/qr', {}); if (generation !== qrGeneration) return;
    qrDeadline = data.expiresAt * 1000;
    $('qr-image').src = '/api/auth/qr/image?t=' + Date.now(); $('qr-panel').hidden = false; $('qr-status').textContent = '请使用小宇宙 App 扫码'; message('');
    const poll = async () => {
      if (generation !== qrGeneration) return;
      if (Date.now() >= qrDeadline) { $('qr-status').textContent = '二维码已过期，请重新生成'; return; }
      try {
        const data = await api('/api/auth/qr/poll', {}); if (generation !== qrGeneration) return;
        if (data.status === 'CONFIRMED' || data.status === 'USED') { $('qr-panel').hidden = true; $('qr-image').removeAttribute('src'); await session(); message('连接成功。现在可以粘贴单集链接。'); return; }
        if (data.status === 'EXPIRED') { $('qr-status').textContent = '二维码已过期，请重新生成'; return; }
        $('qr-status').textContent = data.status === 'SCANNED' ? '扫码成功，请在手机上确认登录' : '等待扫码 · ' + Math.ceil((qrDeadline - Date.now()) / 1000) + ' 秒';
        pollTimer = setTimeout(poll, 2000);
      } catch (error) {
        if (error.code === 'SESSION_BUSY' && generation === qrGeneration) { pollTimer = setTimeout(poll, 2000); return; }
        $('qr-status').textContent = '连接未完成'; message(error.message, true);
      }
    };
    pollTimer = setTimeout(poll, 1800);
  } catch (error) { message(error.message, true); }
  finally { $('connect').disabled = false; }
}
function stamp(ms) { const s = Math.floor(ms / 1000); return [Math.floor(s / 3600), Math.floor(s / 60) % 60, s % 60].map(n => String(n).padStart(2,'0')).join(':'); }
function renderSegments() {
  const query = $('search').value.trim().toLocaleLowerCase(); const segments = result.segments.filter(s => !query || s.text.toLocaleLowerCase().includes(query));
  $('matches').textContent = query ? `找到 ${segments.length} 段匹配内容` : `共 ${segments.length} 段 · 保留原始识别文本`;
  const fragment = document.createDocumentFragment();
  for (const s of segments) { const div = document.createElement('div'); div.className = 'segment'; const time = document.createElement('span'); time.className = 'stamp'; time.textContent = stamp(s.startMs); const p = document.createElement('p'); p.textContent = s.text; div.append(time,p); fragment.append(div); }
  $('segments').replaceChildren(fragment);
}
function render(data) { result = data; $('result').hidden = false; $('episode-title').textContent = data.meta.title; $('episode-meta').textContent = [data.meta.podcast, data.meta.duration ? Math.round(data.meta.duration/60) + ' 分钟' : null].filter(Boolean).join(' · '); $('original').href = data.meta.url; $('search').value = ''; renderSegments(); }
$('connect').addEventListener('click', startQR);
$('cancel-qr').addEventListener('click', () => {stopPolling(); $('qr-panel').hidden = true; $('qr-image').removeAttribute('src');});
$('logout').addEventListener('click', async () => { try { stopPolling(); await api('/api/auth/logout', {}); $('qr-panel').hidden = true; $('qr-image').removeAttribute('src'); $('result').hidden = true; $('segments').replaceChildren(); result = null; await session(); message('已清除本服务保存的登录凭证和文稿。'); } catch (e) {message(e.message,true);} });
$('episode-form').addEventListener('submit', async e => { e.preventDefault(); $('fetch').disabled = true; $('fetch').textContent = '正在获取…'; message('正在读取单集信息与平台逐字稿，请稍候。'); try { const data = await api('/api/transcripts', {url:$('episode-url').value}); render(data); message('逐字稿已获取，可阅读、搜索或下载。'); $('result').scrollIntoView({behavior:'smooth',block:'start'}); } catch (error) {message(error.message,true);} finally {$('fetch').disabled = !connected; $('fetch').textContent = '获取逐字稿 →';} });
$('search').addEventListener('input', renderSegments);
$('copy').addEventListener('click', async () => {try {await navigator.clipboard.writeText(result.meta.title + '\n\n' + result.segments.map(s => `[${stamp(s.startMs)}] ${s.text}`).join('\n\n')); message('全文已复制。');} catch {message('浏览器不支持复制，请下载 TXT 文件。',true);} });
window.addEventListener('pagehide', stopPolling);
session().then(async data => {if(data.lastEpisode) render(await api('/api/transcripts/latest'));}).catch(e => message(e.message,true));
