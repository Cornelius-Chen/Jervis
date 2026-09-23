"""Read projection of original Designer and existing Mission learning records."""
from pathlib import Path
from html import escape
from urllib.parse import quote
import json
import yaml

from . import designer
from .state import StateStore
from ironman.learning.runtime import plain

LABELS={'capability.product_logic.v1':'目标与产品逻辑','capability.information_narrative.v1':'信息与叙事',
 'capability.composition_typography.v1':'构图与文字','capability.asset_fidelity.v1':'图像与材料',
 'capability.interaction_state.v1':'交互与状态','capability.motion_continuity.v1':'运动与连续性',
 'capability.spatial_data_views.v1':'空间与数据','capability.prompt_constraint_engineering.v1':'表达与约束',
 'capability.implementation_completion.v1':'实现与完整性'}

def snapshot(root,database):
    store=StateStore(root,database)
    try:
        corpus=designer.DESIGNER_ROOT
        def registry(name):
            path=corpus/'CCOS/registries'/name
            return yaml.safe_load(path.read_text(encoding='utf-8')) if path.is_file() else {}
        index=registry('active_design_knowledge_index.yaml')
        intake=registry('learning_intake_registry.yaml').get('entries',[])
        sources={x['id']:x for x in intake}
        original=[]
        for item in index.get('items',[]):
            source=sources.get(item.get('source_learning_record') or item['source_record_or_asset'],{})
            asset=designer._local(corpus,item['source_record_or_asset'])
            original_text=asset.read_text(encoding='utf-8') if asset and asset.is_file() and asset.suffix in {'.md','.yaml','.txt'} else None
            original.append({'id':item['id'],'title':source.get('source_name') or item['id'],
                'axes':item.get('capability_axes',[]),'scope':{k:item.get(k,[]) for k in designer._DIMENSIONS},
                'status':source.get('state') or item.get('lifecycle_state'),
                'retrieval':source.get('retrieval_allowed',item.get('retrieval_authority')),
                'origin':'旧 Designer 的建议记录','source':source,'index_record':item,
                'original_text':original_text,
                'history_notice':'保留已有来源和审查链接；未证明存在完整逐步形成史，不事后补写。'})
        works=[]; projects=[]; judgments=[]; studies=[]; applications=[]; readings=[]
        rows=store.memory._index.execute("SELECT object_id FROM objects WHERE object_type='ProjectOverlay'").fetchall()
        for row in rows:
            state,version=store.load(row[0])
            if not state.get('designer') and 'designer' not in state.get('domains',[]):
                continue
            project={'id':row[0],'version':version,'title':state['title'],'status':state['status'],
                'calls':state['calls'],'unknowns':state['unknowns'],'feedback':state['feedback'],
                'entrance':state.get('designer',{}),'domain_versions':state.get('domain_versions',{}),
                'new_round':state.get('batch_id')=='designer-faithful'}
            projects.append(project)
            if state.get('execution_mode')=='LEARNING_ONLY':
                readings.append({'project':row[0],'version':version,'scope':state['learning_scope'],
                    'question':state['learning_question'],'status':state['status'],
                    'stop_reason':state.get('stop_reason'),'calls':state['calls'],'budget':state['budget'],
                    'unknowns':state['unknowns'],'source_gaps':state.get('source_gaps',[]),'sources':[{'source':state['learning_queue'][n['args']['source_index']],
                      'status':n['status'],'result':n.get('result',{}).get('output',{}),
                      'error':n.get('error'),'version':n['version'],'attempt':n['attempt']}
                      for n in state['nodes'] if n['tool']=='design_read']})
            for node in state['nodes']:
                result=node.get('result',{})
                if node['tool']=='design_study' and result.get('output',{}).get('comparison'):
                    contact_path=next((Path(p) for p in result.get('artifacts',[]) if Path(p).name=='source-contact.json'),None)
                    contact=json.loads(contact_path.read_text(encoding='utf-8')) if contact_path and contact_path.is_file() else {}
                    studies.append({'project':row[0],'node':node['id'],'version':node['version'],
                        'scope':state['designer'].get('scope',{}),'result_ref':result.get('result_ref'),
                        'contact':contact,'comparison':result['output']['comparison'],
                        'selection':result['output'].get('selection',{}),'new_round':project['new_round']})
                for raw in result.get('artifacts',[]):
                    path=Path(raw).resolve()
                    if path.name!='index.html' or not path.is_file() or not path.is_relative_to(Path(root)/'projects'):
                        continue
                    url='/'+quote(path.relative_to(Path(root)/'projects').as_posix(),safe='/')
                    shot=path.parent/'observation/desktop.png'
                    if not shot.is_file() and node['tool']=='page':
                        observers=[n for n in state['nodes'] if n['tool']=='inspect'
                                   and n['status']=='COMPLETE' and node['id'] in n.get('requires_all',[])]
                        shot=next((Path(p) for n in observers for p in n.get('result',{}).get('artifacts',[])
                                   if Path(p).name=='desktop.png' and Path(p).is_file()),shot)
                    works.append({'title':('潮间带观察册 · ' if project['new_round'] else '')+node['title'],
                        'project':row[0],'version':node['version'],'project_version':version,'node':node['id'],
                        'status':node['status'],'role':path.parent.name,'url':url,'result_ref':result.get('result_ref'),
                        'new_round':project['new_round'],'baseline':state['title'].endswith('-baseline'),
                        'image':'/'+quote(shot.relative_to(Path(root)/'projects').as_posix(),safe='/') if shot.is_file() else None,
                        'result':result,'application':result.get('output',{}).get('judgment_application',{}),
                        'human_effect':'待真实人类反馈；作品存在和功能检查不证明审美收益。'})
            for event in store.registry.events.read_stream(row[0]):
                if event.event_type=='judgment.applied':
                    applications.append({'project':row[0],'event_id':event.event_id,**plain(event.payload)})
                if event.event_type in {'domain.formed','domain.revised','domain.imported','capability.discovered','knowledge.reconciled'}:
                    payload=plain(event.payload)
                    if 'designer' in str(payload.get('id','')) or event.event_type=='capability.discovered':
                        judgments.append({'project':row[0],'event_id':event.event_id,'event_type':event.event_type,
                            'version':payload.get('version'),'record':payload,
                            'origin':'原入口调用' if event.event_type=='capability.discovered' else ('来源阅读 / 待验证解释' if payload.get('origin')=='source_reading_without_practice' else '模型假说 / 实践修订'),
                            'status':payload.get('status',payload.get('assessment',{}).get('decision','历史记录'))})
        comparisons=[]
        if store.exists('designer:integration:comparisons'):
            comparisons=store.load('designer:integration:comparisons')[0]['pairs']
        feedback=[{'event_id':e.event_id,**plain(e.payload)} for e in store.registry.events.read_stream('designer:integration:human-feedback')]
        baseline=store.load('designer:integration:baseline')[0] if store.exists('designer:integration:baseline') else None
        references=store.load('designer:integration:reference-batch')[0] if store.exists('designer:integration:reference-batch') else None
        mode=store.load('designer:learning:mode')[0] if store.exists('designer:learning:mode') else {}
        consumed=sum(e.event_type=='model.prepared' for e in store.registry.events.read_stream('execution:'+mode.get('execution_batch','designer-faithful')))
        budget={'limit':mode.get('authorized_global_call_limit',48),'consumed':consumed,
                'remaining':max(0,mode.get('authorized_global_call_limit',48)-consumed)}
        return {'projects':projects,'works':sorted(works,key=lambda w:(not w['new_round'],w['role'] in {'practice','variant'},w['baseline'],w['project'])),
            'mode':mode,'readings':readings,'learning_budget':budget,
            'studies':studies,'applications':applications,'references':references,
            'judgments':judgments,'original':original,'intake_counts':{s:sum(x.get('state')==s for x in intake) for s in sorted({str(x.get('state')) for x in intake})},
            'extensions':index.get('local_advisory_extensions_pending_intake_linkage',{}),
            'old_feedback':registry('user_design_preference_ledger.yaml'),
            'baseline':{'file_count':len(baseline['files']),'observed_at':baseline['observed_at'],'entry_chain':baseline['entry_chain']} if baseline else None,
            'comparisons':comparisons,'feedback':feedback,'alignment':designer.ALIGNMENT,
            'read_notice':'本页只读取已存在的记录，浏览和展开详情不会调用模型。'}
    finally:store.close()

