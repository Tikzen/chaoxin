'use strict';
const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icon = n => `<svg aria-hidden="true"><use href="#i-${n}"/></svg>`;
const active = ['queued','running','paused','cancelling','waiting_answer','retry_wait'];
const labels = {queued:'排队中',retry_wait:'等待自动重试',waiting_answer:'待答题',running:'运行中',paused:'已暂停',cancelling:'停止中',completed:'已完成',cancelled:'已停止',attention:'需处理',failed:'运行失败',interrupted:'已中断',ready:'已同步',saved:'已保存'};
const badge = (status, text) => `<span class="badge ${esc(status)}">${esc(text || labels[status] || status)}</span>`;
let savedAccount = '';
try { savedAccount = localStorage.getItem('study-account') || ''; } catch (_) {}
const state = {data:null, view:'overview', search:'', account:savedAccount, courseFilter:'all', taskFilter:'active', taskAccount:'all', taskCourse:'all', selected:new Set(), detail:null, courseDetail:null, authenticated:false, loadingAccounts:new Set(), syncErrors:new Map()};
const syncPromises = new Map();
const accountLoadPromises = new Map();
let refreshVersion = 0;
let firstLoaded = false;
let quizSettingsDirty = false;
let runtimeSettingsDirty = false;
const courseDetails=new Map();
const courseDetailLoads=new Map();
const answerDrafts=new Map();
let toastTimer;
function toast(message, error = false) {const el=$('#toast');el.textContent=message;el.className=`toast${error?' error':''}`;clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.classList.add('hidden'),4200);}
async function api(path, options={}) {
  const res = await fetch(`/api${path}`, {headers:{'Content-Type':'application/json'},...options});
  const result = await res.json().catch(()=>({detail:'本地应用返回异常，请稍后重试'}));
  if (!res.ok) {
    if (res.status===401 && path!=='/auth') {state.authenticated=false;openAuth();}
    throw new Error(typeof result.detail==='string'?result.detail:'输入内容不正确，请检查后重试');
  }
  return result;
}
const post = (path, body) => api(path,{method:'POST',body:JSON.stringify(body||{})});
function openAuth() {if(!$('#auth-dialog').open)$('#auth-dialog').showModal();}
function empty(title, description, button='', action='browse', name='book') {
  return `<div class="empty-state">${icon(name)}<strong>${esc(title)}</strong><p>${esc(description)}</p>${button?`<button class="button secondary" data-action="${action}">${esc(button)} ${icon('arrow')}</button>`:''}</div>`;
}
function accountName(id) {return state.data.accounts.find(a=>a.id===id)?.name||'学习账号';}
function percent(course) {return course.total?Math.min(100,Math.round(course.done/course.total*100)):0;}
function progress(value) {return `<progress class="progress" max="100" value="${Math.max(0,Math.min(100,Number(value)||0))}" aria-label="完成进度 ${Math.round(Number(value)||0)}%"></progress>`;}
function dateText(date) {return new Date(date).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});}
function timeText(date) {return new Date(date).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});}

function navigate() {
  const view=location.hash.slice(1)||'overview';
  state.view=['overview','courses','tasks','accounts','settings','updates'].includes(view)?view:'overview';
  const meta={overview:['学习概览','你的学习，一目了然','在这台电脑 管理账号、课程和后台任务。'],courses:['我的课程','当前账号的学习空间','切换账号加载对应课程，进入课程即可开始学习。'],tasks:['运行任务','所有账号的任务，一处查看','各账号独立异步运行，可按账号、课程和状态筛选。'],accounts:['账号管理','所有账号，一处管理','保存学习通账号，选择账号即可进入对应课程。'],settings:['运行设置','本地运行','设置全局并发数，在本机持续执行学习任务。'],updates:['检查更新','版本与下载','查看当前版本，获取 GitHub 上发布的正式版。']}[state.view];
  $('#page-title').innerHTML=`${meta[0]}<span class="title-dot">.</span>`;$('#breadcrumb-title').textContent=meta[0];$('#page-eyebrow').textContent=meta[1];$('#page-description').textContent=meta[2];
  $$('.nav-item').forEach(el=>el.classList.toggle('active',el.dataset.view===state.view));
  $$('.view').forEach(el=>el.classList.toggle('hidden',el.id!==`view-${state.view}`));
  if(state.data)render();
}
window.addEventListener('hashchange',navigate);

function courseCard(course, index, selectable=false) {
  const task=state.data.tasks.find(t=>t.course_id===course.id&&active.includes(t.status));
  const pct=task?Math.round(task.progress):percent(course);
  const account=state.data.accounts.find(a=>a.id===course.account_id);
  const symbol=['book','spark','grid','users'][index%4];
  const chars=['Aa','AI','文','心'];
  return `<article class="course-card${state.selected.has(course.id)?' is-selected':''}"><div class="course-cover cover-${index%4}"><span class="cover-label">${['LANGUAGE & LEARNING','EXPLORE THE FUTURE','ARTS & HUMANITIES','LIFE & GROWTH'][index%4]}</span><span class="cover-char">${chars[index%4]}</span>${icon(symbol)}${task?taskBadge(task):account?.mode==='demo'?badge('demo','演示课程'):pct===100?badge('completed'):''}${selectable?`<input type="checkbox" class="course-checkbox" data-select="${esc(course.id)}" aria-label="选择 ${esc(course.title)}" ${state.selected.has(course.id)?'checked':''} ${task?'disabled':''}>`:''}</div><div class="course-body"><h3 title="${esc(course.title)}">${esc(course.title)}</h3><p class="course-teacher">${icon('users')}${esc(course.teacher||'学习通课程')}</p><div class="course-progress-info"><span>${course.total?`已完成 ${course.done} / ${course.total} 节`:'可手动刷新章节与任务点进度'}</span><strong>${course.total||task?`${pct}%`:'待同步'}</strong></div>${progress(pct)}<p class="course-point-summary">${course.task_total!=null?`任务点 ${course.task_done}/${course.task_total}${course.tasks_complete?'':' · 部分状态未知'}`:'任务点尚未读取'}${['queued','running'].includes(course.progress_status)?' · 刷新中…':''}</p><p class="course-account">${esc(account?.name||'')} · ${esc(account?.hint||'')}</p><button class="course-action" data-action="open-course" data-id="${esc(course.id)}">${task?'进入课程 · 查看进度 →':'进入课程 →'}</button><button class="course-action refresh-progress-button" data-action="refresh-course-progress" data-id="${esc(course.id)}" ${['queued','running'].includes(course.progress_status)?'disabled':''}>${['queued','running'].includes(course.progress_status)?'正在读取任务点…':'刷新任务点状态'}</button></div></article>`;
}

