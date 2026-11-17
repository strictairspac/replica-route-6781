import {table} from '../components/common/table.js';
import {esc,date} from '../utils/html.js';
import {session} from '../hooks/session.js';
import {topologyApi} from '../api/topology.js';
import {feedback,flash} from '../hooks/feedback.js';

const STATUS_LABEL={draft:'草稿',applying:'切换中',applied:'已生效',failed:'失败',cancelled:'已取消'};
const ROLE_LABEL={primary:'主库',replica:'副本'};
const STATE_LABEL={draft:'badge',applying:'badge running',applied:'badge completed',failed:'badge failed',cancelled:'badge'};
const NODE_FIELDS=['name','role','status','lineage','position','delay_ms','note'];

const nodeRow=n=>`<tr class="node-row" data-id="${n?.id??''}" data-base="${esc(JSON.stringify(n??null))}">
<td><input data-field="name" value="${esc(n?.name??'')}" minlength="2" maxlength="80" required></td>
<td><select data-field="role"><option value="replica" ${n?.role==='replica'?'selected':''}>replica</option><option value="primary" ${n?.role==='primary'?'selected':''}>primary</option></select></td>
<td><select data-field="status"><option value="healthy" ${n?.status!=='down'?'selected':''}>healthy</option><option value="down" ${n?.status==='down'?'selected':''}>down</option></select></td>
<td><input data-field="lineage" value="${esc(n?.lineage??'qiyuan-log-01')}" maxlength="80" required></td>
<td><input data-field="position" type="number" min="0" value="${n?.position??105}" required></td>
<td><input data-field="delay_ms" type="number" min="0" value="${n?.delay_ms??20}" required></td>
<td><input data-field="note" value="${esc(n?.note??'')}" maxlength="300"></td>
<td><button type="button" class="small remove-row" ${session.user.role==='viewer'?'disabled':''}>移除</button></td></tr>`;

function conflictPanel(change){
  const conflicts=change?.conflicts||[];
  if(!conflicts.length)return '';
  return `<section class="section-top"><h2>冲突双方依据</h2><span>${conflicts.length} CONFLICTS / ${change.unresolved_count} 未处理</span></section>
  <div class="conflict-list">${conflicts.map(c=>`<article class="conflict ${c.resolution?'resolved':''}" data-node="${esc(c.node)}">
    <header><strong>${esc(c.node)}</strong><span class="badge">${esc(c.kind_label)}</span><small>基准 v${esc(c.base_version_id??'?')} ／ 当前生效 v${esc(c.current_version??'?')} · 指纹 ${esc((c.current_checksum||'').slice(0,12))}</small></header>
    <div class="conflict-sides">
      <div><span class="eyebrow">草稿依据</span>${side(c.draft)}</div>
      <div><span class="eyebrow">当前生效依据</span>${side(c.current)}</div>
      <div><span class="eyebrow">原基准</span>${side(c.base)}</div>
    </div>
    <div class="conflict-actions">
      <label>处理说明<input name="note-${esc(c.node)}" value="${esc(c.resolution?.note??'')}" placeholder="记录选择理由（可选）"></label>
      <button type="button" class="small choose-conflict" data-node="${esc(c.node)}" data-choice="draft" ${session.user.role==='viewer'?'disabled':''}>采用草稿</button>
      <button type="button" class="small choose-conflict" data-node="${esc(c.node)}" data-choice="current" ${session.user.role==='viewer'?'disabled':''}>采用当前</button>
      ${c.resolution?`<span class="badge completed">已选：${c.resolution.choice==='draft'?'草稿':c.resolution.choice==='current'?'当前':'自定义'} · ${esc(c.resolution.actor||'')}</span>`:'<span class="badge failed">待处理</span>'}
    </div></article>`).join('')}</div>`;
}

function side(node){
  if(!node)return '<p class="muted">（已移除）</p>';
  return `<p>${esc(ROLE_LABEL[node.role]||node.role)} · ${esc(node.status)} · 位点 ${esc(node.position)} · 延迟 ${esc(node.delay_ms)}ms<br>谱系 ${esc(node.lineage)}${node.note?'<br>'+esc(node.note):''}</p>`;
}