def record_feedback(root,database,data):
    store=StateStore(root,database)
    try:
        pairs=store.load('designer:integration:comparisons')[0]['pairs']
        pair=next((p for p in pairs if p['id']==data.get('pair')),None)
        if not pair or data.get('choice') not in {'left','right','tie','unknown'}:
            raise ValueError('请选择本页已有比较和有效判断')
        if data.get('scope') not in pair['scopes'] or not str(data.get('reason','')).strip():
            raise ValueError('反馈需要明确能力范围和观察理由')
        event=store.event('designer:integration:human-feedback','designer.human_comparison',{
            'pair':pair['id'],'versions':pair['versions'],'scope':data['scope'],'choice':data['choice'],
            'reason':data['reason'],'source_kind':'human','attribution':'anonymous local visitor; identity and independence unverified',
            'effect':'scoped evidence only','global_preference_promotion':False})
        return {'event_id':event.event_id,'message':'已绑定作品版本和能力范围；不会自动形成全局偏好。'}
    finally:store.close()

def render(data):
    e=lambda value:escape(str(value),quote=True)
    def raw(value):
        return '<pre>'+e(json.dumps(value,ensure_ascii=False,indent=2))+'</pre>'
    def items(values):
        if not values:return '<p class="muted">尚无该类记录。</p>'
        return '<ul>'+''.join('<li>'+e(v)+'</li>' for v in values)+'</ul>'
    def source_link(url):
        return f'<a href="{e(url)}" rel="noreferrer">打开原始来源 ↗</a>' if isinstance(url,str) and url.startswith(('https://','http://')) else ''
    works='';old_works=''
    for w in data['works']:
        image=f'<img loading="lazy" src="{e(w["image"])}" alt="真实作品截图">' if w['image'] else '<div class="placeholder">打开完整作品查看</div>'
        app=w['application']
        applied=(f'<h4>本次选用</h4>{items(app.get("selected_ids",[]))}<p>{e(app.get("reason","未记录选择理由"))}</p><h4>实际调整方向</h4>{items(app.get("action_changes",[]))}<h4>本次不适用</h4>{items(app.get("not_applicable",[]))}') if app else '<p>该作品结果未记录新判断的应用；不反推其受到了学习影响。</p>'
        card=f'<article class="work"><a class="preview" href="{e(w["url"])}">{image}</a><div class="workbody"><span class="tag">{"本轮 · 原调用基线" if w["baseline"] else "本轮作品" if w["new_round"] else "保留的历史作品"}</span><h3>{e(w["title"])}</h3><p>{e(w["status"])} · 作品版本 {e(w["version"])} · {e({"practice":"练习","variant":"变体"}.get(w["role"],w["role"]))}</p><a class="open" href="{e(w["url"])}">体验完整作品 ↗</a><p class="muted">{e(w["human_effect"])}</p><details><summary>哪些判断影响了这份作品</summary>{applied}<details><summary>原始应用与结果记录</summary>{raw(w["result"])}</details></details></div></article>'
        if w['new_round']:works+=card
        else:old_works+=card
    if old_works:works+='<details><summary>展开保留的历史作品</summary><div class="works">'+old_works+'</div></details>'
    capacities=''
    for axis,label in LABELS.items():
        entries=[x for x in data['original'] if axis in x['axes']]
        contexts=[p for p in data['projects'] if axis in p['entrance'].get('scope',{}).get('capability_axes',[])]
        practices=[s for s in data['studies'] if axis in s['scope'].get('capability_axes',[])]
        status=('有关联练习，尚未逐项验证' if practices else '有任务范围选用记录' if contexts else '仅有旧建议记录' if entries else '索引证据缺失')+' · 人类效果待证'
        capacities+=f'<details class="capacity"><summary><strong>{label}</strong><span>{status}</span></summary><p>以下按实际选择范围关联记录；范围关联不能证明每项能力导致了作品结果。</p>'
        for p in contexts:
            capacities+=f'<p><b>{e(p["title"])}</b> · {e(p["status"])} · 当前判断版本 {e(p["domain_versions"].get("designer","只有原建议"))}</p><p class="muted">任务范围：{e(p["entrance"].get("scope",{}).get("page_archetypes",[]))}</p>'
        for x in entries:
            capacities+=f'<details><summary>{e(x["title"])} · {e(x["status"])}</summary><p>{e(x["history_notice"])}</p>{source_link(x["source"].get("source_path_or_url"))}<p>原接受范围：{e(x["source"].get("accepted_scope","原记录未填写"))}</p><details><summary>原始判断正文（保留原表达）</summary>{"<pre>"+e(x["original_text"])+"</pre>" if x["original_text"] else "<p>该索引项未直接绑定独立判断正文；下面保留原始接受范围和来源元数据，不生成替代正文。</p>"}</details><details><summary>原始来源、作用域与审查记录</summary>{raw(x)}</details></details>'
        if not entries: capacities+='<p>索引未提供本能力的有效记录；不推断不存在其他旧材料。</p>'
        capacities+='</details>'
    history=''
    for study in sorted(data['studies'],key=lambda s:not s['new_round']):
        c=study['contact'];comparison=study['comparison']
        history+=f'<article class="study"><span class="tag">{"本轮练习" if study["new_round"] else "历史练习"} · {e(comparison.get("decision"))}</span><h3>{e(study["project"])}</h3><p><b>选源阶段的原始理由：</b>{e(study["selection"].get("reason","未记录"))}</p><details><summary>后续实际观察、练习与判断修订</summary><h4>模型对实际画面的观察</h4>{items(c.get("observed_artifact",[]))}<h4>作者教法 / 创作意图</h4>{items(c.get("author_claims",[]))}<h4>模型假说</h4>{items(c.get("model_hypotheses",[]))}<h4>练习与变体改变了什么</h4><p>{e(c.get("comparison_question",""))}</p><p>{e(c.get("practice_instruction",""))}</p><p>{e(c.get("variant_instruction",""))}</p><h4>比较后的修订或拒绝</h4><p>{e(comparison.get("reason",""))}</p><p>{e(comparison.get("scope_change",""))}</p><h4>仍缺什么</h4>{items(comparison.get("unknowns",[]))}</details>'
        for j in comparison.get('model',{}).get('judgments',[]):
            uses=[a for a in data['applications'] if a['project']==study['project'] and j['id'] in a.get('selected_ids',[])]
            refused=[a for a in data['applications'] if a['project']==study['project'] and j['id'] not in a.get('selected_ids',[])]
            history+=f'<details><summary>{e(j["title"])} · {"已有本项目应用" if uses else "尚无选用记录"}</summary><h4>判断的原始表达</h4>{items([r["kind"]+"："+r["content"] for r in j["representations"]])}<h4>适用情境</h4>{items(j["applies_when"])}<h4>可能失效</h4>{items(j["fails_when"])}<h4>应用与未选用记录</h4>{items([a.get("reason","") for a in uses+refused])}<p>这些是模型选择和解释；人类效果未证明。</p><details><summary>版本与来源原始记录</summary>{raw({"study_version":study["version"],"judgment":j,"applications":uses,"not_selected":refused})}</details></details>'
        history+='</article>'
    for j in reversed(data['judgments']):
        history+=f'<details><summary><span class="tag">{e(j["origin"])}</span> {e(j["project"])} · {e(j["status"])} · {e(j["version"] or "未记录版本")}</summary>{raw(j)}</details>'
    refs=''
    if data['references']:
        for group in ('works','methods'):
            for r in data['references'][group]:
                refs+=f'<details><summary>{"完成作品参照" if group=="works" else "作者教法 / 意图"} · {e(r["title"])}</summary><p>{e(r["selection_reason"])}</p>{source_link(r["url"])}{items(r.get("quality_basis",{}).get("observations",[]))}{items(r.get("quality_basis",{}).get("limits",[]))}<details><summary>观察状态、来源与使用边界</summary>{raw(r)}</details></details>'
    forms=''
    for p in data['comparisons']:
        options=''.join(f'<option value="{e(s)}">{e(LABELS.get(s,s))}</option>' for s in p['scopes'])
        forms+=f'<article class="comparison"><h3>{e(p["title"])}</h3><p>{e(p["question"])}</p><div class="comparelinks"><a href="{e(p["left_url"])}">打开作品 X ↗</a><a href="{e(p["right_url"])}">打开作品 Y ↗</a></div><form class="review"><input type="hidden" name="pair" value="{e(p["id"])}"><label>本次观察的能力<select name="scope">{options}</select></label><label>比较意见<select name="choice"><option value="unknown">无法判断</option><option value="left">X 更适合</option><option value="right">Y 更适合</option><option value="tie">各有价值 / 没有明显差别</option></select></label><label>在哪个使用情境下，观察到了什么？<textarea name="reason" required></textarea></label><button>保存这次匿名比较</button><p class="receipt" role="status"></p></form></article>'
    if not forms:forms='<p>本轮比较作品尚未齐备。已有历史匿名入口仍可使用。</p>'
    if data.get('mode',{}).get('mode')=='LEARNING_ONLY':
        forms='<p>当前为仅学习阶段，不征集反馈。已有比较与反馈记录保留。</p>'
    current=[p for p in data['projects'] if p['new_round']]
    content=f'''<header><a class="brand" href="/vnext/">JERVIS / 工作台</a><nav><a href="#works">作品</a><a href="#capabilities">能力与证据</a><a href="#history">形成与使用</a><a href="#compare">匿名比较</a></nav></header>
    <main><section class="intro"><div><span class="eyebrow">领域分支 · 按需调用</span><h1>Designer<span>学习与作品</span></h1><p class="lead">先看作品。再看它为何这样做，<br>以及哪些解释还经不起验证。</p></div><aside><span class="tag">当前学习问题</span><h2>图像、浏览与证据，<br>怎样各得其位？</h2><p>围绕完整的“潮间带观察册”，比较主体展示与来源阅读的关系。练习、变体与应用分别保留。</p><p class="muted">审美收益待定 · 没有参数训练</p></aside></section>
    <section id="works"><div class="sectionhead"><h2>实际作品</h2><a href="/apprenticeship-delivery/index.html">历史 A / B / C ↗</a></div><p>完整作品可操作。版本、失败和效果未证明的结论一并保留。</p><div class="works">{works or '<p>当前项目正在生成作品。</p>'}</div><details><summary>本轮运行状态与待解问题</summary>{raw(current)}</details></section>
    <section id="capabilities"><span class="eyebrow">能力 × 情境 × 证据</span><h2>能据什么作判断</h2><p>旧建议、视觉练习、模型解释和人的反应分别看待。记录数量不代表能力强弱。</p>{capacities}</section>
    <section id="history"><span class="eyebrow">来源不会被补写</span><h2>判断如何走到作品里</h2><p>展开查看原调用、作者教法、模型假说、实践修订、拒绝与不更新。模型自述理解不等于验证。</p>{refs}{history}<details><summary>旧 Designer 的真实入口、材料范围与映射</summary>{raw(data['baseline'])}<p>候选 Registry 只是其中一条历史支路。原顶层材料、路由与独立扩展均保留。</p>{raw(data['intake_counts'])}{raw(data['extensions'])}</details><details><summary>原记录中的用户反馈与偏好</summary><p>这是旧账本原文；若原始话语、作品版本或形成时间缺失，不能把摘要提升为可复核的完整人类轨迹。</p>{raw(data['old_feedback'])}</details><details><summary>当前原则与旧表达如何相容</summary>{raw(data['alignment'])}</details></section>
    <section id="compare"><h2>让人的观察留下来</h2><p>先匿名看作品，再记录具体观察。反馈只绑定本次版本和范围；不知道也可以。</p>{forms}<a href="/apprenticeship-delivery/human-review.html">保留的历史匿名 A / B / C 比较入口 ↗</a><details><summary>已收到的本轮人类反馈</summary>{raw(data['feedback']) if data['feedback'] else '<p>尚无真实人类反馈。不会代填。</p>'}</details></section>
    <footer>{e(data['read_notice'])} · 不展示审美总分、掌握百分比或大师等级。</footer></main>'''
    if data.get('mode',{}).get('mode')=='LEARNING_ONLY':
        learning=''
        for branch in data['readings']:
            learning+=f'<section><span class="tag">{e(branch["scope"])}</span><h2>{e(branch["question"])}</h2><p>{e(branch["status"])} · 已记账 {e(branch["calls"])} / {e(branch["budget"]["max_calls"])} 次；正在进行的调用已计入上方共享额度。</p>'
            if branch['stop_reason']:learning+=f'<p>{e(branch["stop_reason"])}</p>'
            if branch['source_gaps']:learning+=f'<details><summary>未读来源及访问缺口</summary>{raw(branch["source_gaps"])}</details>'
            for entry in branch['sources']:
                source=entry['source'];notes=entry['result'].get('notes',{})
                learning+=f'<article class="study"><h3>{e(source["title"])}</h3><p>{e(entry["status"])} · {e(source["version"])} · 来源阅读，解释待验证</p>{source_link(source["url"])}<p>{e(source["quality_basis"])}</p>'
                if notes:
                    decision=entry['result'].get('effective_decision',notes['decision'])
                    label={'no_update':'保留阅读记录；未更新有效判断','experimental':'仅在本作用域保存为待验证候选','rejected':'保留解释及拒绝记录；未采用'}.get(decision,decision)
                    learning+=f'<p><strong>{e(label)}</strong>。记录完成不代表整个来源已完整学习；实际覆盖见下方缺口。</p>'
                    if source.get('original_retrieval_allowed') is False:learning+='<p>原来源仍在 Quarantine，保留原检索限制；作者材料和模型建议未晋升为有效指导。</p>'
                    learning+=f'<h4>本次所得与仍待验证的解释</h4>{items(notes["model_explanations"])}<h4>未解问题</h4>{items(notes["unknowns"])}<h4>下一步依据</h4>{items(notes["next_basis"])}<details><summary>原作观察、作者主张、已有实测与覆盖缺口</summary><h4>原作观察</h4>{items(notes["original_observations"])}<h4>作者主张</h4>{items(notes["author_claims"])}<h4>既有实测证据</h4>{items(notes["existing_empirical_evidence"])}<h4>反例与限制</h4>{items(notes["counterexamples_or_limits"])}{raw(notes["coverage"])}</details><details><summary>候选判断及适用情境</summary>{raw(notes["model"])}</details>'
                elif entry['error']:learning+=f'<p>读取缺口：{e(entry["error"])}</p>'
                else:learning+='<p>尚未形成读取记录，不计为已完整学习。</p>'
                learning+=f'<details><summary>原始引用、权限与版本绑定</summary>{raw(source)}</details></article>'
            learning+='</section>'
        budget=data['learning_budget']
        history=content.split('<main>',1)[1].split('<footer>',1)[0]
        content=f'''<header><a class="brand" href="/vnext/">JERVIS / 工作台</a><nav><a href="#learning">当前学习</a><a href="#preserved">历史记录</a></nav></header><main><section class="intro"><div><span class="eyebrow">LEARNING_ONLY · 领域局部积累</span><h1>Designer<span>学习记录</span></h1><p class="lead">读实际材料，保留出处与疑问。<br>新解释仍待实践验证。</p></div><aside><h2>两个共享执行槽位</h2><p>原累计 {budget['consumed']} / {budget['limit']} 次 · 剩余 {budget['remaining']} 次</p><p>沿用既有 portfolio、预算、checkpoint 与接班路径。批次结束不等于领域学完。</p><p class="muted">本阶段不生成作品、不评测、不征集反馈。</p></aside></section><div id="learning">{learning or '<p>正在核对已有学习队列。</p>'}</div><details id="preserved"><summary>展开保留的历史作品、实验与判断记录</summary>{history}</details><footer>{e(data['read_notice'])} · 刷新读取最新状态；不表示模型已掌握能力。</footer></main>'''
    return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Designer｜学习与作品</title><style>'+CSS+'</style>'+content+SCRIPT+'</html>'