function renderOverview() {
  const d=state.data;const running=d.tasks.filter(t=>active.includes(t.status));const completed=d.tasks.filter(t=>t.status==='completed');
  const metrics=[['已保存账号',d.accounts.length,'个','users','独立保存，随时切换'],['正在运行',running.filter(t=>t.status==='running').length,'门','play',`最多同时运行 ${d.concurrency} 门课程`],['等待运行',running.filter(t=>['queued','retry_wait'].includes(t.status)).length,'门','clock','自动排队，无需等待'],['已完成任务',completed.length,'项','check','每一步，都有记录']];
  $('#metrics').innerHTML=metrics.map(([name,n,unit,i,foot])=>`<div class="metric"><div class="metric-label">${name}</div><div class="metric-icon">${icon(i)}</div><div class="metric-number">${n}<small>${unit}</small></div><div class="metric-foot">${n&&name==='正在运行'?'<span class="green">●</span>':''}${foot}</div></div>`).join('');
  $('#active-total').textContent=running.length;
  $('#overview-tasks').innerHTML=running.length?running.slice(0,3).map(t=>`<div class="task-row"><div class="task-thumb">${icon('book')}</div><div class="task-info"><strong>${esc(t.title)}</strong><p>${esc(accountName(t.account_id))} · ${esc(t.state.chapter||'等待运行')}</p></div>${taskBadge(t)}<div class="task-progress"><div class="task-progress-header"><span>学习进度</span><strong>${Math.round(t.progress)}%</strong></div>${progress(t.progress)}</div><button class="icon-button row-button" data-action="detail" data-id="${t.id}" aria-label="查看任务详情">${icon('arrow')}</button></div>`).join(''):empty('准备好，开始第一门课程','从课程列表中选择内容，任务启动后会显示在这里。','去选择课程');
  const logs=d.tasks.flatMap(t=>(t.state.logs||[]).slice(-2).map(log=>({...log,title:t.title}))).sort((a,b)=>new Date(b.time)-new Date(a.time)).slice(0,4);
  $('#activity-list').innerHTML=logs.length?logs.map(log=>`<div class="activity-item"><p>${esc(log.message)}</p><small>${esc(log.title)} · ${timeText(log.time)}</small></div>`).join(''):`<div class="activity-empty">${icon('clock')}<span>下一次进展，从这里开始</span><small>运行日志会自动更新</small></div>`;
  const courses=d.courses.filter(c=>c.account_id===state.account);
  $('#overview-course-title').textContent=state.account?`${accountName(state.account)}的课程`:'当前账号的课程';
  $('#overview-courses').innerHTML=courses.length?courses.slice(0,4).map((c,i)=>courseCard(c,i)).join(''):empty('当前账号还没有课程','选择账号后会自动读取课程，也可以手动同步。',state.account?'同步课程':'添加账号',state.account?'sync':'add-account');
}

function filteredCourses() {
  return state.data.courses.filter(c=>c.account_id===state.account&&(`${c.title} ${c.teacher}`.toLowerCase().includes(state.search.toLowerCase()))&&(state.courseFilter==='all'||(state.courseFilter==='completed'?percent(c)===100:percent(c)<100))).sort((a,b)=>(a.source_position??0)-(b.source_position??0));
}
function renderCourses() {
  const options=state.data.accounts.length?state.data.accounts.map(a=>`<option value="${a.id}">${esc(a.name)}${a.mode==='demo'?'（演示）':''}</option>`).join(''):'<option value="">请添加账号</option>';
  for(const select of [$('#course-account'),$('#current-account')]) {
    if(select.innerHTML!==options)select.innerHTML=options;
    select.value=state.account;
    select.disabled=!state.data.accounts.length;
  }
  const loading=state.loadingAccounts.has(state.account);
  const error=state.syncErrors.get(state.account);
  $('#account-course-status').textContent=loading?`正在为「${accountName(state.account)}」加载课程，你可以继续查看其他账号和任务…`:error?`课程加载失败：${error}`:state.account?`当前账号：${accountName(state.account)} · 按学习通列表顺序 · 仅显示此账号的课程`:'';
  $('#account-course-status').classList.toggle('has-error',!!error);
  const available=new Set(state.data.courses.map(c=>c.id));
  state.selected.forEach(id=>{if(!available.has(id)||!state.data.courses.some(c=>c.id===id&&c.account_id===state.account)||state.data.tasks.some(t=>t.course_id===id&&active.includes(t.status)))state.selected.delete(id);});
  const courses=filteredCourses();
  $('#course-list').innerHTML=courses.length?courses.map((c,i)=>courseCard(c,i,true)).join(''):loading?empty('正在加载课程','课程会在读取完成后自动显示，切换页面不会中断正在运行的任务。'):empty(state.data.accounts.length?'还没有匹配的课程':'先连接一个学习账号',state.data.accounts.length?'调整筛选条件，或点击「同步课程」读取课程列表。':'保存账号后，就可以同步并选择课程。',state.data.accounts.length?'同步课程':'添加账号',state.data.accounts.length?'sync':'add-account');
  $('#selected-count').textContent=state.selected.size;$('#selection-bar').classList.toggle('hidden',!state.selected.size);
  const eligible=courses.filter(c=>!state.data.tasks.some(t=>t.course_id===c.id&&active.includes(t.status)));
  $('#select-all').checked=eligible.length>0&&eligible.every(c=>state.selected.has(c.id));
  $('#select-all').indeterminate=eligible.some(c=>state.selected.has(c.id))&&!$('#select-all').checked;
}

