// Activity configuration belongs to local user data, never to the installation.
const eventStyles = document.createElement('style');
eventStyles.textContent = `
  .event-modal { width:min(680px,100%); }
  .event-modal .form-grid { grid-template-columns:repeat(2,minmax(0,1fr)); }
  .event-modal .form-grid > label { min-width:0; color:var(--muted); }
  .event-modal input { min-width:0; max-width:100%; width:100%; }
  .event-modal input[type=color] { height:42px; padding:4px; }
  .event-modal input[type=file] { font-size:12px; padding:10px 0; }
  .event-modal label > label { display:flex; align-items:center; color:var(--muted); }
  .event-modal input[type=checkbox] { width:auto; margin:0 6px 0 0; }
  .event-modal .modal-actions { flex-wrap:wrap; }
  @media(max-width:580px) { .event-modal .form-grid {grid-template-columns:1fr;} }
`;
document.head.appendChild(eventStyles);
let currentEventId = '';
let eventDraft = null;
const baseBrand = JSON.parse(JSON.stringify(brand));
const baseAssets = { ...brandAssets };
const originalFetch = window.fetch.bind(window);
window.fetch = async function(input, options = {}) {
  const url = new URL(typeof input === 'string' ? input : input.url, location.href);
  if (url.origin === location.origin && options.method === 'POST') {
    const headers = new Headers(options.headers);
    headers.set('X-Event-Id', currentEventId);
    options = { ...options, headers };
  }
  const response = await originalFetch(input, options);
  if (url.origin === location.origin && url.pathname === '/api/state' && response.ok) {
    const next = await response.clone().json();
    const event = next.event;
    if (event) {
      if (currentEventId && currentEventId !== event.id) {
        modal = ''; pendingConfirm = null; screenConfirm = null; eventDraft = null;
        prizeId = ''; winnerCount = 1; search = ''; filter = 'all';
        screenResult = null; screenLastPending = null; screenPending = '';
        stopScreenV2Roll();
        notice = '已切换到：' + event.name;
      }
      currentEventId = event.id;
      Object.assign(brand, baseBrand, {eventName:event.name, eventMeta:event.date, screenTagline:event.tagline});
      brand.theme = {...baseBrand.theme, navy:event.accent, navyDeep:event.accent, accent:event.accent, coral:event.accent};
      Object.assign(brandAssets, baseAssets);
      if (event.logo) brandAssets.logo = event.logo;
      if (event.background) brandAssets.screenBackground = `url('${event.background}') center / cover no-repeat`;
      applyBrandTheme();
    }
  }
  return response;
};
const legacySignature = syncSignature;
syncSignature = next => JSON.stringify([next.event, legacySignature(next)]);

const priorAdmin = admin;
admin = function() {
  priorAdmin();
  const nav = document.querySelector('.nav');
  if (nav) nav.insertAdjacentHTML('beforeend', '<button onclick="manageEvents()">活动管理</button>');
  const footer = document.querySelector('.side-foot');
  if (footer && window.desktopUpdates?.available) {
    footer.insertAdjacentHTML('afterbegin', '<button class="lock" onclick="checkAppUpdate()">检查软件更新</button><br>');
  }
};

