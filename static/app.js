const api = async (url, options={}) => {
  const res = await fetch(url, {headers:{'Content-Type':'application/json'}, ...options});
  const data = await res.json().catch(()=>({}));
  if (!res.ok) throw new Error(data.message || res.statusText);
  return data;
};
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const km = m => ((m||0)/1000).toFixed(2);
const fmtTime = s => { if(!s) return '—'; h=Math.floor(s/3600),m=Math.floor(s%3600/60); return `${h}h ${m}m`; };
const statusTag = s => ({user_confirmed:['user','用户确认'],device_estimated:['device','设备估算'],gps_measured:['gps','GPS测量']}[s]||['gps',s]);
const showTag = s => { const [c,t]=statusTag(s); return `<span class="tag ${c}">${t}</span>`; };
const tabs=['import','stats','races','duplicates','injuries','reports','share','method'];
const tabNames={import:'导入/活动',stats:'月份统计',races:'比赛/故事',duplicates:'去重/订正',injuries:'伤病私密',reports:'年报',share:'隐私分享',method:'公开口径'};
document.getElementById('tabs').innerHTML=tabs.map(t=>`<button data-tab="${t}" onclick="showTab('${t}')">${tabNames[t]}</button>`).join('');
function showTab(t){ tabs.forEach(x=>document.getElementById('tab-'+x).classList.toggle('hidden',x!==t)); document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('active',b.dataset.tab===t)); localStorage.tab=t; if(t==='stats')loadStats(); if(t==='duplicates')loadDuplicates(); if(t==='races')loadRacesAndStories(); if(t==='injuries')loadInjuries(); if(t==='reports')loadReports(); if(t==='method')loadMethod(); }
showTab(localStorage.tab||'import');

async function importFile(){
  const file=document.getElementById('file').files[0]; if(!file) return alert('请选择 GPX/TCX/JSON 文件');
  const content_base64=btoa(String.fromCharCode(...new Uint8Array(await file.arrayBuffer())));
  const data=await api('/api/imports',{method:'POST',body:JSON.stringify({filename:file.name,content_base64,tz_name:document.getElementById('tz').value})});
  const el=document.getElementById('importResult'); el.classList.remove('hidden');
  el.textContent=JSON.stringify(data,null,2); loadActivities();
}
async function loadActivities(){
  const list=await api('/api/activities?include_duplicates=1');
  document.getElementById('activities').innerHTML=`<table><thead><tr><th>ID</th><th>日期</th><th>名称/来源</th><th>距离</th><th>移动时间</th><th>身份</th><th>订正</th></tr></thead><tbody>${list.map(a=>{
    const src=a.sources?.[0]||{}; const dup=a.canonical_activity_id!==a.id;
    return `<tr><td>${a.id}</td><td>${a.local_start_date}<br><span class="muted">${esc(a.tz_name||'UTC')}</span></td><td>${esc(a.name||src.filename||'未命名')}<br><span class="muted">${esc(src.provider||'未知来源')} / ${esc(src.source_uid||'无来源ID')}</span></td><td>${km(a.effective.distance_m)} km ${showTag(a.effective.distance_status)}</td><td>${fmtTime(a.effective.moving_s)} ${showTag(a.effective.moving_status)}</td><td>${dup?`<span class="tag dup">重复 #${a.canonical_activity_id}</span>`:'<span class="tag user">规范身份</span>'}${a.is_race?' <span class="tag race">比赛</span>':''}</td><td><div class="row"><input style="width:90px" placeholder="订正km" id="d${a.id}"><button onclick="correct(${a.id})">距离</button></div><div class="muted">rev ${a.revisions?.length||1} · 删除跳点 ${a.dropped_points}</div></td></tr>`}).join('')}</tbody></table>`;
}
async function correct(id){ const v=document.getElementById('d'+id).value; if(!v)return; const a=await api(`/api/activities/${id}/corrections`,{method:'PUT',body:JSON.stringify({corrected_distance_m:+v*1000,reason:'网页手工距离订正'})}); alert(`已保存为用户确认，修订版本 ${a.revisions.at(-1).revision}`); loadActivities(); }