function taskActions(t) {
  let result=`<button class="button secondary" data-action="detail" data-id="${t.id}">${t.status==='waiting_answer'?'处理题目':'日志详情'}</button>`;
  if(t.status==='retry_wait'&&t.engine==='server')result+=`<button class="button primary" data-action="retry-now" data-id="${t.id}" ${state.data.capabilities?.retry_now?'':'disabled title="当前后台需在任务结束后重新启动，以启用立即重试"'}>${icon('refresh')}立即重试</button>`;
  if(['running','paused','waiting_answer'].includes(t.status))result+=`<button class="button secondary" data-action="control" data-control="${t.status==='paused'?'run':'pause'}" data-id="${t.id}">${icon(t.status==='paused'?'play':'pause')}${t.status==='paused'?'继续':'暂停'}</button>`;
  if(active.includes(t.status)&&t.status!=='cancelling')result+=`<button class="button danger" data-action="control" data-control="cancel" data-id="${t.id}">${icon('stop')}停止</button>`;
  if(['failed','attention','interrupted','cancelled'].includes(t.status))result+=`<button class="button secondary" data-action="retry" data-id="${t.id}">${icon('refresh')}重试</button>`;
  return result;
}
function taskBadge(t) {
  return badge(t.status,t.status==='retry_wait'&&t.state.retry_scope==='points'?'自动恢复中':undefined);
}
function recoveryPanel(t) {
  const rows=t.state.point_recovery||[];
  if(!rows.length)return '';
  return `<section class="point-queue"><strong>独立恢复队列</strong><p class="field-help">${Number(t.state.point_retry_pending)||0} 个任务点等待重试，${Number(t.state.point_needs_review)||0} 个已隔离待处理。其余任务继续执行；已取得的有效答案会复用。</p>${rows.map(p=>`<p class="point-history"><strong>${esc(p.chapter||p.name)}</strong> · ${p.blocked?'已隔离 · 需处理':`第 ${Number(p.failures)} 次失败 · ${dateText(p.next_retry_at)} 自动恢复`}<br>${esc(p.error)}</p>`).join('')}<p class="field-help">达到上限或明确不可执行的点不再反复调用 AI；后台每 15 分钟只读检查平台状态。修复材料后可点击「立即重试」开启新的任务点恢复预算。</p></section>`;
}
function renderTasks() {
  const d=state.data;
  if(!d.accounts.some(a=>a.id===state.taskAccount))state.taskAccount='all';
  const accountOptions='<option value="all">所有账号</option>'+d.accounts.map(a=>`<option value="${a.id}">${esc(a.name)}</option>`).join('');
  if($('#task-account').innerHTML!==accountOptions)$('#task-account').innerHTML=accountOptions;
  $('#task-account').value=state.taskAccount;
  const scoped=d.tasks.filter(t=>state.taskAccount==='all'||t.account_id===state.taskAccount);
  const courses=[...new Map(scoped.map(t=>[t.course_id,t])).values()];
  if(!courses.some(t=>t.course_id===state.taskCourse))state.taskCourse='all';
  const courseOptions='<option value="all">所有课程</option>'+courses.map(t=>`<option value="${t.course_id}">${esc(t.title)} · ${esc(accountName(t.account_id))}</option>`).join('');
  if($('#task-course').innerHTML!==courseOptions)$('#task-course').innerHTML=courseOptions;
  $('#task-course').value=state.taskCourse;
  let tasks=scoped.filter(t=>state.taskCourse==='all'||t.course_id===state.taskCourse);
  if(state.taskFilter==='active')tasks=tasks.filter(t=>active.includes(t.status));
  if(state.taskFilter==='completed')tasks=tasks.filter(t=>t.status==='completed');
  if(state.taskFilter==='attention')tasks=tasks.filter(t=>['attention','failed','interrupted'].includes(t.status));
  $('#task-result-count').textContent=`${tasks.length} 个任务${state.taskAccount==='all'?' · 所有账号':''}`;
  $('#task-list').innerHTML=tasks.length?tasks.map(t=>`<article class="task-card"><div class="task-card-top"><div class="task-thumb">${icon('book')}</div><div><h3>${esc(t.title)}</h3><p>${esc(accountName(t.account_id))} · ${dateText(t.created_at)}</p></div>${taskBadge(t)}</div><div class="task-card-progress"><div class="task-progress-header"><span>${esc(t.state.chapter||'等待运行')}${t.state.total?` · ${t.state.done||0}/${t.state.total} 节`:''}</span><strong>${Math.round(t.progress)}%</strong></div>${progress(t.progress)}</div>${t.state.error||t.state.sync_error?`<div class="task-error">${esc(t.state.error||t.state.sync_error)}</div>`:''}<div class="task-card-foot"><span>${icon(t.engine==='github'?'cloud':'server')}${t.engine==='github'?'旧版云端任务':'这台电脑'}${t.status==='paused'?' · 将在下一个检查点暂停':''}</span><div class="task-card-actions">${taskActions(t)}</div></div></article>`).join(''):empty('这里暂时没有任务','选择一门课程开始，随后可以在这里查看进度、暂停或停止。','选择课程');
}
function renderAccounts() {
  $('#account-list').innerHTML=state.data.accounts.length?state.data.accounts.map(a=>`<article class="account-card${a.id===state.account?' current':''}"><div class="account-top"><div class="account-avatar">${esc(a.name.slice(0,1))}</div><div><h3>${esc(a.name)}</h3><p>${esc(a.hint)}</p></div></div>${badge(a.mode==='demo'?'demo':a.status,a.mode==='demo'?'演示账号':null)}<p class="account-task-count">${state.data.tasks.filter(t=>t.account_id===a.id&&active.includes(t.status)).length} 个进行中的任务${a.id===state.account?' · 当前账号':''}</p><div class="account-bottom"><button class="button primary" data-action="use-account" data-id="${a.id}">进入课程</button><button class="button secondary" data-action="sync-account" data-id="${a.id}">${icon('refresh')}同步课程</button>${a.mode==='real'?`<button class="button secondary" data-action="password" data-id="${a.id}">改密码</button>`:''}<button class="button secondary account-delete" data-action="delete-account" data-id="${a.id}">删除</button></div></article>`).join(''):empty('账号管理，从一个账号开始','添加学习通账号，密码加密保存在这台电脑 上。','添加账号','add-account','users');
}
function rememberAccount() {
  try {localStorage.setItem('study-account',state.account);} catch (_) {}
}
async function loadCourseDetail(id) {
  if(courseDetailLoads.has(id))return courseDetailLoads.get(id);
  const request=api(`/courses/${id}`).then(result=>{courseDetails.set(id,result);if(state.courseDetail===id)renderCourseDetail();return result;}).finally(()=>courseDetailLoads.delete(id));
  courseDetailLoads.set(id,request);return request;
}
function renderCourseDetail() {
  if(!state.courseDetail)return;
  const course=state.data.courses.find(c=>c.id===state.courseDetail);if(!course)return;
  const chapterScroll=$('#course-detail .chapter-list')?.scrollTop||0;
  const task=state.data.tasks.find(t=>t.course_id===course.id&&active.includes(t.status));
  const account=state.data.accounts.find(a=>a.id===course.account_id);
  const chapters=courseDetails.get(course.id)?.chapters||[];
  const refreshing=['queued','running'].includes(course.progress_status);
  $('#course-detail-title').textContent=course.title;
  const taskNames={video:'音视频',document:'文档',read:'阅读',workid:'章节测验',live:'直播'};
  const chapterHTML=chapters.length?`<div class="chapter-list">${chapters.map(c=>`<div class="chapter-item"><div class="chapter-title"><strong>${esc(c.title)}</strong>${badge(c.completed?'completed':c.locked?'muted':'queued',c.completed?'章节已完成':c.locked?'未开放':'章节待完成')}</div>${c.tasks.map(t=>`<div class="point-item"><span>${esc(taskNames[t.type]||'任务点')} · ${esc(t.name)}${t.requires_answer?' · 需要作答':''}</span>${badge(t.status==='completed'?'completed':t.status==='unknown'?'muted':'queued',t.status==='completed'?'已完成':t.status==='unknown'?'未知':'未完成')}</div>`).join('')}${c.empty?'<p class="field-help">平台显示暂无内容；无可执行任务点，章节完成状态仍以平台确认为准。</p>':''}${!c.tasks_loaded?'<p class="field-help">此章节任务点未能完整读取，数量可能不完整。</p>':''}</div>`).join('')}</div>`:'<p class="field-help progress-help">点击「刷新任务点状态」即可查看，无需开始学习。</p>';
  $('#course-detail').innerHTML=`<div class="course-owner">${icon('users')}<div><strong>${esc(account.name)}</strong><span>${esc(account.hint)}</span></div>${account.mode==='demo'?badge('demo','演示账号'):''}</div><p class="detail-chapter">${esc(course.teacher||'学习通课程')}${course.total?` · 章节 ${course.done}/${course.total}`:' · 章节尚未读取'}${course.task_total!=null?` · 任务点 ${course.task_done}/${course.task_total}${course.tasks_complete?'':'（部分状态未知）'}`:''}</p>${progress(percent(course))}<div class="progress-refresh-row"><button class="button secondary" data-action="refresh-course-progress" data-id="${course.id}" ${refreshing?'disabled':''}>${icon('refresh')}${refreshing?'正在刷新…':'刷新任务点状态'}</button><span>${course.progress_updated_at?`上次读取 ${dateText(course.progress_updated_at)}`:'尚未读取平台任务点'}</span></div>${course.progress_error?`<div class="task-error">${esc(course.progress_error)}</div>`:''}${chapterHTML}<div class="notice compact">章节测验需要作答并提交。自动答题服务在「运行设置」中配置，无人值守时，未匹配的题目自动延迟重试；辅助模式可在任务详情填写。${account.mode==='demo'?' 当前为演示数据。':''}</div><div class="dialog-actions">${task?`<button class="button primary" data-action="course-task" data-id="${task.id}">查看运行任务 ${icon('arrow')}</button>`:`<button class="button primary" data-action="start-course" data-id="${course.id}">${icon('play')}开始学习</button>`}</div>`;
  const chapterList=$('#course-detail .chapter-list');if(chapterList)chapterList.scrollTop=chapterScroll;
}
function renderQuizFields() {
  const value=$('#quiz-provider').value;
  for(const [name,provider] of [['go','TikuGo'],['yanxi','TikuYanxi'],['like','TikuLike'],['adapter','TikuAdapter'],['silicon','SiliconFlow']])$(`#quiz-${name}-fields`).classList.toggle('hidden',value!==provider);
}
function quizForm(t) {
  const quiz=t.state.pending_quiz;if(!quiz)return '';
  const drafts=answerDrafts.get(`${t.id}:${quiz.id}`)||{};
  const names={single:'单选',multiple:'多选',judgement:'判断',completion:'填空',shortanswer:'简答',unknown:'未知题型'};
  return `<form id="task-answer-form" data-task-id="${t.id}" data-quiz-id="${quiz.id}"><div class="notice compact">请填写全部答案后提交。单选填写 A，多选填写 AC，判断填写正确或错误；也支持填写完整选项内容。</div>${quiz.questions.map((q,index)=>`<label class="quiz-question"><strong>${index+1}. [${esc(names[q.type]||q.type)}] ${esc(q.title)}${q.type==='completion'?`（共 ${Number(q.blank_count)||1} 空，按顺序每行一空）`:''}</strong><pre>${esc(q.options)}</pre><textarea name="${esc(q.id)}" data-question-id="${esc(q.id)}" required rows="2" placeholder="填写答案">${esc(drafts[q.id]??quiz.answers[q.id]??'')}</textarea></label>`).join('')}<p class="form-error" id="answer-error"></p><button class="button primary" type="submit" ${t.status!=='waiting_answer'?'disabled':''}>${t.status==='paused'?'请继续任务后提交':'提交答案并继续任务'}</button></form>`;
}