async function checkAppUpdate() {
  try { await window.desktopUpdates.check(); } catch(e) { showError(e); }
}
async function manageEvents() {
  try {
    const response = await fetch('/api/events', {headers:{'X-Admin-Token':token}});
    const data = await response.json();
    if (!response.ok) throw Error(data.error);
    eventDraft = {events:data.events, active:data.active};
    modal = 'events'; render();
  } catch(e) { showError(e); }
}
function editEvent(create) {
  eventDraft.edit = create ? {name:'', date:'', tagline:'欢迎参与现场抽奖', accent:'#2563eb'} : {...eventDraft.active};
  eventDraft.create = create;
  eventDraft.images = {};
  modal = 'eventEdit'; render();
}
function chooseEvent(id) {
  requestConfirm('切换活动后，大屏也会跟随切换。当前活动的数据会保留。', async () => {
    try { await post('/api/events/switch', {id}); lastStateSignature=''; await load(); }
    catch(e) { showError(e); }
  });
}
async function readEventImage(input, key) {
  const file = input.files[0];
  if (!file) return;
  if (!['image/png','image/jpeg','image/webp'].includes(file.type) || file.size > 4*1024*1024) {
    input.value=''; showNotice('请选择 4 MB 以内的 PNG、JPG 或 WebP 图片'); return;
  }
  const draft = eventDraft;
  draft.reading = (draft.reading || 0) + 1;
  try {
    draft.images[key] = await new Promise((resolve,reject) => {
      const reader = new FileReader(); reader.onload=()=>resolve(reader.result); reader.onerror=reject; reader.readAsDataURL(file);
    });
  } finally { draft.reading--; }
}
async function saveEvent(ev) {
  ev.preventDefault();
  if (eventDraft.reading) return;
  const button = ev.target.querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    const fields = Object.fromEntries(new FormData(ev.target));
    delete fields.logo; delete fields.background;
    const create = eventDraft.create;
    const result = await post('/api/events/' + (create?'create':'update'), {
      ...fields, ...eventDraft.images, id:eventDraft.edit.id,
    });
    if (create) await post('/api/events/switch', {id:result.id});
    modal=''; lastStateSignature=''; await load(); showNotice(create?'活动已创建并切换':'活动配置已保存');
  } catch(e) { showError(e); } finally { button.disabled=false; }
}
const priorModal = modalHtml;
modalHtml = function() {
  if (modal==='events' && eventDraft) return `<div class="modal-bg open"><div class="modal"><h2>活动管理</h2><p>每个活动单独保存名单、奖品和中奖记录。</p><div style="max-height:340px;overflow:auto">${eventDraft.events.map(e=>`<div class="activity-row" style="display:flex;gap:16px;justify-content:space-between;align-items:center"><strong>${esc(e.name)}</strong>${e.id===eventDraft.active.id?'<span>当前活动</span>':`<button class="btn light" onclick="chooseEvent('${e.id}')">切换</button>`}</div>`).join('')}</div><div class="modal-actions"><button class="btn light" onclick="closeModal()">关闭</button><button class="btn light" onclick="editEvent(false)">编辑当前活动</button><button class="btn primary" onclick="editEvent(true)">新建活动</button></div></div></div>`;
  if (modal==='eventEdit' && eventDraft) {
    const e=eventDraft.edit;
    return `<div class="modal-bg open"><form class="modal" style="max-height:90vh;overflow:auto" onsubmit="saveEvent(event)"><h2>${eventDraft.create?'新建活动':'活动配置'}</h2><div class="form-grid"><label>活动名称<input class="input" name="name" maxlength="80" value="${esc(e.name)}" required></label><label>日期／副标题<input class="input" name="date" maxlength="80" value="${esc(e.date)}"></label><label>大屏欢迎语<input class="input" name="tagline" maxlength="120" value="${esc(e.tagline)}"></label><label>主题颜色<input class="input" type="color" name="accent" value="${esc(e.accent)}"></label><label>Logo（可稍后设置）<input type="file" accept="image/png,image/jpeg,image/webp" onchange="readEventImage(this,'logo')"><label><input type="checkbox" onchange="eventDraft.images.logo=this.checked?'':undefined">恢复默认 Logo</label></label><label>大屏背景（可稍后设置）<input type="file" accept="image/png,image/jpeg,image/webp" onchange="readEventImage(this,'background')"><label><input type="checkbox" onchange="eventDraft.images.background=this.checked?'':undefined">恢复默认背景</label></label></div><p style="color:var(--muted)">图片支持 PNG、JPG、WebP，每张不超过 4 MB。新活动名单为空，奖品可在后台调整。</p><div class="modal-actions"><button type="button" class="btn light" onclick="manageEvents()">返回</button><button type="submit" class="btn primary">保存活动</button></div></form></div>`;
  }
  return priorModal();
};
const unstyledEventModal = modalHtml;
modalHtml = function() {
  const html = unstyledEventModal();
  return (modal==='events' || modal==='eventEdit') ? html.replace('class="modal"', 'class="modal event-modal"') : html;
};
// Keep polling after login without rebuilding the password form while typing.
boot = async function() {
  if (!screenMode && token) {
    try {
      const auth = await fetch('/api/auth', {headers:{'X-Admin-Token':token}}).then(r=>r.json());
      if (!auth.ok) { token=''; localStorage.removeItem('lucky-draw-admin-token'); }
    } catch (_) {}
  }
  if (screenMode || token) load(); else renderLogin();
  setInterval(()=>{ if(screenMode || token) load(); },400);
};
boot();