async function loadMethod(){ const m=await api('/api/methodology'); document.getElementById('method').innerHTML=`<div class="notice"><b>非医疗声明：</b>${esc(m.non_medical_notice)}</div><h3>跨午夜/时区/移动时间</h3><pre>${esc(JSON.stringify(m.rules,null,2))}</pre><h3>GPS与去重</h3><pre>${esc(JSON.stringify({gps:m.gps,duplicate_matching:m.duplicate_matching,labels:m.labels,privacy:m.privacy},null,2))}</pre>`; populateRules(m); }
function populateRules(m){ const keys=Object.keys(m.rules); ['statsRule','reportRule'].forEach(id=>{const s=document.getElementById(id); if(s && !s.options.length) s.innerHTML=keys.map(k=>`<option value="${k}" ${k===m.current_rule?'selected':''}>${k} — ${m.rules[k].label}</option>`).join('')}); }
async function loadStats(){
  await fetch('/api/methodology').then(r=>r.json()).then(populateRules);
  const year=document.getElementById('statsYear').value, start=document.getElementById('statsStart').value, end=document.getElementById('statsEnd').value, rule=document.getElementById('statsRule').value;
  const q=new URLSearchParams({rule_version:rule}); if(year)q.set('year',year); if(start)q.set('start',start); if(end)q.set('end',end);
  const s=await api('/api/statistics?'+q); window._stats=s;
  document.getElementById('statsCards').innerHTML=[['活动数',s.training.activity_count],['总里程',s.training.distance_km+' km'],['移动时间',fmtTime(s.training.moving_s)],['平均配速',s.training.average_pace_s_per_km?Math.floor(s.training.average_pace_s_per_km/60)+':'+String(Math.round(s.training.average_pace_s_per_km%60)).padStart(2,'0')+'/km':'—']].map(x=>`<div class="card"><div class="muted">${x[0]}</div><div class="num">${x[1]}</div></div>`).join('')
    +`<div class="card"><div class="muted">统一范围</div><b>${s.data_range.start}</b><br><span class="muted">至 ${s.data_range.end}</span><br>${showTag('user_confirmed')} ${km(s.training.distance_by_status.user_confirmed)} km<br>${showTag('device_estimated')} ${km(s.training.distance_by_status.device_estimated)} km<br>${showTag('gps_measured')} ${km(s.training.distance_by_status.gps_measured)} km</div>`;
  drawChart(s.training.monthly);
  document.getElementById('statsActivities').innerHTML=`<table><thead><tr><th>日期</th><th>名称</th><th>距离</th><th>移动</th><th>类型</th></tr></thead><tbody>${s.activities.map(a=>`<tr><td>${a.date}</td><td>${esc(a.name||'')}</td><td>${km(a.distance_m)} ${showTag(a.distance_status)}</td><td>${fmtTime(a.moving_s)} ${showTag(a.moving_status)}</td><td>${a.is_race?'<span class="tag race">比赛</span>':'训练'}</td></tr>`).join('')}</tbody></table><p class="muted">${esc(s.separation_notice)}</p>`;
}
function drawChart(monthly){ const c=document.getElementById('monthChart'),x=c.getContext('2d'); c.width=c.clientWidth;c.height=230;x.clearRect(0,0,c.width,c.height); const max=Math.max(1,...monthly.map(m=>m.distance_m)); const w=c.width/monthly.length; monthly.forEach((m,i)=>{const h=(m.distance_m/max)*170;x.fillStyle='#2563eb';x.fillRect(i*w+12,c.height-35-h,w-24,h);x.fillStyle='#334155';x.font='11px sans-serif';x.save();x.translate(i*w+18,c.height-18);x.rotate(-0.55);x.fillText(m.month.slice(2),0,0);x.restore();x.fillText(km(m.distance_m),i*w+12,c.height-42-h);});x.strokeStyle='#94a3b8';x.beginPath();x.moveTo(0,c.height-35);x.lineTo(c.width,c.height-35);x.stroke(); }