function confirmPointConcurrency(count) {
  return count <= 3 || window.confirm(`单课程并发推荐设置为 3，当前选择为 ${count}。\n过高的并发暂未实测可用性，可能导致意外问题，请谨慎开启。`);
}

function pointQueue(t) {
  if(!t.state.point_concurrency)return '';
  const limit=Number(t.state.point_concurrency)||1;const rows=t.state.active_points||[];const history=t.state.point_history||[];
  const experimental=limit>1||Number(t.state.point_peak)>1;
  const names={processed:'执行结束 · 待平台确认',completed:'平台已确认完成',running:'运行中',waiting_answer:'待答题',retry_wait:'等待重试',blocked:'已隔离 · 需处理',attention:'需处理',failed:'执行失败'};
  return `<section class="point-queue"><div class="point-queue-top"><strong>任务点队列</strong><span>目标并发 ${limit} · 运行 ${rows.length}</span></div>${experimental?'<div class="notice compact experimental-notice">单课程多并发：试验性功能，不保证稳定</div>':''}<div class="point-concurrency-buttons">${Array.from($('#point-concurrency').options,o=>Number(o.value)).map(n=>`<button class="button ${n===limit?'primary':'secondary'}" data-action="point-limit" data-id="${t.id}" data-count="${n}" aria-label="单课程并发 ${n}" aria-pressed="${n===limit}" ${n===limit||!active.includes(t.status)?'disabled':''}>${n}${n===1?' · 串行':n===3?' · 推荐':''}</button>`).join('')}</div>${rows.length>limit?'<p class="field-help">正在等待已领取任务结束，再降至目标并发；不会中断或重新领取。</p>':''}<p class="field-help">并发数是执行上限；只有当前可领取的任务点才会占用位置。章节完成数以最近一次平台回读为准。</p>${t.state.platform_progress_at?`<p class="field-help">平台进度读取于 ${dateText(t.state.platform_progress_at)}${t.state.platform_progress_error?' · 刷新暂未成功，现有任务继续':''}</p>`:''}<div class="point-workers">${rows.map(p=>`<div class="point-worker"><strong>${esc(p.name)}</strong><small>${esc(p.chapter)} · PID ${Number(p.pid)} · ${esc(names[p.status]||p.status)}</small>${p.video_total?`<span>${Number(p.video_seconds)||0}/${Number(p.video_total)} 秒</span>${progress(p.video_progress)}`:''}</div>`).join('')}${!rows.length?'<p class="field-help">当前没有运行中的任务点。</p>':''}</div>${history.length?`<details><summary>本轮已处理 ${history.length} 个任务点</summary>${history.map(p=>`<p class="point-history">${esc(p.name)} · ${esc(names[p.status]||p.status)}${p.error?` · ${esc(p.error)}`:''}</p>`).join('')}</details>`:''}${t.state.task_point_limit?`<p class="field-help">本次限定 ${Number(t.state.task_point_limit)} 个任务点；课程完成度仍以平台回读为准。</p>`:''}</section>`;
}