CSS='''*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:#f4f1e9;color:#22251f;font-family:"Segoe UI","Microsoft YaHei",sans-serif;font-size:15px;line-height:1.7}header{max-width:1320px;margin:auto;padding:26px 42px;display:flex;justify-content:space-between;border-bottom:1px solid #d3d2c8}.brand{font-size:12px;font-weight:700;letter-spacing:2px}a{color:inherit;text-underline-offset:5px}nav{display:flex;gap:28px}nav a{text-decoration:none;font-size:13px}main{max-width:1320px;margin:auto;padding:0 42px}.intro{display:grid;grid-template-columns:1.5fr 1fr;gap:60px;padding:70px 0 58px}.eyebrow{font-size:12px;letter-spacing:2px;color:#63715a}h1{font-family:Georgia,"Microsoft YaHei",serif;font-size:76px;line-height:1.08;font-weight:400;letter-spacing:-3px;margin:20px 0}h1 span{font-size:39px;display:block;letter-spacing:0;margin-top:14px}.lead{font-size:20px;color:#5e635a}.intro aside{border-left:1px solid #bcbfb3;padding:20px 0 0 32px;align-self:center}h2{font-size:27px;line-height:1.4;font-weight:500}h3{font-size:21px;line-height:1.4;margin:14px 0}.tag{display:inline-block;font-size:11px;color:#5c674f;letter-spacing:.5px}.muted{font-size:12px;color:#6d7367}.sectionhead{display:flex;align-items:center;justify-content:space-between}section:not(.intro){padding:35px 0 50px;border-top:1px solid #cdd0c2}.works{display:grid;grid-template-columns:1fr 1fr;gap:32px;margin:30px 0}.work{border-bottom:1px solid #cdd0c2;min-width:0}.preview{display:block;height:320px;overflow:hidden;background:#e1e4d8}.preview img{width:100%;height:100%;object-fit:cover;object-position:top;transition:transform .25s}.preview:hover img{transform:scale(1.02)}.placeholder{padding:110px 30px;color:#687460;text-align:center}.workbody{padding:22px 0 28px}.open{font-weight:600}.workbody p{margin:12px 0}.capacity{border-top:1px solid #d2d4c9;padding:17px 0}.capacity summary{display:flex;justify-content:space-between;gap:20px}.capacity summary span{font-size:12px;color:#65715c}details{margin:14px 0}summary{cursor:pointer;overflow-wrap:anywhere}pre{max-height:500px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;background:#e9ece3;padding:18px}form{max-width:620px}label{display:block;margin:14px 0;font-size:13px}input,select,textarea{display:block;width:100%;font:inherit;padding:10px;border:1px solid #a7afa0;background:#fbfcf7;border-radius:0}textarea{min-height:95px}button{padding:12px 20px;font:inherit;background:#3d4e35;color:white;border:0;cursor:pointer}.comparison{padding:22px;border:1px solid #b8c0af;margin:24px 0}.comparelinks{display:flex;gap:35px}.receipt{color:#365e2d}footer{padding:28px 0 40px;color:#697061;font-size:12px}:focus-visible{outline:3px solid #a25333;outline-offset:4px}@media(max-width:700px){header{padding:20px;display:block}nav{gap:16px;margin-top:20px;flex-wrap:wrap}main{padding:0 20px}.intro{grid-template-columns:1fr;gap:20px;padding:45px 0}h1{font-size:60px}.intro aside{padding:0 0 0 20px}.works{grid-template-columns:1fr}.preview{height:270px}.capacity summary{display:block}.capacity summary span{display:block}.sectionhead{align-items:start;gap:20px}}@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}.preview img{transition:none}}'''
SCRIPT='''<script>document.querySelectorAll('form.review').forEach(form=>form.addEventListener('submit',async event=>{event.preventDefault();const receipt=form.querySelector('.receipt');try{const response=await fetch('/vnext/designer/feedback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.fromEntries(new FormData(form)))});const data=await response.json();receipt.textContent=data.message||data.error;}catch(error){receipt.textContent='未保存，请检查本机服务后重试。';}}));</script>'''