async function addRace(){ await api('/api/races',{method:'POST',body:JSON.stringify({name:raceName.value,race_date:raceDate.value,distance_m:+raceDistance.value,elapsed_s:+raceSeconds.value||null})}); raceName.value=''; loadRacesAndStories(); }
async function addStory(){ await api('/api/stories',{method:'POST',body:JSON.stringify({happened_on:storyDate.value,title:storyTitle.value,body:storyBody.value,mood:storyMood.value})}); storyTitle.value=storyBody.value=''; loadRacesAndStories(); }
async function loadRacesAndStories(){ const [r,s]=await Promise.all([api('/api/races'),api('/api/stories')]); races.innerHTML=`<h3>成绩（独立于训练统计）</h3><table><tr><th>日期</th><th>比赛</th><th>距离</th><th>成绩</th></tr>${r.map(x=>`<tr><td>${x.race_date}</td><td>${esc(x.name)}</td><td>${km(x.distance_m)} km</td><td>${fmtTime(x.elapsed_s)}</td></tr>`).join('')}</table>`; stories.innerHTML=`<h3>个人故事</h3>${s.map(x=>`<div class="card"><b>${esc(x.title)}</b> <span class="muted">${x.happened_on} ${esc(x.mood||'')}</span><p>${esc(x.body)}</p></div>`).join('')}`; }