function renderDetail() {
  if(!state.detail)return;
  const t=state.data.tasks.find(t=>t.id===state.detail);if(!t)return;
  const existingForm=$('#task-answer-form');
  if(existingForm&&existingForm.dataset.quizId===t.state.pending_quiz?.id&&existingForm.contains(document.activeElement))return;
  const log=$('#task-detail .log-console');const atBottom=!log||log.scrollHeight-log.scrollTop-log.clientHeight<35;const scroll=log?.scrollTop||0;
  $('#detail-title').textContent=t.title;
  const safeUrl=t.state.run_url&&/^https:\/\/github\.com\/[^/]+\/[^/]+\/actions\/runs\/\d+$/.test(t.state.run_url)?t.state.run_url:null;
  $('#task-detail').innerHTML=`<div class="detail-summary">${taskBadge(t)}<span>${esc(accountName(t.account_id))} · ${t.engine==='github'?'旧版云端任务':'这台电脑'}</span></div>${progress(t.progress)}<p class="detail-chapter">${esc(t.state.chapter||'等待运行')} · 总进度 ${Math.round(t.progress)}%${t.state.video_total?`<br>当前音视频：${t.state.video_seconds||0}/${t.state.video_total} 秒（此数值为播放计时，章节完成以平台确认为准）`:''}</p>${t.state.next_retry_at&&t.status==='retry_wait'?`<p class="field-help">计划重试：${dateText(t.state.next_retry_at)} · ${t.state.retry_scope==='points'?'按任务点独立恢复，不消耗整门课重试预算':`已安排 ${Number(t.state.retry_count)||0} 次`}；未占用运行位置</p>`:''}${t.state.answer_progress?`<p class="field-help">有效答案 ${Number(t.state.answer_progress.matched)||0}/${Number(t.state.answer_progress.total)||0} · 当前来源 ${esc(t.state.answer_progress.source)}</p>`:''}${t.state.quiz_result?`<p class="field-help">平台作答记录：已提交 · 成绩 ${t.state.quiz_result.score==null?'待批阅':esc(t.state.quiz_result.score)}</p>`:''}${t.state.error?`<div class="task-error">${esc(t.state.error)}</div>`:''}${pointQueue(t)}${recoveryPanel(t)}${quizForm(t)}<div class="log-console">${(t.state.logs||[]).length?t.state.logs.map(l=>`<div class="log-line ${esc(l.level)}"><time>${timeText(l.time)}</time><span>${esc(l.message)}</span></div>`).join(''):'等待工作进程输出日志…'}</div><div class="dialog-actions">${safeUrl?`<a class="button secondary" href="${esc(safeUrl)}" target="_blank" rel="noopener noreferrer">打开 GitHub 运行记录 ↗</a>`:''}${taskActions(t).replace(/<button[^>]*data-action="detail"[^>]*>.*?<\/button>/,'')}</div>`;
  const next=$('#task-detail .log-console');next.scrollTop=atBottom?next.scrollHeight:scroll;
}
function render() {
  const d=state.data;
  if(!d.accounts.some(a=>a.id===state.account)) {state.account=d.accounts.find(a=>a.mode==='real')?.id||d.accounts[0]?.id||'';rememberAccount();}
  $('#nav-course-count').textContent=d.courses.filter(c=>c.account_id===state.account).length;$('#nav-task-count').textContent=d.tasks.filter(t=>active.includes(t.status)).length;
  renderOverview();renderCourses();renderTasks();renderAccounts();renderDetail();renderCourseDetail();
  if(!runtimeSettingsDirty&&!$('#runtime-form').contains(document.activeElement)){$('#concurrency').value=d.concurrency;const select=$('#point-concurrency');select.value=d.point_concurrency??1;select.dataset.accepted=select.value;}
  if(!quizSettingsDirty&&!$('#quiz-settings-form').contains(document.activeElement)){for(const [field,value] of Object.entries(d.quiz)){const el=$(`#quiz-settings-form [name=${field}]`);if(!el)continue;if(el.multiple){[...el.options].forEach(o=>{o.selected=value.includes(o.value);});}else{el.value=String(value);}}renderQuizFields();}
  $('#ai-readiness').textContent=state.data.answer_sources?.ai_ready?'AI 兜底已就绪；当前有效顺序：'+state.data.answer_sources.chain.join(' → '):'AI 兜底尚未配置。当前仅使用可用题库，缺题时会延迟重试；填写上方 AI 配置并测试连接后保存。';
  $('#quiz-config-status').textContent=Object.values(d.quiz_secrets).some(Boolean)?'凭据已加密保存':'';
  if(state.courseDetail){const c=d.courses.find(c=>c.id===state.courseDetail);const cached=courseDetails.get(state.courseDetail);if(c&&cached?.course.progress_updated_at!==c.progress_updated_at)loadCourseDetail(c.id).catch(()=>{});}

}
async function refresh(quiet=false) {
  const version=++refreshVersion;
  try {
    const data=await api('/dashboard');
    if(version!==refreshVersion)return true;
    state.data=data;state.authenticated=true;$('#connection-label').textContent='本地应用运行中';$('#connection-light').classList.remove('offline');$('#connection-error').classList.add('hidden');render();
    if(!firstLoaded){firstLoaded=true;$('.version').textContent='v'+state.data.version;
      checkUpdates(true);if(state.account)loadAccountCourses(state.account).catch(error=>toast(error.message,true));}
    return true;
  } catch(error) {
    if(version!==refreshVersion)return false;
    $('#connection-label').textContent='本地应用未连接';$('#connection-light').classList.add('offline');
    if(state.authenticated){$('#connection-error').textContent='连接暂时中断，正在重试。请确认启动窗口仍在运行。';$('#connection-error').classList.remove('hidden');}
    if(!quiet&&state.authenticated)toast(error.message,true);return false;
  }
}
async function switchAccount(id) {
  if(!state.data.accounts.some(a=>a.id===id))return;
  state.account=id;rememberAccount();state.selected.clear();state.search='';state.courseFilter='all';
  $('#course-search').value='';$$('#course-filters button').forEach(b=>b.classList.toggle('selected',b.dataset.filter==='all'));
  location.hash='courses';render();
  await loadAccountCourses(id);
}
async function loadAccountCourses(id) {
  if(accountLoadPromises.has(id))return accountLoadPromises.get(id);
  const work=(async()=>{
    state.loadingAccounts.add(id);state.syncErrors.delete(id);renderCourses();
    try {
      const result=await api(`/accounts/${id}/courses`);
      // Merge only this account. A slow response for A must never replace B's courses.
      if(!state.data.accounts.some(a=>a.id===id))return;
      state.data.courses=state.data.courses.filter(c=>c.account_id!==id).concat(result.courses);
      render();
      if(!result.courses.length||result.account.status==='saved')await syncAccount(id,true);
    } catch(error) {state.syncErrors.set(id,error.message);throw error;}
    finally {accountLoadPromises.delete(id);state.loadingAccounts.delete(id);if(state.data)render();}
  })();
  accountLoadPromises.set(id,work);return work;
}
async function syncAccount(id,quiet=false) {
  if(syncPromises.has(id))return syncPromises.get(id);
  const work=(async()=>{
    state.loadingAccounts.add(id);state.syncErrors.delete(id);renderCourses();
    if(!quiet)toast(`正在为「${accountName(id)}」同步课程…`);
    try {const r=await post(`/accounts/${id}/sync`);await refresh();if(!quiet)toast(`「${accountName(id)}」已同步 ${r.count} 门课程`);return r;}
    catch(error){state.syncErrors.set(id,error.message);throw error;}
    finally{syncPromises.delete(id);state.loadingAccounts.delete(id);if(state.data)render();}
  })();
  syncPromises.set(id,work);return work;
}
async function startCourses(ids) {
  await post('/tasks',{course_ids:ids,engine:'server'});state.selected.clear();
  state.taskAccount='all';state.taskCourse='all';state.taskFilter='active';
  $$('#task-filters button').forEach(b=>b.classList.toggle('selected',b.dataset.filter==='active'));
  await refresh();location.hash='tasks';toast('已加入本机后台队列，切换账号不会停止任务');
}
async function perform(el,fn) {if(el?.disabled)return;if(el)el.disabled=true;try{await fn();}catch(error){toast(error.message,true);}finally{if(el)el.disabled=false;}}

