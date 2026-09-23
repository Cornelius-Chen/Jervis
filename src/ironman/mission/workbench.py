"""Trusted local controls, separate from sandboxed generated project artifacts."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

import psutil

from ironman.storage import StorageError
from .console import _workers
from .state import StateStore


ARTIFACT_CSP = ("sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; "
                "style-src 'unsafe-inline'; img-src 'self' data:; media-src 'self' data:; "
                "connect-src 'none'; font-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
CONTROL_CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
               "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def alive(pid):
    return bool(pid and psutil.pid_exists(pid))


def identity(value):
    return value if value.startswith('mission:') else 'mission:' + value


class Workbench:
    def __init__(self, root, database):
        self.root = Path(root).resolve()
        self.database = Path(database).resolve()
        self.children = {}
        self.lock = threading.Lock()

    def project(self, store, project_id):
        state, version = store.load(identity(project_id))
        if state.get('architecture') != 'vnext':
            raise ValueError('This entry controls vNext projects only')
        return state, version

    def snapshot(self):
        store = StateStore(self.root, self.database)
        try:
            projects = []
            rows = store.memory._index.execute("SELECT object_id FROM objects WHERE object_type='ProjectOverlay'").fetchall()
            for row in rows:
                state, version = store.load(row[0])
                if state.get('architecture') != 'vnext':
                    continue
                public = {key:state.get(key) for key in (
                    'title','brief','status','stop_reason','current_action','calls','usage','steps','budget',
                    'scope_policy','entities','impact_history','questions','unknowns','feedback',
                    'domain_versions','scope_versions','batch_id','global_max_calls','control_sequence')}
                public.update(id=row[0], version=version, coordinator={
                    'pid':state.get('owner_pid'), 'alive':alive(state.get('owner_pid'))})
                public['handoffs'] = [{'node':h['node'],'reason':h.get('reason'),
                    'capsule':{key:h.get('capsule',{}).get(key) for key in
                        ('derived','attempt_version','project_version','status','remaining_calls','limitations')}}
                    for h in state.get('handoffs',[])]
                public['workers'] = _workers(Path(state['directory']))
                public['accounting'] = {key:state.get('execution_accounting',{}).get(key) for key in
                    ('calls','usage','unreported_calls','baseline_calls','baseline_usage_unknown')}
                for worker in public['workers']:
                    worker['alive'] = alive(worker.get('worker_pid'))
                public['pending_controls'] = [{'action':e.event_type,'sequence':e.stream_sequence}
                    for e in store.controls(row[0],state.get('control_sequence',0))]
                public['nodes'] = []
                for node in state['nodes']:
                    entry = {key:node.get(key) for key in ('id','title','tool','status','version','attempt',
                        'requires_all','requires_any','joint_with','alternative_group','resources','contract','execution')}
                    entry['artifacts'] = []
                    directory = Path(state['directory']).resolve()
                    for artifact in node.get('result',{}).get('artifacts',[]):
                        path = Path(artifact).resolve()
                        if path.is_file() and path.is_relative_to(directory) and path.is_relative_to(self.root/'projects'):
                            entry['artifacts'].append({'name':path.name,
                                'url':'/'+quote(path.relative_to(self.root/'projects').as_posix(),safe='/')})
                    public['nodes'].append(entry)
                public['attempts'] = []
                for event in store.registry.events.read_stream(row[0]+':execution'):
                    if event.event_type != 'attempt.started':
                        continue
                    attempt, av = store.load(event.payload['attempt_ref'])
                    item = {k:attempt.get(k) for k in ('attempt_ref','node','generation','worker_instance','status','reason','successor')}
                    item['version'] = av
                    item['checkpoints'] = [c['phase'] for c in attempt['checkpoints']]
                    compiled = next((c['detail']['packet'] for c in reversed(attempt['checkpoints'])
                                     if c['phase']=='context_compiled'),None)
                    if compiled:
                        memory = compiled['local_memory']
                        item['context'] = {'scope':compiled['scope'],'actual_chars':compiled['actual_chars'],
                            'health':compiled['health'],'entity_versions':compiled['semantic_contract']['entity_versions'],
                            'selected_memory':[{k:x.get(k) for k in ('id','version','scope','source_kind','reason')} for x in memory['selected']],
                            'rejected_memory':memory['excluded']}
                    public['attempts'].append(item)
                projects.append(public)
            batches = []
            for batch in sorted({p['batch_id'] for p in projects}):
                ref = 'portfolio:'+batch
                if store.exists(ref):
                    state, version = store.load(ref)
                    batches.append({'id':ref,'version':version,'owner_pid':state.get('owner_pid'),
                        'owner_alive':alive(state.get('owner_pid')),**{k:state.get(k) for k in
                        ('projects','status','budget','dispatch_count','project_statuses','stop_reason','errors')}})
            with self.lock:
                processes = [{'batch':batch,'pid':child.pid,'alive':child.poll() is None,'returncode':child.poll()}
                             for batch,child in self.children.items()]
            return {'authority':'Registry and public worker receipts; process existence is checked now',
                    'observed_at':datetime.now(timezone.utc).isoformat(),
                    'projects':projects,'portfolios':batches,'launched_processes':processes}
        finally:
            store.close()

    def launch(self, store, projects, batch):
        projects = list(dict.fromkeys(identity(p) for p in projects))
        if not projects:
            raise ValueError('Select at least one project')
        ref = 'portfolio:'+batch
        if store.exists(ref):
            portfolio, _ = store.load(ref)
            if alive(portfolio.get('owner_pid')):
                raise ValueError('This portfolio coordinator is already running')
            if portfolio['projects'] != projects:
                raise ValueError('Use the existing portfolio project set, or another batch name')
        for project in projects:
            state, _ = self.project(store,project)
            if alive(state.get('owner_pid')):
                raise ValueError('A project coordinator is already running')
        previous = self.children.get(batch)
        if previous and previous.poll() is None:
            raise ValueError('This portfolio process is already running')
        env = os.environ.copy()
        env['PYTHONPATH'] = str(self.root/'src') + os.pathsep + env.get('PYTHONPATH','')
        env['PYTHONIOENCODING'] = 'utf-8'
        logs = self.root/'projects'/'_vnext_control'
        logs.mkdir(exist_ok=True)
        # The fixed log is diagnostic public CLI output, not a model transport stream.
        with (logs/'portfolio.log').open('ab') as output:
            child = subprocess.Popen([sys.executable,'-m','ironman.mission','portfolio',
                '--root',str(self.root),'--database',str(self.database),'--batch',batch,
                '--projects',*projects],cwd=self.root,env=env,stdout=output,stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        self.children[batch] = child
        return {'pid':child.pid,'state':'process launched; completion is determined by Registry results'}

    def action(self, data):
        from .runtime import Mission
        with self.lock:
            action = data['action']
            if action == 'create':
                brief = data['brief'].strip()
                if not brief:
                    raise ValueError('A brief is required')
                calls, steps = int(data.get('max_calls',24)),int(data.get('max_steps',30))
                if calls < 1 or steps < 1:
                    raise ValueError('Budgets must be positive')
                mission = Mission.create(self.root,self.database,data['project'],brief,
                    architecture='vnext',max_calls=calls,max_steps=steps,
                    excluded_scopes=data.get('excluded_scopes',[]),batch_id=data.get('batch','vnext-alignment'))
                try:
                    mission.export()
                    return {'project':mission.id,'state':'created; no work started'}
                finally:
                    mission.close()
            store = StateStore(self.root,self.database)
            try:
                if action == 'run':
                    return self.launch(store,data['projects'],data.get('batch','vnext-alignment'))
                if action not in {'pause','resume','stop','handoff','feedback','fact_change'}:
                    raise ValueError('Unknown control')
                project_id = identity(data['project'])
                state, _ = self.project(store,project_id)
                stopped = state['status']=='STOPPED' or any(e.event_type=='stop'
                    for e in store.controls(project_id,state.get('control_sequence',0)))
                if action in {'resume','handoff'} and stopped:
                    raise ValueError('STOP is final for this project; create a new project to continue')
                detail = {}
                if action == 'handoff':
                    if not any(n['id']==data['node'] and n['status']=='RUNNING' for n in state['nodes']):
                        raise ValueError('Select a running node for replacement')
                    detail = {'node':data['node'],'reason':data.get('reason','Local owner planned replacement')}
                elif action == 'feedback':
                    if data['kind'] not in {'constraint','preference','observation'} or not data['text'].strip() or not data['scopes']:
                        raise ValueError('Feedback needs text, scopes and a supported kind')
                    detail = {k:data[k] for k in ('text','scopes','kind')}
                    detail['supersedes_event_ids'] = data.get('supersedes_event_ids',[])
                    detail.update(source_kind='human',source_ref='local owner workbench',
                                  apply_to_future=bool(data.get('apply_to_future',False)))
                    from .scopes import validate_supersession
                    validate_supersession(state,detail)
                elif action == 'fact_change':
                    field = state['entities'][data['entity_id']]['fields'][data['field']]
                    if data['expected_version'] != field['version']:
                        raise ValueError('Fact version has changed; refresh before submitting')
                    detail = {k:data[k] for k in ('entity_id','field','value','expected_version')}
                    detail.update(source_kind='human',source_ref='local owner workbench')
                event = store.control(project_id,action,detail)
                response = {'event':event.event_id,'state':'recorded; coordinator applies at next boundary'}
                child = self.children.get(state.get('batch_id'))
                portfolio_running = bool(child and child.poll() is None)
                portfolio_ref = 'portfolio:'+state.get('batch_id','vnext-alignment')
                if store.exists(portfolio_ref):
                    portfolio_running = portfolio_running or alive(store.load(portfolio_ref)[0].get('owner_pid'))
                if not alive(state.get('owner_pid')) and not portfolio_running:
                    mission = Mission(self.root,self.database,project_id)
                    try:
                        mission.apply_controls()
                        response['state'] = mission.state['status']
                    finally:
                        mission.close()
                    if action == 'resume':
                        batch = state.get('batch_id','vnext-alignment')
                        members = store.load('portfolio:'+batch)[0]['projects'] if store.exists('portfolio:'+batch) else [project_id]
                        response['launch'] = self.launch(store,members,batch)
                return response
            finally:
                store.close()


def handler_factory(root, database):
    app = Workbench(root,database)

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,directory=str(app.root/'projects'),**kwargs)

        def end_headers(self):
            trusted = self.path in {'/vnext/','/vnext/api','/vnext/designer/','/vnext/designer/api','/vnext/designer/feedback'}
            self.send_header('Content-Security-Policy',CONTROL_CSP if trusted else ARTIFACT_CSP)
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Cache-Control','no-store')
            super().end_headers()

        def local_host(self):
            return self.headers.get('Host') in {f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'}

        def respond(self, status, body, content_type='application/json; charset=utf-8'):
            raw = body.encode('utf-8') if isinstance(body,str) else json.dumps(body,ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type',content_type)
            self.send_header('Content-Length',str(len(raw)))
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(raw)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not self.local_host():
                self.respond(403,{'error':'Local host required'})
            elif self.path == '/vnext/':
                self.respond(200,PAGE,'text/html; charset=utf-8')
            elif self.path == '/vnext/api':
                self.respond(200,app.snapshot())
            elif self.path in {'/vnext/designer/','/vnext/designer/api'}:
                from .designer_view import snapshot, render
                data=snapshot(app.root,app.database)
                if self.path.endswith('/api'):
                    self.respond(200,data)
                else:
                    self.respond(200,render(data),'text/html; charset=utf-8')
            else:
                super().do_GET()

        def do_POST(self):
            if self.path not in {'/vnext/api','/vnext/designer/feedback'}:
                self.respond(404,{'error':'Unknown control route'})
                return
            if not self.local_host() or self.headers.get('Origin') != 'http://'+self.headers.get('Host',''):
                self.respond(403,{'error':'Exact local origin required'})
                return
            if self.headers.get_content_type() != 'application/json':
                self.respond(415,{'error':'JSON is required'})
                return
            try:
                data = json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
                if not isinstance(data,dict):
                    raise ValueError('A JSON object is required')
                if self.path=='/vnext/designer/feedback':
                    from .designer_view import record_feedback
                    result=record_feedback(app.root,app.database,data)
                else:
                    result = app.action(data)
            except (ValueError,KeyError,TypeError,StorageError,PermissionError) as error:
                self.respond(400,{'error':str(error)})
                return
            self.respond(200,result)

    Handler.workbench = app
    return Handler


def serve(root, database, port=8766):
    server = ThreadingHTTPServer(('127.0.0.1',port),handler_factory(root,database))
    print(f'Jervis controls: http://127.0.0.1:{server.server_port}/vnext/',flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


PAGE = r'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Jervis · 本地项目控制</title><style>
*{box-sizing:border-box}body{font:16px/1.6 system-ui;margin:0;background:#f2f3f0;color:#202b28}main{max-width:1100px;margin:auto;padding:24px}h1{margin:0}section,article{background:white;border:1px solid #cdd6d0;border-radius:8px;padding:20px;margin:18px 0}label{display:block;margin:8px 0}input,textarea,select,button{font:inherit;max-width:100%;padding:7px;border:1px solid #a9b7ae;border-radius:4px}textarea{width:100%;min-height:90px}button{cursor:pointer;background:#e4eee7;margin:4px}button.stop{background:#fae3df}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px;background:#f3f5f2;padding:12px}small{color:#536159}.row{display:flex;flex-wrap:wrap;gap:12px}.row label{flex:1;min-width:150px}a{color:#14593e}#notice{position:sticky;top:0;background:#fff3d5;padding:10px;white-space:pre-wrap}summary{cursor:pointer;font-weight:600}ul{padding-left:22px}.check{display:inline-block;margin-right:12px}
</style><main><nav><a href="/vnext/designer/">Designer｜学习与作品 →</a></nav><h1>Jervis · 本地项目控制</h1><p>直接读取 Registry。仅显示 vNext 项目；未知信息保持未知。进程存在不代表作品通过验收。</p><div id="notice" role="status">正在读取…</div>
<section><details><summary>创建一个普通项目</summary><form id="create"><label>项目 ID <input name="project" required pattern="[a-zA-Z0-9_-]+" placeholder="my-project"></label><label>目标与已知事实 <textarea name="brief" required></textarea></label><div class="row"><label>最多模型调用 <input name="max_calls" type="number" value="24" min="1"></label><label>最多步骤 <input name="max_steps" type="number" value="30" min="1"></label></div><label>排除的专业范围（逗号分隔）<input name="excluded_scopes" placeholder="audio/music"></label><label>批次 <input name="batch" value="vnext-alignment" required></label><button>创建，不自动运行</button></form></details></section>
<section><h2>共同运行</h2><div id="choices"></div><label>批次 <input id="batch" value="vnext-alignment"></label><button id="run">运行所选项目</button><button id="refresh">刷新记录</button><small>消耗已有模型额度；整个批次上限 144 次，最多同时两个执行槽。等待、暂停和 STOP 不会自动恢复。</small><details><summary>组合调度与本入口启动的进程</summary><pre id="portfolios"></pre></details></section><div id="projects"></div>
<section><h2>提交修正或反馈</h2><form id="feedback"><label>项目 <select name="project" class="project-select"></select></label><label>类型 <select name="kind"><option value="constraint">项目约束</option><option value="preference">个人偏好</option><option value="observation">实际观察</option></select></label><label>专业范围（逗号分隔）<input name="scopes" placeholder="例如 audio/foley 或 designer" required></label><label>内容 <textarea name="text" required></textarea></label><label><input name="apply_to_future" type="checkbox"> 明确同意将该范围的偏好用于未来任务</label><label>明确替代已有反馈（可多选；留空则新增，不覆盖）<select name="supersedes_event_ids" multiple size="4"></select></label><button>记录人的反馈</button></form>
<details><summary>修正共享实体事实</summary><form id="fact"><label>项目 <select name="project" class="project-select"></select></label><div class="row"><label>实体 ID <input name="entity_id" required></label><label>字段 <input name="field" required></label><label>当前字段版本 <input name="expected_version" type="number" min="1" required></label></div><label>新值（JSON，如 7 或 "blue"）<input name="value" required></label><button>提交人的事实修正</button><small>请先核对上面的实体记录。版本冲突不会覆盖新事实。</small></form></details></section></main>
<script>
const $=s=>document.querySelector(s), pretty=x=>JSON.stringify(x,null,2), split=s=>s.split(',').map(x=>x.trim()).filter(Boolean);
let latestProjects=[];
function feedbackReceipts(){const form=$('#feedback'),select=form.elements.supersedes_event_ids;const selected=new Set([...select.selectedOptions].map(o=>o.value));select.replaceChildren();const p=latestProjects.find(p=>p.id===form.elements.project.value);for(const record of p?.feedback||[]){if(record.status==='superseded')continue;const o=el('option',record.event_id+' · '+record.kind+' · '+record.scopes.join(', ')+' · '+record.text,select);o.value=record.event_id;o.selected=selected.has(o.value);}}
function el(tag,text,parent){const x=document.createElement(tag);if(text!==undefined)x.textContent=text;if(parent)parent.append(x);return x;}
function detail(parent,title,value){const d=el('details',undefined,parent);el('summary',title,d);el('pre',pretty(value),d);}
async function act(data){try{const r=await fetch('/vnext/api',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});const result=await r.json();if(!r.ok)throw Error(result.error);$('#notice').textContent=pretty(result);await refresh(false);}catch(e){$('#notice').textContent='未执行或未完成：'+e.message;}}
async function refresh(message=true){try{const r=await fetch('/vnext/api');if(!r.ok)throw Error('读取失败 '+r.status);const data=await r.json();latestProjects=data.projects;const selected=new Set([...document.querySelectorAll('#choices input:checked')].map(x=>x.value));$('#choices').replaceChildren();for(const p of data.projects){const label=el('label',undefined,$('#choices'));label.className='check';const cb=el('input',undefined,label);cb.type='checkbox';cb.value=p.id;cb.checked=selected.has(p.id);label.append(document.createTextNode(' '+p.title));}for(const select of document.querySelectorAll('.project-select')){const prior=select.value;select.replaceChildren();for(const p of data.projects){const o=el('option',p.title,select);o.value=p.id;}if([...select.options].some(x=>x.value===prior))select.value=prior;}feedbackReceipts();$('#portfolios').textContent=pretty({portfolios:data.portfolios,processes:data.launched_processes});$('#projects').replaceChildren();for(const p of data.projects){const card=el('article',undefined,$('#projects'));el('h2',p.title+' · '+p.status,card);const briefBox=el('details',undefined,card);el('summary','原始目标（当前事实与修正见下方）',briefBox);el('p',p.brief,briefBox);el('p','版本 '+p.version+' · 调用 '+p.calls+'/'+p.budget.max_calls+' · 步骤 '+p.steps+'/'+p.budget.max_steps,card);el('p','协调进程 '+(p.coordinator.pid??'未记录')+' / '+(p.coordinator.alive?'存在':'未发现')+' · '+(p.current_action||p.stop_reason||'无当前动作'),card);const controls=el('div',undefined,card);for(const [action,label] of [['pause','暂停'],['resume','明确恢复并运行'],['stop','STOP']]){const b=el('button',label,controls);b.disabled=p.status==='STOPPED';if(action==='stop')b.className='stop';b.onclick=()=>act({action,project:p.id});}for(const n of p.nodes){const line=el('p',n.title+' · '+n.status+' · v'+n.version,card);for(const a of n.artifacts){const link=el('a',' '+a.name,line);link.href=a.url;link.target='_blank';link.rel='noopener';}if(n.status==='RUNNING'){const b=el('button','计划交接此节点',line);b.onclick=()=>act({action:'handoff',project:p.id,node:n.id});}}detail(card,'范围与实体事实（含来源和版本）',{scope:p.scope_policy,entities:p.entities});detail(card,'节点、最小契约与依赖',p.nodes);detail(card,'反馈收据与替代记录',p.feedback);detail(card,'语义变更影响',p.impact_history);detail(card,'执行尝试、选入 / 拒绝的记忆与上下文健康',p.attempts);detail(card,'交接记录',p.handoffs);detail(card,'进程、预算与待应用控制',{workers:p.workers,budget:p.budget,usage:p.usage,accounting:p.accounting,pending_controls:p.pending_controls});detail(card,'待解问题与未知',{questions:p.questions,unknowns:p.unknowns});}if(message)$('#notice').textContent='已读取 '+data.projects.length+' 个项目 · '+new Date(data.observed_at).toLocaleString();}catch(e){$('#notice').textContent=e.message;}}
$('#create').onsubmit=e=>{e.preventDefault();const d=Object.fromEntries(new FormData(e.target));d.max_calls=Number(d.max_calls);d.max_steps=Number(d.max_steps);d.excluded_scopes=split(d.excluded_scopes);act({action:'create',...d});};$('#feedback').onsubmit=e=>{e.preventDefault();const d=Object.fromEntries(new FormData(e.target));d.scopes=split(d.scopes);d.supersedes_event_ids=new FormData(e.target).getAll('supersedes_event_ids');d.apply_to_future=e.target.apply_to_future.checked;act({action:'feedback',...d});};$('#fact').onsubmit=e=>{e.preventDefault();try{const d=Object.fromEntries(new FormData(e.target));d.value=JSON.parse(d.value);d.expected_version=Number(d.expected_version);act({action:'fact_change',...d});}catch(err){$('#notice').textContent='新值不是合法 JSON：'+err.message;}};$('#feedback').elements.project.onchange=feedbackReceipts;$('#run').onclick=()=>act({action:'run',projects:[...document.querySelectorAll('#choices input:checked')].map(x=>x.value),batch:$('#batch').value});$('#refresh').onclick=()=>refresh();refresh();
</script></html>'''