function changeList(changes,selected){
  return changes.length?`<div class="change-list">${changes.map(c=>`<a class="change-item ${selected===c.id?'active':''} ${c.status}" href="#topology/${c.id}">
    <span class="${STATE_LABEL[c.status]||'badge'}">${STATUS_LABEL[c.status]||c.status}</span>
    <strong>${esc(c.name)}</strong>
    <small>#${c.id} · 基准 v${c.base_version?.version??'—'}${c.applied_version?' → v'+c.applied_version.version:''} · ${date(c.updated_at)}</small>
    ${c.unresolved_count?`<small class="error">${c.unresolved_count} 项冲突待处理</small>`:''}
  </a>`).join('')}</div>`:'<div class="empty">还没有整套拓扑变更。右侧以当前生效拓扑为基准创建草稿。</div>';
}

export function topologyPage({head,versions,changes,change,versionView}){
  if(versionView){
    return `<div class="split"><section>
      <div class="section-top"><h2>拓扑版本 v${versionView.version}</h2><span>${esc(versionView.source)} · ${esc(versionView.created_by)}</span></div>
      <p class="policy-strip">完整节点集 ${versionView.nodes.length} 个 · SHA256 ${esc(versionView.checksum)} · ${date(versionView.created_at)}</p>
      <div class="topology">${versionView.nodes.map(n=>`<div class="node ${n.role}"><span class="eyebrow">${n.role==='primary'?'PRIMARY / 主站':'REPLICA / 副本'}</span><strong>${esc(n.name)}</strong><div class="position">${n.position}<small>LOG POSITION</small></div><div>${n.delay_ms}ms · ${esc(n.status)} · ${esc(n.lineage)}</div></div>`).join('')}</div>
      ${table(['节点','角色 / 状态','位点 / 延迟','谱系 / 备注'],versionView.nodes.map(n=>[`<strong>${esc(n.name)}</strong>`,`${esc(n.role)} / ${esc(n.status)}`,`${n.position} / ${n.delay_ms} ms`,`${esc(n.lineage)}<small>${esc(n.note)}</small>`]))}
      <p><a href="#topology">← 返回整套拓扑变更</a></p></section>
      <section class="form-panel"><span class="eyebrow">IMMUTABLE VERSION</span><h2>版本只读</h2><p class="help">拓扑版本一经生成即冻结。已有试验引用该版本时始终重放这份完整节点集，不受后续换主影响。</p></section></div>`;
  }
  const editable=change?['draft','failed'].includes(change.status):false;
  const rows=(change?change.draft_nodes:head?.nodes||[]).map(nodeRow).join('');
  return `<div class="split"><section>
    <div class="section-top"><h2>当前生效拓扑</h2><span>HEAD v${head?.version??'—'} / ${head?.nodes?.length??0} NODES</span></div>
    <div class="topology">${(head?.nodes||[]).map(n=>`<div class="node ${n.role}"><span class="eyebrow">${n.role==='primary'?'PRIMARY / 主站':'REPLICA / 副本'}</span><strong>${esc(n.name)}</strong><div class="position">${n.position}<small>LOG POSITION</small></div><div>${n.delay_ms}ms · ${esc(n.status)} · v${head.version}</div></div>`).join('')}</div>
    <div class="notice">主副角色调整必须走整套变更：草稿在校验唯一主库、谱系与位点后，以单个事务切换到新版本。任何页面都不会看到无主或双主的中间态。</div>
    ${change?`<div class="section-top"><h2>变更草稿：${esc(change.name)}</h2><span><span class="${STATE_LABEL[change.status]}">${STATUS_LABEL[change.status]}</span> 修订 ${change.revision}</span></div>
      ${change.stale_base?`<div class="notice error">草稿基准 v${change.base_version?.version??'—'} 已落后于当前生效 v${head?.version}：期间已有修改生效。校验会保留双方依据，需要逐项处理冲突并确认后才能发布。</div>`:''}
      ${change.error?`<div class="error">${esc(change.error)}</div>`:''}
      <form id="topology-form">
        <input type="hidden" name="change_id" value="${change.id}">
        <input type="hidden" name="revision" value="${change.revision}">
        <label>变更名称<input name="name" value="${esc(change.name)}" maxlength="120" required ${editable?'':'disabled'}></label>
        <div class="table-wrap"><table><thead><tr><th>节点名</th><th>角色</th><th>状态</th><th>谱系</th><th>位点</th><th>延迟ms</th><th>备注</th><th></th></tr></thead>
        <tbody id="node-rows">${rows}</tbody></table></div>
        <div class="form-actions">
          <button type="button" class="small" id="add-row" ${editable&&session.user.role!=='viewer'?'':'disabled'}>+ 添加节点</button>
          <button type="button" class="small" id="validate-change" data-id="${change.id}">校验唯一主库 / 谱系 / 位点</button>
          <button type="button" class="small" id="cancel-change" data-id="${change.id}" ${['applied','cancelled'].includes(change.status)?'disabled':''}>取消变更</button>
        </div>
        ${change.validation_errors?.length?`<div class="error">${change.validation_errors.map(esc).join('<br>')}</div>`:''}
        <button class="primary" id="save-change" ${editable&&session.user.role!=='viewer'?'':'disabled'}>保存整套草稿（原子校验）</button>
      </form>
      ${conflictPanel(change)}
      <div class="publish-panel">
        <h3>发布并原子切换</h3>
        <label class="check"><input type="checkbox" id="confirm-stale" ${change.stale_base?'':'checked'} ${change.stale_base?'':'disabled'}> 我已核对冲突双方依据，确认基于当前拓扑合并</label>
        <button class="primary" id="publish-change" data-id="${change.id}" data-revision="${change.revision}" ${['applied','cancelled'].includes(change.status)||session.user.role==='viewer'?'disabled':''}>发布整套变更 → v${(head?.version??0)+1}</button>
        <p class="help">发布请求带幂等ID：网络中断后重试同一次发布不会重复生效。切换中断的记录会在恢复后沿用原记录，仍为草稿或补记为已生效。</p>
      </div>`:''}
    <div class="section-top"><h2>变更记录</h2><span>${changes.length} CHANGES</span></div>
    ${changeList(changes,change?.id)}
    <div class="section-top"><h2>生效版本历史</h2><span>IMMUTABLE SNAPSHOTS</span></div>
    ${table(['版本','来源 / 操作人','节点数','校验和','时间',''],versions.map(v=>[`<strong>v${v.version}</strong>`,`${esc(v.source)}<small>${esc(v.created_by)}</small>`,`${v.node_count}`,`<code>${esc(v.checksum.slice(0,16))}…</code>`,`${date(v.created_at)}`,`<a href="#topology-version/${v.id}">查看 →</a>`]))}
  </section>
  <section class="form-panel"><span class="eyebrow">WHOLE TOPOLOGY CHANGE</span><h2>${change?'当前草稿':'创建整套变更草稿'}</h2>
    <p class="help">以当前生效拓扑 v${head?.version??'—'} 为基准。草稿可反复调整节点与角色；发布前校验有且仅有一个主库，同谱系副本位点不高于主库。</p>
    ${change?`<p class="help">草稿 #${change.id} 创建于 ${date(change.created_at)}。修改草稿不会影响生效拓扑与正在运行的试验。</p><p><a href="#topology">新建另一套草稿</a></p>`:
    `<form id="topology-create">
      <label>变更名称<input name="name" value="交接班拓扑调整 ${new Date().toLocaleDateString('zh-CN')}" maxlength="120" required></label>
      <label>基准版本<input value="v${head?.version??'—'} · ${head?.nodes?.length??0} 个节点" disabled></label>
      <button class="primary" ${session.user.role==='viewer'?'disabled':''}>创建整体草稿 →</button>
    </form>
    <div class="notice">切换期间仍可继续编辑草稿、登记节点和发起试验。试验只冻结切换前或切换后的完整版本，不会读到中间态。</div>`}
    <div class="protocol"><h3>发布规则</h3><ol><li>草稿不能覆盖期间已生效的修改：三方比对保留草稿、当前、基准依据</li><li>必须处理全部冲突并确认基于当前拓扑合并</li><li>校验唯一主库、位点单调与谱系后单事务切换</li><li>并发发布只允许一个基于当前拓扑的方案胜出</li><li>中断或失败沿用原记录，同次重试不重复生效</li></ol></div>
  </section></div>`;
}

export function readDraftRows(){
  return [...document.querySelectorAll('#node-rows .node-row')].map(row=>{
    const node={};
    if(row.dataset.id)node.id=Number(row.dataset.id);
    for(const field of NODE_FIELDS){
      const input=row.querySelector('[data-field="'+field+'"]');
      node[field]=['position','delay_ms'].includes(field)?Number(input.value):input.value;
    }
    return node;
  });
}

function rerender(){window.dispatchEvent(new HashChangeEvent('hashchange'))}

export function bindTopology(changeId){
  const add=document.querySelector('#add-row');
  if(add)add.onclick=()=>{
    const tbody=document.querySelector('#node-rows');
    const holder=document.createElement('tbody');
    holder.innerHTML=nodeRow(null);
    tbody.appendChild(holder.firstElementChild);
    tbody.querySelectorAll('.remove-row').forEach(b=>b.onclick=()=>b.closest('tr').remove());
  };
  document.querySelectorAll('.remove-row').forEach(b=>b.onclick=()=>b.closest('tr').remove());
  document.querySelector('#topology-create')?.addEventListener('submit',async e=>{
    e.preventDefault();const button=e.target.querySelector('button');button.disabled=true;
    try{
      const name=new FormData(e.target).get('name');
      const headNodes=(await topologyApi.get()).head.nodes;
      const created=await topologyApi.create({name,nodes:headNodes});
      feedback('草稿已创建，可调整整套节点后校验发布。');location.hash='topology/'+created.id;
    }catch(err){feedback(err.message,true);button.disabled=false}
  });
  document.querySelector('#save-change')?.addEventListener('click',async e=>{
    e.preventDefault();if(e.target.disabled)return;e.target.disabled=true;
    try{
      const form=document.querySelector('#topology-form');
      const data={name:form.elements.name.value,nodes:readDraftRows(),revision:Number(form.elements.revision.value)};
      await topologyApi.update(Number(form.elements.change_id.value),data);
      flash('草稿已保存并重新校验。');rerender();
    }catch(err){feedback(err.message,true);e.target.disabled=false}
  });
  document.querySelector('#validate-change')?.addEventListener('click',async e=>{
    const button=e.target;button.disabled=true;
    try{
      const form=document.querySelector('#topology-form');
      await topologyApi.update(Number(form.elements.change_id.value),{name:form.elements.name.value,nodes:readDraftRows(),revision:Number(form.elements.revision.value)});
      const result=await topologyApi.validate(Number(button.dataset.id));
      flash(result.publishable?`校验通过：唯一主库、谱系与位点均满足，可发布。${result.stale_base?'（基准已过期，需处理冲突并确认合并）':''}`:('校验未通过：'+((result.validation_errors||[]).join('；')||'存在未处理冲突')),!result.publishable);
      rerender();
    }catch(err){feedback(err.message,true)}
    finally{button.disabled=false}
  });
  document.querySelector('#cancel-change')?.addEventListener('click',async e=>{
    const button=e.target;button.disabled=true;
    try{await topologyApi.cancel(Number(button.dataset.id));flash('变更已取消，生效拓扑未改变。');location.hash='topology';rerender()}
    catch(err){feedback(err.message,true);button.disabled=false}
  });
  document.querySelectorAll('.choose-conflict').forEach(b=>b.onclick=async()=>{
    const change=changeId;const note=document.querySelector('[name="note-'+b.dataset.node+'"]')?.value||'';
    const buttons=[...document.querySelectorAll('.choose-conflict')];buttons.forEach(x=>x.disabled=true);
    try{
      await topologyApi.resolve(change,{resolutions:[{node:b.dataset.node,choice:b.dataset.choice,note}]});
      flash('冲突处理已记录，双方依据保留在变更历史中。');rerender();
    }catch(err){feedback(err.message,true);buttons.forEach(x=>x.disabled=false)}
  });
  document.querySelector('#publish-change')?.addEventListener('click',async e=>{
    const button=e.target;const id=Number(button.dataset.id);const revision=Number(button.dataset.revision);
    const stale=document.querySelector('#confirm-stale');
    button.disabled=true;
    try{
      const form=document.querySelector('#topology-form');
      if(form)await topologyApi.update(id,{name:form.elements.name.value,nodes:readDraftRows(),revision});
      const updated=await topologyApi.change(id);
      const key='topology-attempt-'+id;const stored=JSON.parse(sessionStorage.getItem(key)||'null');
      const requestId=stored&&stored.revision===updated.revision?stored.requestId:(crypto.randomUUID?crypto.randomUUID():('req-'+Date.now()+'-'+Math.random().toString(36).slice(2)));
      sessionStorage.setItem(key,JSON.stringify({revision:updated.revision,requestId}));
      const result=await topologyApi.publish(id,{request_id:requestId,confirm_stale_base:Boolean(stale?.checked)});
      sessionStorage.removeItem(key);
      flash('整套拓扑已原子切换至 v'+result.applied_version.version+'。');
      rerender();
    }catch(err){flash(err.message+(err.message.includes('记录已保留')||err.message.includes('失败')?'':'（记录已保留，可处理后重试同一条变更）'),true);rerender()}
  });
}