document.addEventListener('click',async event=>{
  const close=event.target.closest('[data-close]');if(close){document.getElementById(close.dataset.close).close();return;}
  const button=event.target.closest('[data-action]');if(!button)return;
  const {action,id}=button.dataset;
  if(action==='add-account'){$('#account-error').textContent='';$('#account-dialog').showModal();return;}
  if(action==='browse'){location.hash='courses';return;}
  if(action==='detail'){state.detail=id;renderDetail();if(!$('#task-dialog').open)$('#task-dialog').showModal();return;}
  if(action==='course-task'){$('#course-dialog').close();state.detail=id;renderDetail();$('#task-dialog').showModal();return;}
  if(action==='open-course'){state.courseDetail=id;renderCourseDetail();if(!$('#course-dialog').open)$('#course-dialog').showModal();loadCourseDetail(id).catch(error=>toast(error.message,true));return;}
  if(action==='use-account'){await perform(button,()=>switchAccount(id));return;}
  if(action==='password'){$('#password-form [name=accountId]').value=id;$('#password-form [name=password]').value='';$('#password-dialog').showModal();return;}
  await perform(button,async()=>{
    if(action==='demo'||action==='demo-new'){const r=await post(action==='demo-new'?'/demo?new_account=true':'/demo');await refresh();await switchAccount(r.account_id);toast('演示账号已就绪，任务不会访问学习通');}
    if(action==='clear-task-filters'){state.taskAccount='all';state.taskCourse='all';renderTasks();}
    if(action==='start-course'){await startCourses([id]);$('#course-dialog').close();}
    if(action==='refresh')await refresh();
    if(action==='refresh-course-progress'){await post(`/courses/${id}/progress`);await refresh();toast('已加入进度刷新队列，不会启动学习任务');}
    if(action==='refresh-account-progress'){if(!state.account)return;const r=await post(`/accounts/${state.account}/progress`);await refresh();toast(`已安排刷新 ${r.count} 门课的任务点进度`);}
    if(action==='sync-account')await syncAccount(id);
    if(action==='sync'){
      const accounts=state.data.accounts.filter(a=>a.id===state.account);
      if(!accounts.length){$('#account-dialog').showModal();return;}
      for(const a of accounts)await syncAccount(a.id);
    }
    if(action==='delete-account'){
      if(!confirm('删除此账号及其课程、历史任务？运行中的任务需先停止。'))return;
      await api(`/accounts/${id}`,{method:'DELETE'});await refresh();toast('账号及本机记录已删除');
    }
    if(action==='point-limit'){if(!confirmPointConcurrency(Number(button.dataset.count)))return;await api(`/tasks/${id}/point-concurrency`,{method:'PUT',body:JSON.stringify({concurrency:Number(button.dataset.count)})});await refresh();toast('并发已调整；已领取的任务继续，后续按新上限派发');}
    if(action==='control'){await post(`/tasks/${id}/control`,{action:button.dataset.control});await refresh();toast({pause:'已请求暂停，将在下一个检查点生效',run:'任务已继续',cancel:'已请求停止任务'}[button.dataset.control]);}
    if(action==='retry-now'){await post(`/tasks/${id}/retry-now`);await refresh();toast('已立即重新排队；有空闲运行位置后开始');}
    if(action==='retry'){const t=state.data.tasks.find(t=>t.id===id);await startCourses([t.course_id]);$('#task-dialog').close();}
  });
});
$('#course-search').addEventListener('input',event=>{state.search=event.target.value;renderCourses();});
for(const id of ['course-account','current-account'])document.getElementById(id).addEventListener('change',event=>{switchAccount(event.target.value).catch(error=>toast(error.message,true));});
$('#task-account').addEventListener('change',event=>{state.taskAccount=event.target.value;state.taskCourse='all';renderTasks();});
$('#task-course').addEventListener('change',event=>{state.taskCourse=event.target.value;renderTasks();});
$('#course-list').addEventListener('change',event=>{const id=event.target.dataset.select;if(!id)return;event.target.checked?state.selected.add(id):state.selected.delete(id);renderCourses();});
$('#select-all').addEventListener('change',event=>{filteredCourses().filter(c=>!state.data.tasks.some(t=>t.course_id===c.id&&active.includes(t.status))).forEach(c=>event.target.checked?state.selected.add(c.id):state.selected.delete(c.id));renderCourses();});
for(const kind of ['course','task'])$(`#${kind}-filters`).addEventListener('click',event=>{const b=event.target.closest('button[data-filter]');if(!b)return;state[`${kind}Filter`]=b.dataset.filter;$$(`#${kind}-filters button`).forEach(el=>el.classList.toggle('selected',el===b));kind==='course'?renderCourses():renderTasks();});
$('#start-selected').addEventListener('click',event=>perform(event.currentTarget,()=>startCourses([...state.selected])));
$('#auth-dialog').addEventListener('cancel',event=>event.preventDefault());
$('#task-dialog').addEventListener('close',()=>{state.detail=null;});
$('#course-dialog').addEventListener('close',()=>{state.courseDetail=null;});
$('#auth-form').addEventListener('submit',async event=>{event.preventDefault();const b=$('button',event.currentTarget);b.disabled=true;$('#auth-error').textContent='';try{await post('/auth',Object.fromEntries(new FormData(event.currentTarget)));$('#auth-form').reset();$('#auth-dialog').close();await refresh();}catch(error){$('#auth-error').textContent=error.message;}finally{b.disabled=false;}});
$('#account-form').addEventListener('submit',async event=>{event.preventDefault();const form=event.currentTarget;const b=$('button[type=submit]',form);b.disabled=true;$('#account-error').textContent='';try{const a=await post('/accounts',Object.fromEntries(new FormData(form)));form.reset();$('#account-dialog').close();await refresh();await switchAccount(a.id);toast('账号已保存，正在加载课程');}catch(error){$('#account-error').textContent=error.message;}finally{b.disabled=false;}});
$('#password-form').addEventListener('submit',event=>{event.preventDefault();const form=event.currentTarget;const values=Object.fromEntries(new FormData(form));perform($('button[type=submit]',form),async()=>{await api(`/accounts/${values.accountId}`,{method:'PATCH',body:JSON.stringify({password:values.password})});form.reset();$('#password-dialog').close();await refresh();toast('密码已更新，请重新同步课程');});});
$('#point-concurrency').addEventListener('change',({target})=>{
  if(confirmPointConcurrency(Number(target.value))){target.dataset.accepted=target.value;runtimeSettingsDirty=true;}
  else target.value=target.dataset.accepted??String(state.data?.point_concurrency??1);
});
$('#concurrency').addEventListener('change',()=>{runtimeSettingsDirty=true;});
$('#runtime-form').addEventListener('submit',event=>{event.preventDefault();perform($('button',event.currentTarget),async()=>{await api('/settings',{method:'PUT',body:JSON.stringify({concurrency:Number($('#concurrency').value),point_concurrency:Number($('#point-concurrency').value)})});runtimeSettingsDirty=false;await refresh();toast('运行设置已保存');});});
$('#logout').addEventListener('click',event=>perform(event.currentTarget,async()=>{await post('/logout');state.authenticated=false;openAuth();}));
navigate();refresh(true);setInterval(()=>{if(state.authenticated&&!document.hidden)refresh(true);},2500);
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&state.authenticated)refresh(true);});