async function loadDuplicates(){ const groups=await api('/api/duplicates'); duplicates.innerHTML=groups.length?groups.map(g=>`<div class="card"><h3>重复组 #${g.id} · ${g.status} · 证据 ${g.match_basis} (${g.match_score})</h3>${g.members.map(a=>`<div>#${a.id} ${a.local_start_date} ${esc(a.name||'')} ${km(a.effective.distance_m)}km</div>`).join('')}<p><button class="danger" onclick="split(${g.id})">人工拆分（阻止再次自动合并）</button> <button class="good" onclick="restore(${g.id})">恢复为同一次</button></p></div>`).join(''):'<p class="muted">暂无重复组。可导入同一 GPX 验证手表/手机重复文件。</p>'; }
async function split(id){ await api(`/api/duplicates/${id}/split`,{method:'PUT',body:JSON.stringify({reason:'人工确认是两次真实活动'})}); loadActivities();loadDuplicates(); }
async function restore(id){ await api(`/api/duplicates/${id}/restore`,{method:'PUT',body:JSON.stringify({})}); loadActivities();loadDuplicates(); }

async function addInjury(){ await api('/api/injuries',{method:'POST',body:JSON.stringify({body_part:injPart.value,occurred_on:injDate.value,severity:injSeverity.value,notes:injNotes.value,is_private:true})}); injPart.value=injNotes.value=''; loadInjuries(); }
async function loadInjuries(){ const list=await api('/api/injuries'); injuries.innerHTML=list.map(x=>`<div class="card dangerbox"><b>${esc(x.body_part)}</b> ${x.occurred_on||''} <span class="tag dup">${x.visibility}</span><p>${esc(x.notes)}</p></div>`).join(''); }

async function createReport(){ const d=await api('/api/reports',{method:'POST',body:JSON.stringify({year:+reportYear.value,rule_version:reportRule.value,status:reportStatus.value})}); alert('已生成报告 #'+d.id); loadReports(); }
async function loadReports(){ const list=await api('/api/reports'); reports.innerHTML=`<table><tr><th>ID</th><th>年</th><th>规则</th><th>状态</th><th>范围</th><th>操作</th></tr>${list.map(r=>`<tr><td>${r.id}</td><td>${r.year}</td><td>${r.rule_version}</td><td>${r.status}</td><td>${r.range_start}~${r.range_end}</td><td><button onclick="viewReport(${r.id})">查看</button> <button onclick="publishReport(${r.id})">发布</button> <button onclick="regenerateReport(${r.id})">按原规则生成新草稿</button></td></tr>`).join('')}</table>`; }
async function viewReport(id){ const r=await api('/api/reports/'+id); reportSnapshot.classList.remove('hidden'); reportSnapshot.textContent=JSON.stringify(r.snapshot,null,2); }
async function publishReport(id){ await api(`/api/reports/${id}/publish`,{method:'PUT'});loadReports(); }
async function regenerateReport(id){ const r=await api(`/api/reports/${id}/regenerate`,{method:'PUT'}); alert('已保留旧报告，并创建新草稿 #'+r.id); loadReports(); }

async function createShare(){ const id=+shareActivity.value; const d=await api(`/api/shares/activity/${id}`,{method:'POST',body:JSON.stringify({title:'隐私分享跑',redactions:[{label:'home',lat:+homeLat.value,lon:+homeLon.value,radius_m:+homeRadius.value}]})}); const url=`${location.origin}/#s=${d.token}`; const base=`/api/shares/${d.token}`; shareResult.innerHTML=`<div class="card"><p class="${d.privacy_check.pass?'ok':'bad'}"><b>泄漏检查：${d.privacy_check.pass?'通过':'失败'}</b>；删除 ${d.privacy_check.removed_points} 个隐私点</p><p>公开页：<a href="${url}">${url}</a></p><p>缩略图：<a href="${base}?thumbnail=svg" target="_blank">SVG</a> · 下载：<a href="${base}?download=gpx">redacted-run.gpx</a> · <a href="${base}?check=privacy">检查结果</a></p><div class="map">${svgMap(d.activity.points)}</div><pre>${esc(JSON.stringify(d,null,2))}</pre></div>`; }
function svgMap(points){ if(!points?.length)return '<p>无可见轨迹点。</p>'; let lats=points.map(p=>p.lat),lons=points.map(p=>p.lon),mnlat=Math.min(...lats),mxlat=Math.max(...lats),mnlon=Math.min(...lons),mxlon=Math.max(...lons); let segs={}; points.forEach(p=>(segs[p.segment]??=[]).push(p)); let pl=Object.values(segs).map(seg=>`<polyline points="${seg.map(p=>{let x=20+(p.lon-mnlon)/Math.max(1e-9,mxlon-mnlon)*760,y=220-(p.lat-mnlat)/Math.max(1e-9,mxlat-mnlat)*180;return `${x.toFixed(1)},${y.toFixed(1)}`}).join(' ')}"/>`).join(''); return `<svg viewBox="0 0 800 240">${pl}</svg>`; }

loadActivities();
(async function publicShareLoader(){
  const token=new URLSearchParams(location.hash.slice(1)).get('s');
  if(!token) return;
  document.querySelectorAll('.tab,nav').forEach(el=>el.style.display='none');
  const share=await api('/api/shares/'+encodeURIComponent(token));
  document.querySelector('main').innerHTML=`<section class="panel" style="display:block"><h2>${esc(share.activity?.name||share.title||'分享跑步')}</h2><p class="muted">隐私区域附近轨迹、缩略图和下载文件均已处理。移动 ${fmtTime(share.activity?.distance_m?0:0)}</p><div class="map">${svgMap(share.activity?.points||[])}</div><p><a href="/api/shares/${encodeURIComponent(token)}?download=gpx">下载已脱敏 GPX</a> · <a href="/api/shares/${encodeURIComponent(token)}?thumbnail=svg" target="_blank">缩略图</a></p><p class="ok">泄漏检查删除 ${share.privacy_check?.removed_points ?? 0} 点，结果 ${share.privacy_check?.pass?'通过':'失败'}</p></section>`;
})();
