import {DispatchStatus, LegStatus, PartyLabel} from '../../constants/enums.js';
import {esc,date,notify,refreshSoon} from '../../utils/dom.js';

const openStates = ['pending_send', 'awaiting_verification', 'cancelling'];
const cancelableStates = ['pending_send', 'awaiting_verification', 'cancelling', 'fulfilled'];
const filters = [
  ['all', '全部'],
  ['open', '进行中'],
  ['pending_send', '待发送'],
  ['awaiting_verification', '待查证'],
  ['fulfilled', '已落实'],
  ['cancelling', '撤单中'],
  ['cancelled', '已撤单'],
  ['manual', '待人工'],
];

export function legPill(leg) {
  return '<span class="status leg ' + esc(leg.status) + '">' + esc(PartyLabel[leg.party] || leg.party) + ' · ' + esc(LegStatus[leg.status] || leg.status) + '</span>';
}

function legBlock(leg, admin) {
  const e = leg.evidence || [];
  return '<div class="leg"><div class="leg-head">' + legPill(leg) + '<small>对端单号 ' + esc(leg.order_id) + '</small></div>' +
    '<p class="leg-meta">发送 ' + leg.send_count + ' 次 · 查证 ' + leg.verify_count + ' 次 · 撤回 ' + leg.cancel_count + ' 次' +
    (leg.last_error ? '<br><span class="leg-error">' + esc(leg.last_error) + '</span>' : '') +
    (leg.partner ? '<br>对端实情：' + esc(LegStatus[leg.partner.status] || leg.partner.status) + ' · 版本 ' + leg.partner.revision + (leg.partner.reason ? ' · ' + esc(leg.partner.reason) : '') : '<br>对端实情：尚未收到') +
    '</p><div class="evidence">' + (e.length ? e.slice(-4).reverse().map(x => '<div><small>' + esc(date(x.at)) + ' · ' + esc(x.source) + (x.status ? ' · ' + esc(LegStatus[x.status] || x.status) : '') + '</small><p>' + esc(x.note || '') + '</p></div>').join('') : '<small class="empty-note">尚无凭据</small>') + '</div></div>';
}

function dispatchCard(d, admin) {
  const open = openStates.includes(d.status);
  return '<article class="dispatch-card ' + esc(d.status) + '" data-dispatch="' + d.id + '">' +
    '<div class="dispatch-head"><div><strong>' + esc(d.title) + '</strong><p class="ticket">单号 ' + esc(d.ticket_no) + ' · ' + (d.is_drill ? '<span class="drill">演练</span>' : '备餐单') + ' · ' + d.items.length + ' 天 · 合计 ' + d.total_portions + ' 份</p></div><span class="status ' + esc(d.status) + '">' + esc(DispatchStatus[d.status] || d.status) + '</span></div>' +
    '<div class="legs">' + d.legs.map(l => legBlock(l, admin)).join('') + '</div>' +
    '<div class="dispatch-foot"><small>来源 ' + esc(d.job_name) + ' · 快照 SHA256 ' + esc(d.snapshot_checksum.slice(0, 20)) + '…</small></div>' +
    (d.cancel_requested ? '<p class="cancel-note">撤单请求：' + esc(d.cancel_requested_by || '') + ' · ' + esc(d.cancel_reason || '') + (d.status === 'cancelled' ? ' · 两端均已确认撤回' : '') + '</p>' : '') +
    (d.status === 'manual' ? '<p class="manual-note">' + (d.resolved_by ? '已由 ' + esc(d.resolved_by) + ' 记录处置：' + esc(d.resolution_note) : '一端已开工、另一端需撤回，请人工确认去向并记录结论。') + '</p>' : '') +
    '<div class="dispatch-actions">' +
      '<button class="text-button" data-items="' + d.id + '">查看逐日份数</button>' +
      '<button class="text-button" data-events="' + d.id + '">责任轨迹</button>' +
      ((d.status === 'awaiting_verification' || (open && d.legs.some(l => l.status === 'unknown'))) && admin ? '<button class="text-button" data-verify="' + d.id + '">重试查证 ↻</button>' : '') +
      (cancelableStates.includes(d.status) && admin ? '<button class="text-button danger" data-cancel="' + d.id + '">撤单</button>' : '') +
      (d.status === 'manual' && !d.resolved_by && admin ? '<button class="text-button" data-manual="' + d.id + '">记录人工处置</button>' : '') +
    '</div>' +
    '<div class="dispatch-drawer" data-drawer="' + d.id + '" hidden><div class="items">' + d.items.map(i => '<div><span>' + esc(i.date) + ' ' + esc(i.weekday || '') + '</span><strong>' + esc(i.portions) + ' 份</strong><small>预测 ' + esc(i.forecast ?? '—') + ' · 缓冲 ' + esc(i.buffer ?? '—') + '</small></div>').join('') + '</div></div>' +
  '</article>';
}