$('#quiz-provider').addEventListener('change',renderQuizFields);
for(const eventName of ['input','change'])$('#quiz-settings-form').addEventListener(eventName,()=>{quizSettingsDirty=true;});
$('#quiz-settings-form').addEventListener('submit',event=>{
  event.preventDefault();const form=event.currentTarget;
  perform($('button[type=submit]',form),async()=>{await api('/quiz',{method:'PUT',body:JSON.stringify(quizSettingsBody(form))});$$('input[type=password]',form).forEach(el=>{el.value='';});quizSettingsDirty=false;await refresh();toast('答题设置已保存，等待重试的任务将重新排队');});
});
document.addEventListener('input',event=>{
  const form=event.target.closest('#task-answer-form');if(!form||!event.target.dataset.questionId)return;
  const key=`${form.dataset.taskId}:${form.dataset.quizId}`;const drafts=answerDrafts.get(key)||{};
  drafts[event.target.dataset.questionId]=event.target.value;answerDrafts.set(key,drafts);
});
document.addEventListener('submit',async event=>{
  const form=event.target.closest('#task-answer-form');if(!form)return;
  event.preventDefault();const button=$('button[type=submit]',form);button.disabled=true;
  try{await post(`/tasks/${form.dataset.taskId}/answers`,{quiz_id:form.dataset.quizId,answers:Object.fromEntries(new FormData(form))});toast('答案已提交到本机任务，正在处理章节测验');await refresh();}
  catch(error){toast(error.message,true);button.disabled=false;}
});

