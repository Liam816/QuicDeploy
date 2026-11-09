import './style.css';
import { Workspace } from './scene.js';

document.querySelector('#app').innerHTML = `
  <header class="header">
    <a class="brand" href="/" aria-label="QuicDeploy 可视化首页"><span class="brand-mark">Q</span><span>QuicDeploy<span class="brand-sub">现场可视化 / PICK2BOX</span></span></a>
    <div class="header-right"><span id="source" class="tag">只读遥测</span><span id="connection" class="connection" role="status">连接中</span></div>
  </header>
  <main>
    <section class="intro"><div><p class="eyebrow">DEPLOYMENT OBSERVATORY</p><h1>看见每一次预测<span class="heading-dot">.</span></h1><p id="model" class="muted">等待推理进程接入。原有终端负责机械臂操作。</p></div>
      <div class="session"><div class="session-state"><span class="muted">会话状态</span><strong id="status">等待数据</strong><span id="mode" class="mono">—</span></div>
        <div class="inference-metrics" aria-label="实时推理性能">
          <div title="从提交推理请求到主进程收到完整结果，包含 IPC、模型调用及轮询等待"><span>完整推理 latency</span><strong id="inference-latency" class="mono">— ms</strong></div>
          <div title="最近 5 秒完成的完整推理请求数除以 5，包含调度空闲时间"><span>实测频率 · 近5秒</span><strong id="inference-frequency" class="mono">— Hz</strong></div>
        </div><p id="inference-detail" class="mono">单次等效 — Hz · 模型调用 — ms</p>
      </div></section>
    <div id="banner" class="banner" role="status" hidden></div>
    <section class="dashboard" aria-label="相机与三维轨迹">
      <aside class="camera-column"><div class="section-title"><h2>相机画面</h2><span class="mono muted">03 VIEWS</span></div>
        ${[['head', '头部相机', '01'], ['left_wrist', '左腕相机', '02'], ['right_wrist', '右腕相机', '03']].map(([name, label, index]) => `
          <article class="camera-card" id="camera-${name}"><div class="camera-top"><span><span class="camera-index mono">${index}</span>${label}</span><span class="camera-state" id="state-${name}">等待画面</span></div>
            <div class="camera-image"><div class="camera-empty">等待 ${name} 图像流</div><img id="image-${name}" alt="${label}实时画面" src="/api/camera/${name}"></div>
            <div class="camera-bottom mono"><span id="size-${name}">— × —</span><span id="age-${name}">— ms</span></div></article>`).join('')}
      </aside>
      <div class="workspace-column">
        <section class="workspace-panel"><div class="workspace-toolbar"><div><span class="eyebrow">WORLD FRAME · METERS</span><h2>双臂预测空间</h2></div>
          <div class="toolbar-actions"><button id="home" title="恢复默认视角">复位视角</button><button id="top">俯视</button><button id="side">侧视</button><button id="fullscreen">全屏</button></div></div>
          <div class="scene-wrap" id="scene-wrap"><div id="scene"></div><div class="scene-label"><span class="live-dot"></span><span id="scene-caption">等待第一帧预测</span></div>
            <div class="legend"><span><i class="line left"></i>左臂</span><span><i class="line right"></i>右臂</span><span><i class="line previous"></i>上一帧 / 虚线</span></div>
            <div class="scene-guide">拖动旋转 · 右键平移 · 滚轮缩放<span>X 红 / Y 绿 / Z 蓝 · Z 向上</span></div></div>
          <div class="robot-status"><span class="tiny-square"></span><span id="robot-status">正在加载 URDF…</span></div>
        </section>
        <section class="prediction-panel" aria-label="预测轨迹检查"><div class="prediction-header"><div><p class="eyebrow">PREDICTION INSPECTOR</p><h2>当前与上一帧</h2></div><button id="freeze" aria-pressed="false">冻结轨迹</button></div>
          <div class="metrics"><div><span>当前预测</span><strong id="current-step" class="mono">—</strong></div><div><span>上一帧预测</span><strong id="previous-step" class="mono">—</strong></div><div><span>推理往返</span><strong id="latency" class="mono">—<small>ms</small></strong></div><div><span>执行 / 预测行数</span><strong id="rows" class="mono">— / —</strong></div></div>
          <div class="controls"><label class="select-label">轨迹类型<select id="trajectory"><option value="u">U · 模型原始预测</option><option value="e">E · 实际执行轨迹</option></select></label><label class="check"><input id="previous" type="checkbox" checked>显示上一帧</label><label class="check"><input id="robot" type="checkbox" checked>显示机械臂</label><label class="check"><input id="follow" type="checkbox" checked>跟随执行时间</label></div>
          <div class="scrubber"><label for="step">预测步</label><input id="step" type="range" min="0" max="0" value="0" aria-label="选择预测步"><output id="step-label" class="mono">— / —</output></div>
          <div class="coordinate-row"><div><span class="arm-label left">L</span><span id="left-position" class="mono">X — &nbsp; Y — &nbsp; Z —</span></div><div><span class="arm-label right">R</span><span id="right-position" class="mono">X — &nbsp; Y — &nbsp; Z —</span></div></div>
          <p class="inspector-note" id="trajectory-note">U 为观测锚点解码后的 world 绝对位姿；E 为裁剪与融合后交给控制环的轨迹。</p>
        </section>
      </div>
    </section>
    <footer><span>READ-ONLY TELEMETRY<span class="footer-separator">/</span>图像为实时预览；轨迹按每次推理结果更新。</span><span>URDF 预测姿态由浏览器 IK 近似重建，不代表最终关节指令。</span></footer>
  </main>`;