export function boardSection(list, admin, selectedMode) {
  const counts = {total: list.length};
  for (const f of filters) counts[f[0]] = f[0] === 'all' ? list.length : f[0] === 'open' ? list.filter(d => openStates.includes(d.status)).length : list.filter(d => d.status === f[0]).length;
  return '<section class="board"><div class="section-title"><h2>两端派发与对账</h2><span>中央厨房 + 配送站 · 两端都接单才算落实</span></div>' +
    '<div class="board-toolbar"><div class="filters">' + filters.map(f => '<button class="' + (selectedMode === f[0] ? 'active' : '') + '" data-filter="' + f[0] + '">' + f[1] + ' <small>' + counts[f[0]] + '</small></button>').join('') + '</div>' +
    '<p class="hint board-hint">演练场景在左侧创建表单中选择一端与场景；进行中的“待查证”单可用卡片上的“重试查证”推进。超时只代表未知，不等于拒单。</p></div>' +
    '<div class="dispatch-list">' + (list.length ? list.map(d => dispatchCard(d, admin)).join('') : '<p class="empty">尚无派发单。从上方已批准结果创建一张，并选择演练模式。</p>') + '</div>' +
  '</section>';
}

export function bindBoard(refresh, ctx) {
  document.querySelectorAll('[data-filter]').forEach(b => b.onclick = () => ctx.setFilter(b.dataset.filter));
  document.querySelectorAll('[data-items]').forEach(b => b.onclick = () => {const el = document.querySelector('[data-drawer="' + b.dataset.items + '"]'); if (el) el.hidden = !el.hidden;});
  document.querySelectorAll('[data-events]').forEach(b => b.onclick = async () => {
    try {
      const ev = await ctx.events(b.dataset.events);
      const text = ev.slice(-6).reverse().map(e => new Date(e.at * 1000).toLocaleTimeString('zh-CN', {hour12: false}) + ' ' + e.actor + ' ' + e.action).join('\n');
      alert(text || '暂无责任轨迹');
    } catch (e) {notify(e.message, true);}
  });
  document.querySelectorAll('[data-verify]').forEach(b => b.onclick = async () => {
    b.disabled = true;
    try {const d = await ctx.verify(b.dataset.verify); notify('已向对端查证：' + dispatchStatusText(d.status)); refreshSoon(ctx.refresh);}
    catch (e) {notify(e.message, true); b.disabled = false;}
  });
  document.querySelectorAll('[data-cancel]').forEach(b => b.onclick = async () => {
    const reason = prompt('撤回该派发单？两端都会尝试撤回；已开工的一端将转人工。请填写原因：', '计划变更');
    if (reason === null) return;
    try {await ctx.cancel(b.dataset.cancel, reason || '计划变更'); notify('已提交撤单，正在进行两端对账'); refreshSoon(ctx.refresh);}
    catch (e) {notify(e.message, true);}
  });
  document.querySelectorAll('[data-manual]').forEach(b => b.onclick = async () => {
    const note = prompt('记录人工处置结论与责任去向：', '与厨房确认已开工份数，配送站撤回完成，损耗由计划组承担');
    if (!note) return;
    try {await ctx.manual(b.dataset.manual, note); notify('人工处置已记录'); refreshSoon(ctx.refresh);}
    catch (e) {notify(e.message, true);}
  });
}

export function dispatchStatusText(s) { return DispatchStatus[s] || s; }