function quizSettingsBody(form) {
  const data=new FormData(form);const body=Object.fromEntries(data);
  body.fallbacks=data.getAll('fallbacks');
  for(const field of ['attempts','max_retries','retry_delay'])body[field]=Number(body[field]);
  body.likeapi_vision=body.likeapi_vision==='true';
  body.auto_ai_fallback=body.auto_ai_fallback==='true';
  body.ai_vision=body.ai_vision==='true';
  return body;
}

$('#quiz-test-ai').addEventListener('click',event=>perform(event.currentTarget,async()=>{
  const output=$('#quiz-test-result');output.textContent='正在测试 AI 连接与 JSON 答案格式，最长约 45 秒…';
  try {const result=await post('/quiz/test',quizSettingsBody($('#quiz-settings-form')));output.textContent=`${result.message}（${result.latency_ms} ms）`;}
  catch(error){output.textContent=error.message;throw error;}
}));

let offeredVersion = null;
function updateLink(id, url) {
  const link=$(id);
  if(url) link.href=url; else link.removeAttribute('href');
  link.classList.toggle('hidden',!url);
}
async function checkUpdates(automatic=false){
  const check=$('#update-check'), install=$('#update-install'), result=$('#update-result');
  if(check.disabled)return;
  check.disabled=true;install.disabled=true;
  result.textContent='正在检查 GitHub 上的正式版本…';
  try {
    const r=await api('/updates/check');
    const available=Boolean(r.update_available);
    const nav=$('#nav-updates');
    nav.title=available?'检查更新：有可用更新 '+r.latest_version:'检查更新';
    nav.setAttribute('aria-label',nav.title);
    $('#update-badge').classList.toggle('hidden',!available);
    updateLink('#update-release',r.release_url);
    updateLink('#update-download',r.download_url);
    offeredVersion=available && r.asset_available && state.data.capabilities?.portable_updates ? r.latest_version : null;
    install.classList.toggle('hidden',!offeredVersion);
    if(!r.latest_version) result.textContent='当前 v'+r.current_version+'；仓库尚未发布正式版本。';
    else if(!available) result.textContent='当前已是最新版本：v'+r.current_version+'。';
    else {
      result.textContent='有可用更新：'+r.latest_version+'（当前 v'+r.current_version+'）。'+(r.download_url?'':' 可在发布页查看可用下载。');
      if(automatic && state.view!=='updates')toast('有可用更新 '+r.latest_version+'，点击左侧「检查更新」即可下载。');
    }
  }catch(error){result.textContent='检查失败：'+error.message+'。可点击「重新检查」。';}
  finally{check.disabled=false;install.disabled=false;}
}
$('#update-check').onclick=()=>checkUpdates();
$('#update-install').onclick=async()=>{
  if(!offeredVersion)return;
  const result=$('#update-result'), check=$('#update-check'), install=$('#update-install');
  install.disabled=check.disabled=true;result.textContent='正在下载、校验并试启动新版，请保持窗口打开…';
  try{
    const target=offeredVersion.replace(/^v/,'');
    await post('/updates/install',{version:offeredVersion});
    result.textContent='正在重启本地控制台…';
    for(let i=0;i<60;i++){
      await new Promise(resolve=>setTimeout(resolve,1000));
      try{const d=await api('/dashboard');if(d.version===target){location.reload();return;}}catch(_){}
    }
    result.textContent='未确认新版启动，请查看启动窗口并刷新页面。';
  }catch(error){result.textContent=error.message;}
  finally{install.disabled=check.disabled=false;}
};