const $ = id => document.getElementById(id);
const put = (id, value) => { $(id).textContent = value; };
const cameraNames = ['head', 'left_wrist', 'right_wrist'];
let latest = null, shown = null, frozen = false, lastReceive = 0, lastRevision = -1;
let workspace;
try {
  workspace = new Workspace($('scene'), message => put('robot-status', message));
  fetch('/api/config').then(response => {
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }).then(config => workspace.loadRobot(config)).catch(error => put('robot-status', `配置读取失败：${error.message}`));
} catch (error) {
  put('robot-status', `3D 初始化失败：${error.message}`);
  const message = document.createElement('p');
  message.className = 'webgl-error';
  message.textContent = '无法初始化 WebGL。请开启浏览器硬件加速后刷新；相机与数据仍可查看。';
  $('scene').appendChild(message);
}

const options = () => ({ trajectory: $('trajectory').value, index: Number($('step').value), previous: $('previous').checked });

function render() {
  if (!shown) return;
  const { current, previous } = shown;
  const key = $('trajectory').value;
  const trajectory = current?.[key];
  const rows = trajectory?.poses ?? [];
  const max = Math.max(0, rows.length - 1);
  $('step').max = max;
  if ($('follow').checked && rows.length && !frozen) {
    const t = shown.server_monotonic;
    const nearest = trajectory.times.reduce((best, value, i) => Math.abs(value - t) < Math.abs(trajectory.times[best] - t) ? i : best, 0);
    $('step').value = nearest;
  }
  const index = Math.min(max, Number($('step').value));
  $('step').value = index;
  put('current-step', current ? `#${String(current.step).padStart(4, '0')}` : '—');
  put('previous-step', previous ? `#${String(previous.step).padStart(4, '0')}` : '首帧');
  put('latency', current ? `${Math.round(current.roundtrip_ms)} ms` : '— ms');
  put('rows', current ? `${current.e.poses.length} / ${current.u.poses.length}` : '— / —');
  put('step-label', rows.length ? `${index + 1} / ${rows.length}` : '— / —');
  put('scene-caption', current ? `${frozen ? '已冻结 · ' : ''}预测 #${current.step} · ${key.toUpperCase()} · 第 ${index + 1} 步` : '等待第一帧预测');
  for (const [side, base] of [['left', 0], ['right', 10]]) {
    const row = rows[index];
    put(`${side}-position`, row ? `X ${row[base].toFixed(3)}   Y ${row[base + 1].toFixed(3)}   Z ${row[base + 2].toFixed(3)} m  ·  夹爪 ${row[base + 9].toFixed(1)} mm` : 'X —   Y —   Z —');
  }
  put('trajectory-note', current ?
    `U：${current.u.poses.length} 行 · E：${current.e.poses.length} 行 · 过期裁剪 ${current.expired_steps} 行 · 融合 ${current.fused_steps} 行 · RTC 前缀 ${current.prefix_source_rows.length} 行。上一帧位姿按同一绝对时间匹配，超出覆盖范围取边界。` :
    'U 为观测锚点解码后的 world 绝对位姿；E 为裁剪与融合后交给控制环的轨迹。');
  workspace?.update(current, previous, shown.robot, options());
}

