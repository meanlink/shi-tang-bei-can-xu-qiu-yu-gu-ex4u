import {jobs,dispatches} from '../api/entities.js';
import {workspace} from '../stores/workspace.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {SimulationMode} from '../constants/enums.js';
import {boardSection,bindBoard} from '../components/common/dispatch.js';
import {esc,notify} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';

const openStates = ['pending_send', 'awaiting_verification', 'cancelling'];

export async function dispatchPage() {
  const [list, allJobs] = await Promise.all([dispatches.list(), jobs.list()]);
  workspace.dispatches = list;
  const admin = session.user.role === 'admin';
  const approved = allJobs.filter(j => j.approved && j.status === 'completed');
  const filter = workspace.dispatchFilter || 'all';
  const filtered = filter === 'all' ? list : filter === 'open' ? list.filter(d => openStates.includes(d.status)) : list.filter(d => d.status === filter);
  const counts = {open: list.filter(d => openStates.includes(d.status)).length, manual: list.filter(d => d.status === 'manual').length, verification: list.filter(d => d.status === 'awaiting_verification').length};
  return heading('05 / TWO-PARTY DISPATCH', '备餐单两端派发', '一张已批准备餐单同时交给中央厨房和配送站。逐日份数与单号创建即冻结；两端都接单才算落实，超时先查对端实情。', '<button class="button" id="refresh-dispatch">刷新对账 ↻</button>') +
    '<div class="dispatch-summary"><div><span>进行中</span><strong>' + counts.open + '</strong></div><div><span>待查证</span><strong>' + counts.verification + '</strong></div><div><span>待人工</span><strong>' + counts.manual + '</strong></div><div><span>已落实</span><strong>' + list.filter(d => d.status === 'fulfilled').length + '</strong></div><div><span>已撤单</span><strong>' + list.filter(d => d.status === 'cancelled').length + '</strong></div></div>' +
    '<div class="report-grid"><section class="panel"><div class="section-title"><h2>创建两端派发</h2></div><form id="dispatch-form">' +
      '<label>派发标题<input name="title" required minlength="2" value="午餐备餐单 · 两端派发"></label>' +
      '<label>已批准分析<select name="job_id" required>' + approved.map(j => '<option value="' + j.id + '">' + esc(j.name) + '</option>').join('') + '</select></label>' +
      '<div class="parameter-grid"><label>演练对象<select name="party"><option value="kitchen">中央厨房</option><option value="station">配送站</option></select></label>' +
      '<label>演练场景<select name="mode">' + Object.entries(SimulationMode).map(([k, v]) => '<option value="' + k + '">' + esc(v) + '</option>').join('') + '</select></label></div>' +
      '<p class="hint">创建时冻结逐日份数与单号。正常模式下 worker 自动发送两端；演练模式只作用于所选一端，另一端保持正常。演练单不后台轮询，查证与撤回由卡片上的按钮逐步推进，便于观察对账与责任轨迹。</p>' +
      '<button type="submit" class="primary" ' + (!admin || !approved.length ? 'disabled' : '') + '>生成派发单并冻结</button>' +
      '<p class="hint">仅负责人可派发、撤单、重试查证与记录人工处置。分析员和观察员可查看两端凭据。</p></form></section>' +
    '<section>' + boardSection(filtered, admin, filter) + '</section></div>';
}

export function bindDispatch(refresh) {
  document.querySelector('#refresh-dispatch').onclick = refresh;
  const ctx = {
    refresh,
    setFilter(f) {workspace.dispatchFilter = f; refresh();},
    events: id => dispatches.events(id),
    verify: id => dispatches.verify(id),
    cancel: (id, reason) => dispatches.cancel(id, reason),
    manual: (id, note) => dispatches.manual(id, note),
  };
  bindBoard(refresh, ctx);
  const form = document.querySelector('#dispatch-form');
  if (form) submit(form, async f => {
    const mode = f.get('mode');
    const party = f.get('party');
    await dispatches.create({
      job_id: Number(f.get('job_id')),
      title: f.get('title'),
      is_drill: mode !== 'normal',
      simulation: mode === 'normal' ? {} : {[party]: mode},
      idempotency_key: 'ui-' + Date.now() + '-' + Math.random().toString(36).slice(2, 8),
    });
    notify('派发单已冻结，两端将按单号派发');
    await refresh();
  });
}