function updateHealth() {
  const connectionStale = Date.now() - lastReceive > 3000;
  const age = latest?.received_age_s;
  const stale = connectionStale || age === null || age > 2;
  const finished = latest?.finished;
  const source = latest?.source;
  const offline = source === 'demo' || source === 'replay';
  $('connection').classList.toggle('connected', !connectionStale && !!latest);
  put('connection', connectionStale ? '连接中断 · 正在重连' : '遥测已连接');
  put('source', source === 'demo' ? '模拟数据 · 无硬件' : source === 'replay' ? '离线回放 · 无硬件' : '只读遥测');
  const statusNames = { waiting: '等待推理', running: '运行中', predicting: '推理中', holding: '保持', paused: '已暂停', passed: '已结束', failed: '异常停止', interrupted: '已中止', offline: '离线预览' };
  put('status', latest ? (statusNames[latest.status] ?? latest.status) : '等待数据');
  put('mode', latest?.mode ? `${latest.mode.toUpperCase()} · 动作目标 ${latest.policy_hz ?? '—'} Hz` : '—');
  const inference = latest?.inference;
  const metric = (value, places) => Number.isFinite(value) ? value.toFixed(places) : '—';
  put('inference-latency', `${metric(inference?.latency_ms, 1)} ms`);
  put('inference-frequency', `${metric(stale || finished ? null : inference?.frequency_hz, 2)} Hz`);
  put('inference-detail', `单次等效 ${metric(inference?.latency_equivalent_hz, 2)} Hz · 模型调用 ${metric(inference?.model_ms, 1)} ms${finished ? ' · 最后一次' : ''}`);
  if (latest?.model) put('model', latest.model);
  const banner = $('banner');
  const message = latest?.error ? `会话错误：${latest.error}` : finished ? '推理已结束，当前显示最后一份快照。' : offline ?
    (source === 'demo' ? '当前为合成数据，用于检查界面与 3D 显示。未读取相机或控制机械臂。' : '当前为诊断文件回放，图像来自推理观测快照。') :
    stale ? '等待推理数据；启动原有推理指令后自动接入。过期画面会保留并标记。' : '';
  banner.hidden = !message;
  banner.textContent = message;
  for (const name of cameraNames) {
    const camera = latest?.cameras?.[name];
    const cameraAge = camera ? Math.max(0, latest.server_monotonic - camera.timestamp + (Date.now() - lastReceive) / 1000) : Infinity;
    const cameraStale = stale || finished || cameraAge > 1;
    $(`camera-${name}`).classList.toggle('stale', !!cameraStale);
    put(`state-${name}`, !camera ? '等待画面' : finished ? '最后一帧' : cameraStale ? '画面过期' : offline ? '离线画面' : '实时');
    put(`size-${name}`, camera ? `${camera.width} × ${camera.height} / ≤${camera.preview_fps ?? '—'} fps` : '— × —');
    put(`age-${name}`, camera ? `${Math.round(cameraAge * 1000)} ms` : '— ms');
  }
}

for (const name of cameraNames) {
  const image = $(`image-${name}`);
  image.addEventListener('load', () => image.parentElement.classList.add('has-image'));
  image.addEventListener('error', () => setTimeout(() => { image.src = `/api/camera/${name}?retry=${Date.now()}`; }, 2000));
}
const events = new EventSource('/api/events');
events.onmessage = event => {
  try {
    const packet = JSON.parse(event.data);
    latest = packet;
    lastReceive = Date.now();
    if (!frozen && packet.revision !== lastRevision) {
      if (shown?.session_id !== packet.session_id) $('step').value = 0;
      shown = packet;
      render();
    }
    lastRevision = packet.revision;
    updateHealth();
  } catch (error) {
    put('robot-status', `遥测数据处理失败：${error.message}`);
  }
};
events.onerror = () => { lastReceive = 0; updateHealth(); };
setInterval(updateHealth, 500);
$('freeze').addEventListener('click', () => {
  frozen = !frozen;
  $('freeze').setAttribute('aria-pressed', String(frozen));
  $('freeze').textContent = frozen ? '恢复实时轨迹' : '冻结轨迹';
  if (!frozen) shown = latest;
  render();
});
$('trajectory').addEventListener('change', () => { $('step').value = 0; render(); });
$('previous').addEventListener('change', render);
$('follow').addEventListener('change', render);
$('step').addEventListener('input', () => { $('follow').checked = false; render(); });
$('robot').addEventListener('change', () => workspace?.setRobotVisible($('robot').checked));
for (const name of ['home', 'top', 'side']) $(name).addEventListener('click', () => workspace?.view(name));
$('fullscreen').addEventListener('click', async () => {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await $('scene-wrap').requestFullscreen();
  } catch { put('robot-status', '浏览器不支持当前全屏请求'); }
});
