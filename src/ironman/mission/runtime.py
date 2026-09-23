"""State-driven local coordinator. Model outputs propose; Registry commits."""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

from ironman.learning.memory import _now
from ironman.learning.workers import obj, STRING, STRINGS, dump
from ironman.storage import VersionConflictError
from .state import StateStore
from .workers import MissionWorker
from .toolkit import Toolkit
from .domains import DomainLearning
from .designer import discover
from . import scopes as scoped


NODE = obj({'id':STRING,'title':STRING,'tool':STRING,'args_json':STRING,
    'requires_all':STRINGS,'requires_any':STRINGS,'conflicts_with':STRINGS,
    'joint_with':STRINGS,'resources':STRINGS,'scopes':STRINGS,
    'alternative_group':STRING,'priority':{'type':'integer'},
    'expected':STRING,'falsified_by':STRING,'assumptions':STRINGS})
PLAN = obj({'goal_summary':STRING,'domains':STRINGS,'needs':STRINGS,
    'nodes':{'type':'array','items':NODE,'maxItems':12},
    'alternatives':{'type':'array','items':obj({'action':STRING,'reason':STRING})},
    'reason':STRING,'unknowns':STRINGS,'hypotheses':STRINGS,'questions':STRINGS,
    'decision':{'enum':['execute','wait','stop']}})
SCOPE = obj({'scope_ids':STRINGS,'reason':STRING})
VNEXT_PLAN = obj({**PLAN['properties'],
    'entities':{'type':'array','items':scoped.ENTITY,'maxItems':12},
    'nodes':{'type':'array','maxItems':12,'items':obj({**NODE['properties'],'contract':scoped.CONTRACT})}})
VNEXT_INSTRUCTION = '''\nFor this scoped architecture, every node has a minimal semantic contract. Choose
motion, audio, designer, writing or observation as appropriate; scene produces actual motion/contact data,
audio produces a real WAV and event mapping, page combines actual interfaces, and compose_inspect observes
playback and synchronized visual state. Do not replace actual audio with a written sound proposal.
Derive stable shared entity IDs and initially sourced fields from the ordinary brief. Confirmed fields need
an exact source_quote from the brief; unprovided weight/mass or measured properties remain unknown, and
creative design choices are hypotheses. Current supplied shared facts supersede older brief facts after
an explicit intervention. Do not reintroduce old values or mutate existing shared facts by paraphrasing.
Each node contract names only needed entity fields, receiver_scope matching its tool domain, concrete
input conditions/units/timing/outputs/checks, assumptions, missing_information and validity conditions.
Declare actual shared prerequisites and joint observation; a composed page depends on actual motion and
audio results. A separate useful explanatory document can remain independent. Reading material is an
audio dependency; motion need not depend on material when contact times/trajectory are unchanged.
Select paths and dependencies yourself, not a fixed sequence. Alternative nodes are only appropriate if
there is a real alternative route; conflicts/resources must name a concrete incompatibility. Domain deep
details stay in local outputs; integration gets public interfaces, current facts and observation summaries.
memory_query should describe the concrete local problem. Missing facts request an exact field, never all
project history. Existing judgments can be rejected or not updated. Do not call design_study without an
actual missing judgment that matters to this project. No inferred human preference or new permission.'''

PLAN_INSTRUCTION = '''You coordinate a bounded local project, not a fixed pipeline.
Given the ordinary brief, available tools, persistent project state, tool observations and prior versions,
propose the useful workgraph NOW. Return the entire revised graph with stable IDs for unchanged work.
Only use installed tools. Infer needs from the job, not keywords; a text task does not need Designer.
Existing persisted Designer judgments can be used automatically on ordinary page work. Study only when a
real judgment gap matters and a permitted reference catalog exists; design_study makes complete original
practice/variant works and observes actual images. Avoid repeating a completed study without new evidence.
The runtime supplies each revising worker with previous_artifact: actual prior successful text contents,
result reference and hashes, not merely a path. Exact artifact revisions can use that supplied content.
When a genuine source gap matters and the brief provides relevant public URLs, use one bounded research
node and make downstream work consume its actual memory packet. No recursive acquisition. Do not research
when direct local operations answer the question. The research domain is the subject (e.g. scheduling).
Choose domain names from scheduling, designer, writing, observation as genuinely needed.
Tool args_json is JSON matching that tool's supplied input schema. Extract concrete schedule sessions
from the brief when requested; do not invent human preferences. Every material numeric/semantic constraint
must be respected in args and artifact. On feedback, preserve independent branches and change affected ones.
requires_all is AND; requires_any is OR (empty means no alternative prerequisite); conflicts_with prevents
concurrent execution; resources also serialize overlapping work. alternative_group marks substitutable
paths; at most one should be used. joint_with lists branches that must be observed TOGETHER, and must
also be in requires_all. Downstream uses exact dependency outputs. Never average local scores.
Use scopes to name semantic inputs e.g. scheduling, visual, content so targeted feedback invalidates only
affected work and actual dependents. A composed page must depend on schedule if a schedule is requested;
inspect should depend on BOTH schedule and page to verify the combination. A useful separate explanation
can be independent. Use deterministic schedule tool for selection, never fabricate solver output in text.
For important choices supply genuinely distinct alternatives and concise public decision reasons.
If observations fail, change causal candidate(s) and inspect again. Preserve the failed version. If no new
evidence or feasible useful action, wait/stop with exact unknowns. Human aesthetic rating is not an engineering
requirement; leave it unknown. Do not ask to approve ordinary local engineering actions. No private reasoning.'''


def signature(node):
    return {k:v for k,v in node.items() if k not in {'status','result','attempt','version','error','execution','replacement_pending'}}


def artifact_hashes(paths):
    return {str(path):hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in paths}


def dependent_closure(nodes, initial):
    affected=set(initial)
    while True:
        expanded=affected | {n['id'] for n in nodes if affected.intersection(n['requires_all']+n['requires_any']+n['joint_with'])}
        if expanded==affected:
            return affected
        affected=expanded


class Mission:
    def __init__(self, root, database, project_id, worker=None, toolkit=None):
        self.root=Path(root).resolve()
        self.store=StateStore(root,database)
        self.id=project_id
        self.state,self.version=self.store.load(project_id)
        self.toolkit=toolkit or Toolkit()
        self.worker=worker
        self._accounted_calls=0
        self._accounted_usage={}
        self.execution=None
        if self.state.get('architecture')=='vnext':
            from .execution import ExecutionLedger
            self.execution=ExecutionLedger(self.store,self.id,self.state.get('execution_batch',self.state['batch_id']),self.state['global_max_calls'])

    @classmethod
    def create(cls,root,database,project_id,brief,*,max_calls=24,max_steps=30,worker=None,toolkit=None,learning_objective=None,
               architecture=None,excluded_scopes=(),batch_id='vnext-alignment',global_max_calls=144):
        if not re.fullmatch(r'[a-zA-Z0-9_-]+',project_id):
            raise ValueError('project ID must be a local identifier')
        identity='mission:'+project_id
        store=StateStore(root,database)
        if store.exists(identity):
            store.close()
            raise VersionConflictError('project already exists; use resume')
        directory=(Path(root)/'projects'/project_id).resolve()
        directory.mkdir(parents=True,exist_ok=True)
        state={'title':project_id,'brief':brief,'constraints':[], 'preferences':[],
            'learning_objective':learning_objective,
            'directory':str(directory),'status':'READY','nodes':[],'domains':[],
            'domain_versions':{},'domain_models':{},'designer':{},'decision_refs':[],
            'observations':[], 'hypotheses':[],'unknowns':[], 'questions':[],
            'feedback':[],'control_sequence':0,'needs_plan':True,
            'calls':0,'usage':{},'steps':0,'budget':{'max_calls':max_calls,'max_steps':max_steps,'concurrency':2,'timeout':240},
            'scope_versions':{},'stop_reason':None,'no_progress':0,'owner_pid':None}
        if architecture=='vnext':
            state.update(scoped.initialize(identity,excluded=excluded_scopes,batch_id=batch_id))
            state['global_max_calls']=global_max_calls
        store.save(identity,state,kind='ProjectOverlay')
        store.event(identity,'mission.created',{'brief':brief,'budget':state['budget'],'mode':'local_experimental'})
        store.close()
        return cls(root,database,identity,worker,toolkit)

    def close(self):
        self.store.close()

    def save(self):
        self.version=self.store.save(self.id,self.state,self.version,'ProjectOverlay',self.state['decision_refs'])
        self.export()

    def export(self):
        if self.state.get('execution_mode')=='LEARNING_ONLY':
            return
        from .console import export_project
        export_project(self.store,self.id,self.state,self.version)

    def emit(self,kind,data):
        return self.store.event(self.id,kind,data)

    def control_pending(self):
        # Called by worker threads; use their own read-only connection.
        if self.state['status'] in {'PAUSED','STOPPED'}:
            return True
        import sqlite3
        with sqlite3.connect(f'file:{self.store.memory.database.as_posix()}?mode=ro',uri=True) as db:
            rows=db.execute("SELECT event_type FROM events WHERE stream_id=? AND stream_sequence>? AND event_type IN ('pause','stop','resume','handoff') ORDER BY stream_sequence",
                            (self.id+':control',self.state['control_sequence'])).fetchall()
        actions=[row[0] for row in rows]
        return 'stop' in actions or bool(actions and actions[-1] in {'pause','handoff'})

    def model(self):
        if self.worker is None:
            remaining=self.state['budget']['max_calls']-self.state['calls']
            requested=self.state.get('worker_configuration',{})
            self.worker=MissionWorker(Path(self.state['directory'])/'workers',remaining,
                                      self.state['budget']['timeout'],self.control_pending,
                                      model=requested.get('model'),
                                      reasoning_effort=requested.get('reasoning_effort'))
            if self.execution:
                self.worker.lifecycle=self.execution.worker_event
            self.emit('resource.route',self.worker.configuration)
        return self.worker

    def account(self):
        if self.execution:
            totals=self.execution.reconcile(Path(self.state['directory'])/'workers')
            self.state['calls']=totals['calls'];self.state['usage']=totals['usage']
            self.state['execution_accounting']=totals
            return
        if self.worker:
            self.state['calls']+=self.worker.calls-self._accounted_calls
            self._accounted_calls=self.worker.calls
            for key,value in self.worker.usage.items():
                self.state['usage'][key]=self.state['usage'].get(key,0)+value-self._accounted_usage.get(key,0)
            self._accounted_usage=dict(self.worker.usage)

    def ask(self,name,instruction,data,schema):
        if self.state['calls']>=self.state['budget']['max_calls']:
            raise RuntimeError('MODEL_CALL_BUDGET_EXHAUSTED')
        try:
            self.state['current_action']=name
            self.save()
            return self.model().ask(f'{self.state["steps"]:03d}-{name}-{uuid.uuid4().hex[:6]}',instruction,data,schema)
        finally:
            self.account()
            self.state['current_action']=None
            self.save()

    def apply_controls(self):
        for event in self.store.controls(self.id,self.state['control_sequence']):
            data=json.loads(json.dumps(dict(event.payload),default=list))
            self.state['control_sequence']=event.stream_sequence
            if event.event_type in {'pause','stop'}:
                self.state['status']='PAUSED' if event.event_type=='pause' else 'STOPPED'
                self.state['stop_reason']=event.event_type
            elif event.event_type=='resume':
                if self.state['status']!='STOPPED':
                    self.state.update(status='READY',stop_reason=None)
            elif event.event_type=='handoff' and self.execution and self.state['status'] not in {'STOPPED','PAUSED'}:
                for node in self.state['nodes']:
                    if node['status']=='RUNNING' and (not data.get('node') or data['node']==node['id']):
                        ref=node.get('execution',{}).get('attempt_ref')
                        if ref:
                            self.execution.checkpoint(ref,'planned_handoff',{'reason':data.get('reason','requested local replacement')})
                            capsule=self.execution.capsule(ref)
                            self.execution.revoke(ref,'planned_handoff')
                            self.state.setdefault('handoffs',[]).append({'node':node['id'],'capsule':capsule,'reason':data.get('reason')})
                        node['status']='PENDING'
                        node['replacement_pending']=True
                self.state.update(status='READY',stop_reason=None)
            elif event.event_type=='fact_change' and self.execution:
                changed=scoped.change_fact(self,data,actor_scope='owner',source_kind=data['source_kind'],source_ref=data['source_ref'])
                impact=scoped.semantic_impact(self.state,[changed])
                affected=dependent_closure(self.state['nodes'],set(impact['definite']+impact['needs_check']))
                impact['propagated']=sorted(affected);self.state['impact_history'].append(impact)
                self.state['preserve_completed']=[n['id'] for n in self.state['nodes'] if n['status']=='COMPLETE' and n['id'] not in affected]
                for node in self.state['nodes']:
                    if node['id'] in affected:
                        if node.get('execution'):self.execution.revoke(node['execution']['attempt_ref'],'semantic_input_changed')
                        node['status']='STALE'
                self.state['needs_plan']=True
                if self.state['status'] not in {'STOPPED','PAUSED'}:self.state.update(status='READY',stop_reason=None)
                self.emit('semantic.invalidated',impact)
            elif event.event_type=='budget':
                if data['max_calls']<self.state['calls']:
                    raise ValueError('budget cannot erase already used calls')
                self.state['budget']['max_calls']=data['max_calls']
                self.state['budget']['max_steps']=data.get('max_steps',self.state['budget']['max_steps'])
                if self.worker is not None:
                    self.worker.max_calls=self.worker.calls+data['max_calls']-self.state['calls']
                self.emit('budget.changed',{'new':self.state['budget'],'used_calls_preserved':self.state['calls'],'reason':data['reason']})
            elif event.event_type=='feedback':
                record={'event_id':event.event_id,'source_kind':data['source_kind'],'text':data['text'],
                        'scopes':data['scopes'],'source_ref':data.get('source_ref','local control input'),
                        'kind':data.get('kind','constraint')}
                if self.execution:
                    supersedes=scoped.validate_supersession(self.state,data)
                    record.update(status='current',supersedes_event_ids=sorted(supersedes))
                    for collection in ('feedback','constraint_records','preferences','observations'):
                        for old in self.state.get(collection,[]):
                            if old.get('event_id') in supersedes:
                                old.update(status='superseded',superseded_by=event.event_id)
                    for item in self.state['memory_catalog']:
                        if item.get('source_kind')=='human' and item.get('status')=='explicit_future_default':
                            body,_=self.store.load(item['id'])
                            if supersedes.intersection(body['body'].get('evidence',[])):
                                item['status']='superseded'
                self.state['feedback'].append(record)
                record['project']=self.id
                if self.execution:
                    self.state['constraint_records'].append(record)
                if record['kind']=='preference':
                    preference={**record,'person':data.get('person','current_owner'),'status':'project_applied'}
                    self.state['preferences'].append(preference)
                    if data.get('apply_to_future') and record['source_kind']=='human':
                        if self.execution:
                            for scope in data['scopes']:
                                if scope!='project':scoped.put_memory(self,{'title':'Scoped explicit preference','text':data['text'],
                                    'keywords':data['text'].split(),'evidence':[event.event_id]},scope=scope,
                                    source_kind='human',status='explicit_future_default')
                        else:
                            ref='preference:'+uuid.uuid4().hex
                            self.store.save(ref,{**preference,'title':'Explicit scoped preference','status':'explicit_future_default'},provenance=[event.event_id])
                elif record['kind']=='observation':
                    self.state['observations'].append({'kind':'external_observation',**record})
                else:
                    self.state['constraints'].append(data['text'])
                for scope in data['scopes']:
                    self.state['scope_versions'][scope]=self.state['scope_versions'].get(scope,0)+1
                if self.execution:
                    targets={n['id'] for n in self.state['nodes'] if any(
                        scope=='project' or scoped.under(n.get('contract',{}).get('receiver_scope',''),scope)
                        or scoped.under(scope,n.get('contract',{}).get('receiver_scope',''))
                        for scope in data['scopes'])}
                else:
                    targets={n['id'] for n in self.state['nodes'] if set(n['scopes'])&set(data['scopes'])}
                affected=dependent_closure(self.state['nodes'],targets)
                self.state['preserve_completed']=[n['id'] for n in self.state['nodes'] if n['status']=='COMPLETE' and n['id'] not in affected]
                for node in self.state['nodes']:
                    if node['id'] in affected:
                        if self.execution and node.get('execution'):
                            self.execution.revoke(node['execution']['attempt_ref'],'scoped_feedback_changed')
                        node['status']='STALE'
                self.state['needs_plan']=True
                if self.state['status'] not in {'PAUSED','STOPPED'}:
                    self.state.update(status='READY',stop_reason=None)
                self.emit('project.invalidated',{'feedback':record,'affected':sorted(affected),
                          'preserved':[n['id'] for n in self.state['nodes'] if n['id'] not in affected]})
            self.emit('control.applied',{'action':event.event_type,'sequence':event.stream_sequence})
        self.save()

    def source(self,name,body,domain="mission"):
        data=json.dumps(body,ensure_ascii=False,indent=2).encode('utf-8')
        digest=hashlib.sha256(data).hexdigest()
        identity=f'source.mission.{domain}.{digest[:24]}'
        if self.store.exists(identity):
            prior=self.store.memory._payload(identity)
            if prior['content_hash']!=digest:
                raise VersionConflictError('source identity already registered with different content')
            return identity
        path=Path(self.state['directory'])/'evidence'/f'{name}-{digest[:12]}.json'
        path.parent.mkdir(exist_ok=True)
        if not path.exists():
            path.write_bytes(data)
        payload={'object_id':identity,'object_type':'SourceEvidence','version':'0.1.0','semantic_owner':domain,
                 'lifecycle_state':'quarantine','created_at':_now(),'created_by':'jervis_mission_runtime',
                 'provenance_refs':[self.id],'source_type':'other','source_locator':str(path),
                 'captured_at':_now(),'usage_status':'permitted','discovery_reason':'targeted local operation comparison',
                 'discovery_channel':'runtime domain laboratory','raw_content_ref':str(path),'content_ref':str(path),
                 'content_hash':digest,'human_name':name}
        self.store.memory.register_source(payload)
        return identity

    def form_domain(self, domain='scheduling'):
        adapters=getattr(self.toolkit,'domains',Toolkit.domains)
        return DomainLearning(self,domain,adapters[domain],dependent_closure).form()

    def revise_domain(self, proposal=None, rollback_to=None, apply_to_project=True, domain='scheduling'):
        adapters=getattr(self.toolkit,'domains',Toolkit.domains)
        return DomainLearning(self,domain,adapters[domain],dependent_closure).revise(
            proposal,rollback_to,apply_to_project)

    def review_local_experience(self,domain='designer'):
        """Use the existing open-form apprenticeship admission in this project only."""
        if not self.execution or self.state['status']!='COMPLETE':
            raise ValueError('scoped reflection requires a completed vnext project')
        from .design_judgment import MODEL, Adapter
        schema=obj({'decision':{'type':'string','enum':['experimental','no_update','rejected']},
                    'reason':STRING,'scope':STRING,'model':MODEL,'limitations':STRINGS})
        observed=[{'node':n['id'],'result_ref':n.get('result',{}).get('result_ref'),
                   'observations':n.get('result',{}).get('observations',[])} for n in self.state['nodes']
                  if n['tool'] in {'compose_inspect','inspect'} and n['status']=='COMPLETE']
        response=self.ask('local-experience-review',
            '''Review this completed project's actual tool observations and prior conditional Designer judgments.
Propose only scoped lessons supported by supplied evidence, or no_update/rejected when there is no reliable new
judgment. Functional playback success does not establish aesthetic or human preference improvement. Do not
invent a lesson to fill fields or generalize another domain's internals. Keep existing uncertainty and failure
conditions. Scope is this project and the designer domain, not all future projects. Return a proposed open-form
judgment model; if no_update, preserve the supplied active judgments. Evidence refs must refer to supplied results.''',
            {'project':self.id,'scope':'designer','brief':self.state['brief'],'observations':observed,
             'prior_model':self.state['domain_models'].get(domain,{'judgments':[]}),
             'prior_scope':self.state.get('domain_imports',{}).get(domain),'human_evidence':'AWAITING_HUMAN_EVIDENCE'},schema)
        refs=[x['result_ref'] for x in observed if x['result_ref']]
        learner=DomainLearning(self,domain,Adapter(),dependent_closure)
        # Create a project-local base by reference; never rewrite the imported object.
        if not self.store.exists(learner.identity):
            self.store.save(learner.identity,{'title':Adapter.title,'active':self.state['domain_models'].get(domain,{'judgments':[]}),
                'scope':self.id+'/designer','status':'imported_candidate','evidence':refs,
                'import':self.state.get('domain_imports',{}).get(domain)},provenance=refs)
        revision=learner.record_observed(response['model'],{'decision':response['decision'],'reason':response['reason'],
            'limitations':response['limitations'],'evidence_kind':'MODEL_JUDGMENT','human_evidence':'AWAITING_HUMAN_EVIDENCE'},refs,apply_to_project=True)
        metadata=scoped.put_memory(self,{'title':'Composition playback experience','keywords':['playback','contact','preview','composition'],
            'decision':response['decision'],'reason':response['reason'],'model':response['model'],
            'applies_when':['this project, this observed composition'],'fails_when':['unobserved projects or human preference claims'],
            'evidence':refs},scope='designer',status='experimental' if response['decision']=='experimental' else response['decision'])
        self.state['local_experience_review']={'decision':response['decision'],'reason':response['reason'],
                                             'domain_revision':revision['version'],'memory':metadata}
        self.save()
        return self.state['local_experience_review']

    def reconcile_current_knowledge(self):
        """Resolve planning questions against committed observations, not prose alone."""
        observed=[{'node':n['id'],'result_ref':n['result']['result_ref'],
                   'observations':n['result'].get('observations',[]),
                   'output':n['result'].get('output',{})} for n in self.state['nodes']
                  if n['status']=='COMPLETE' and n['tool'] in {'inspect','compose_inspect'}]
        if not observed:
            return None
        # Rendering measurements are a different committed interface from the
        # browser behavior check. Supply that small slice when resolving what
        # was generated/measured; don't infer it from a generic playback PASS.
        observed += [{'node':n['id'],'result_ref':n['result']['result_ref'],
                      'observations':n['result'].get('observations',[]),
                      'output':{'interface':n['result']['output']['interface']}}
                     for n in self.state['nodes'] if n['status']=='COMPLETE' and n['tool']=='audio'
                     and n['result'].get('output',{}).get('interface')]
        refs=[x['result_ref'] for x in observed]
        if not self.execution or not observed or not self.state['unknowns'] or self.state.get('knowledge_reconciled_for')==refs:
            return None
        prior=list(dict.fromkeys(self.state['unknowns']))
        schema=obj({'assessments':{'type':'array','items':obj({'prior_unknown':STRING,
            'status':{'type':'string','enum':['resolved','still_unknown','disputed']},
            'evidence_refs':STRINGS,'reason':STRING})}})
        result=self.ask('reconcile-current-questions',
            '''Review every supplied prior planning question exactly once against the committed tool observations.
Resolve only what the supplied actual observations establish; cite supplied result refs for each resolution.
Keep unsupported claims still_unknown or disputed. A working file or digital measurement never proves human
listening, aesthetic preference, physical realism, or general usability. Unknown entity fields remain unknown.
This is an evidence interpretation for the current project, not a new fact, learning promotion or instruction.''',
            {'project':self.id,'scope':'integration','prior_unknowns':prior,'observed':observed,
             'unknown_entity_fields':[e['entity_id']+'.'+name for e in self.state['entities'].values()
                                      for name,f in e['fields'].items() if f['status']=='unknown']},schema)
        assessed=result['assessments']
        if len(assessed)!=len(prior) or {x['prior_unknown'] for x in assessed}!=set(prior):
            raise ValueError('question review must preserve an assessment for every prior question')
        if any(not set(x['evidence_refs'])<=set(refs) or (x['status']=='resolved' and not x['evidence_refs']) for x in assessed):
            raise ValueError('resolved question requires a current committed observation reference')
        record={'evidence_kind':'MODEL_JUDGMENT_OVER_TOOL_OBSERVATIONS','prior_unknowns':prior,
                'assessments':assessed,'result_refs':refs}
        self.state.setdefault('knowledge_history',[]).append(record)
        self.state['unknowns']=[x['prior_unknown'] for x in assessed if x['status']!='resolved']
        self.state['knowledge_reconciled_for']=refs
        self.emit('knowledge.reconciled',record)
        self.save()
        return record

    def setup_domains(self,plan):
        for domain in getattr(self.toolkit,'domains',Toolkit.domains):
            if domain in plan['domains'] and domain not in self.state['domain_versions']:
                self.form_domain(domain)
        if 'designer' in plan['domains'] and not self.state['designer']:
            packet=discover(self.store.memory,self.state['brief'],plan['needs'])
            if packet['status']=='needs_scope':
                scope=self.ask('designer-scope','Choose appropriate exact task topology, style family, page archetype and capability IDs from this existing taxonomy for the brief. Guidance is advisory; no assumption of user taste.',
                               {'brief':self.state['brief'],'taxonomy':packet['taxonomy']},SCOPE)
                packet=discover(self.store.memory,self.state['brief'],scope['scope_ids'])
                packet['selection_reason']=scope['reason']
            self.state['designer']=packet
            self.emit('capability.discovered',packet)
        elif 'designer' not in plan['domains']:
            self.emit('capability.not_needed',{'domain':'designer','reason':plan['reason']})

    def replan(self):
        data={'brief':self.state['brief'],'constraints':self.state['constraints'],
              'learning_objective':self.state.get('learning_objective'),
              'preferences':self.state['preferences'],
              'nodes':self.state['nodes'],'observations':self.state['observations'][-6:],
              'feedback':self.state['feedback'],'tools':self.toolkit.catalog,
              'domain_versions':self.state['domain_versions'],'known_unknowns':self.state['unknowns']}
        from .design_judgment import catalog_for
        catalog=catalog_for(self.root,self.state.get('designer_reference_catalog'),self.id)
        data['reference_catalog']={key:[{field:item.get(field) for field in ('id','title','selection_reason','url')}
                                      for item in catalog.get(key,[])] for key in ('works','methods')}
        if self.execution:
            current=scoped.effective(self.state['constraint_records'])
            data['constraints']=[r['text'] for r in current if r['kind']=='constraint']
            data['preferences']=scoped.effective(self.state['preferences'])
            data['feedback']=scoped.effective(self.state['feedback'])
            data['observations']=scoped.effective(self.state['observations'])[-6:]
            data['entities']=self.state['entities'];data['scope_policy']=self.state['scope_policy']
            data['impact']=self.state['impact_history'][-2:]
            data['nodes']=[{**signature(n),'status':n['status'],'result_ref':n.get('result',{}).get('result_ref'),
                'interface':n.get('result',{}).get('output',{}).get('interface',{})} for n in self.state['nodes']]
        plan=self.ask('plan',PLAN_INSTRUCTION+(VNEXT_INSTRUCTION if self.execution else ''),data,VNEXT_PLAN if self.execution else PLAN)
        if self.execution:scoped.adopt_initial_facts(self,plan['entities'])
        names={t['name'] for t in self.toolkit.catalog}
        old={n['id']:n for n in self.state['nodes']}
        protected=set(self.state.get('preserve_completed',[]))
        for identity in protected-{n['id'] for n in plan['nodes']}:
            plan['nodes'].append(deepcopy(signature(old[identity])))
        ids={n['id'] for n in plan['nodes']}
        if len(ids)!=len(plan['nodes']):
            raise ValueError('duplicate workgraph node IDs')
        for node in plan['nodes']:
            if not re.fullmatch(r'[a-zA-Z0-9_-]+',node['id']) or node['tool'] not in names:
                raise ValueError('invalid node identifier or uninstalled tool')
            refs=node['requires_all']+node['requires_any']+node['conflicts_with']+node['joint_with']
            if not set(refs)<=ids or node['id'] in refs:
                raise ValueError('workgraph references unknown or own node')
            if not set(node['joint_with'])<=set(node['requires_all']):
                raise ValueError('joint observation must bind all jointly evaluated inputs')
            if 'args_json' in node:
                node['args']=json.loads(node.pop('args_json'))
            from jsonschema import Draft202012Validator
            Draft202012Validator(next(x['input'] for x in self.toolkit.catalog if x['name']==node['tool'])).validate(node['args'])
        self.setup_domains(plan)
        rows=[] if self.execution else self.store.memory._index.execute("SELECT object_id FROM objects WHERE object_id LIKE 'preference:%' ORDER BY updated_at DESC LIMIT 20").fetchall()
        for row in rows:
            preference,_=self.store.load(row[0])
            if (preference.get('status')=='explicit_future_default' and preference.get('source_kind')=='human'
                and set(preference['scopes'])&(set(plan['domains'])|{scope for node in plan['nodes'] for scope in node['scopes']})
                and not any(p['event_id']==preference['event_id'] for p in self.state['preferences'])):
                self.state['preferences'].append(preference)
                self.emit('preference.reused',{'id':row[0],'source':preference['event_id'],'scope':preference['scopes']})
        old={n['id']:n for n in self.state['nodes']}
        # A feedback replan cannot spend work again just because a model paraphrases
        # an unrelated completed instruction. New dependencies still invalidate it.
        protected=set(self.state.get('preserve_completed',[]))
        for index,node in enumerate(plan['nodes']):
            if node['id'] in protected and node['id'] in old:
                previous=old[node['id']]
                if set(node['requires_all'])==set(previous['requires_all']) and set(node['requires_any'])==set(previous['requires_any']):
                    plan['nodes'][index]=deepcopy(signature(previous))
        changed={n['id'] for n in plan['nodes'] if n['id'] not in old or signature(n)!=signature(old[n['id']]) or old[n['id']]['status'] in {'STALE','FAILED'}}
        affected=dependent_closure(plan['nodes'],changed)
        nodes=[]
        for node in plan['nodes']:
            previous=old.get(node['id'])
            if previous and node['id'] not in affected:
                nodes.append(previous)
            else:
                node.update(status='PENDING',attempt=previous.get('attempt',0) if previous else 0,
                            version=(previous.get('version',0)+1) if previous else 1)
                nodes.append(node)
        record={'title':'Workgraph decision','plan':plan,'changed':sorted(affected),
                'preserved':[n['id'] for n in nodes if n['id'] not in affected],
                'observed_versions':self.state['domain_versions']}
        ref=self.id+':decision:'+uuid.uuid4().hex
        self.store.save(ref,record,provenance=[self.id])
        self.state['decision_refs'].append(ref)
        self.state.update(nodes=nodes,domains=plan['domains'],unknowns=plan['unknowns'],
                          hypotheses=plan['hypotheses'],questions=plan['questions'],needs_plan=False)
        if plan['decision']!='execute':
            self.state.update(status='WAITING' if plan['decision']=='wait' else 'STOPPED',stop_reason=plan['reason'])
        self.emit('graph.revised',record)
        self.save()

    def ready(self):
        nodes=self.state['nodes']
        done={n['id'] for n in nodes if n['status']=='COMPLETE'}
        used_groups={n['alternative_group'] for n in nodes if n['status']=='COMPLETE' and n['alternative_group']}
        ready=[]
        for n in sorted(nodes,key=lambda n:-n['priority']):
            if n['status']!='PENDING':
                continue
            if n['alternative_group'] and n['alternative_group'] in used_groups:
                n['status']='NOT_NEEDED'
                if self.execution:self.emit('graph.alternative_not_needed',{'node':n['id'],'group':n['alternative_group'],
                    'selected':[x['id'] for x in nodes if x['status']=='COMPLETE' and x['alternative_group']==n['alternative_group']]})
                continue
            if not set(n['requires_all'])<=done or (n['requires_any'] and not done.intersection(n['requires_any'])):
                continue
            blockers=[x['id'] for x in ready if set(n['resources'])&set(x['resources']) or n['id'] in x['conflicts_with']
                      or x['id'] in n['conflicts_with'] or (n['alternative_group'] and n['alternative_group']==x['alternative_group'])]
            if blockers:
                if self.execution:self.emit('graph.conflict_deferred',{'node':n['id'],'with':blockers,
                    'resources':n['resources'],'declared_conflicts':n['conflicts_with'],'alternative_group':n['alternative_group']})
                continue
            ready.append(n)
            if len(ready)>=self.state['budget']['concurrency']:
                break
        return ready

    def work_order(self,node):
        if self.state.get('execution_mode')=='LEARNING_ONLY' and node['tool']!='design_read':
            raise PermissionError('LEARNING_ONLY permits source reading only')
        by_id={n['id']:n for n in self.state['nodes']}
        dependencies=node['requires_all']+[x for x in node['requires_any'] if by_id[x]['status']=='COMPLETE']
        inputs={x:deepcopy(by_id[x]['result']) for x in dependencies}
        bound_files=artifact_hashes([path for result in inputs.values() for path in result['artifacts']])
        for result in inputs.values():
            if any(bound_files.get(path)!=digest for path,digest in result.get('artifact_hashes',{}).items()):
                raise VersionConflictError('dependency artifact changed outside its registered result')
        baseline=self.previous_artifact(node['id'])
        bound_files.update({p['path']:p['sha256'] for p in baseline.get('files',[])})
        context=None;execution={}
        if self.execution:
            context,inputs=scoped.compile_context(self,node,inputs)
            node['attempt']+=1
            identity=self.id+':work:'+node['id']+f':{node["version"]}:{node["attempt"]}'
            preliminary={'title':node['title'],'project':self.id,'node':node['id'],'node_version':node['version'],
                'attempt':node['attempt'],'generation':node['attempt'],'tool':node['tool'],
                'input_versions':{x:by_id[x]['version'] for x in dependencies},
                'entity_versions':context['semantic_contract']['entity_versions'],'scope':context['scope'],
                'call_name':f'{node["id"]}-v{node["version"]}-a{node["attempt"]}',
                'directory':str(Path(self.state['directory'])/'work'/node['id']/f'{node["version"]}-{node["attempt"]}'),
                'input_artifact_hashes':bound_files}
            execution=self.execution.begin_attempt(identity,preliminary)
            node['execution']=execution;node['status']='RUNNING'
            self.execution.checkpoint(execution['attempt_ref'],'context_compiled',{'packet':context,'input_hashes':bound_files})
            self.emit('context.compiled',{'node':node['id'],'attempt':node['attempt'],'packet':context})
            self.save()
        application={};design_images=[]
        if node['tool']=='inspect':
            application={name:value['output']['judgment_application'] for name,value in inputs.items()
                         if value.get('output',{}).get('judgment_application')}
        if node['tool']=='page' and self.state['domain_models'].get('designer',{}).get('judgments'):
            from .design_judgment import applicability
            try:
                application=applicability(self.model(),f"{node['id']}-v{node['version']}-a{node['attempt'] if self.execution else node['attempt']+1}",self.state['brief'],
                                          self.state['domain_models']['designer'],self.state['domain_versions']['designer'])
            finally:
                self.account()
            for example in self.state['domain_models']['designer'].get('examples',[]):
                if example['id'] in application['example_refs'] or example['path'] in application['example_refs']:
                    if artifact_hashes([example['path']])[example['path']]!=example['sha256']:
                        raise VersionConflictError('judgment example changed outside its source binding')
                    design_images.append(example['path']);bound_files[example['path']]=example['sha256']
            self.emit('judgment.applied',{'node':node['id'],**application,'images':design_images})
        if not self.execution:node['attempt']+=1
        directory=Path(self.state['directory'])/'work'/node['id']/f'{node["version"]}-{node["attempt"]}'
        order={'title':node['title'],'project':self.id,'node':node['id'],'node_version':node['version'],'attempt':node['attempt'],
            'tool':node['tool'],'args':node['args'],'input_versions':{x:by_id[x]['version'] for x in dependencies},
            'scope_versions':{x:self.state['scope_versions'].get(x,0) for x in node['scopes']},
            'domain_versions':self.state['domain_versions'],'domain_models':self.state['domain_models'],
            'designer':self.state['designer'] if node['tool']=='page' else {},
            'directory':str(directory),'inputs':inputs,'input_artifact_hashes':bound_files,'brief':self.state['brief'],'constraints':self.state['constraints'],
            'preferences':self.state['preferences'],'previous_artifact':baseline,
            'design_application':application,'design_images':design_images,'visual_observation':True,
            'permissions':['write_assigned_directory','installed_tool_only'],'budget':self.state['budget'],
            'expected':node['expected'],'falsified_by':node['falsified_by'],'assumptions':node['assumptions'],
            'call_name':f'{node["id"]}-v{node["version"]}-a{node["attempt"]}'}
        if context:
            scope=context['scope'].split('/')[0]
            order.update(context_packet=context,semantic_contract=context['semantic_contract'],
                entity_versions=context['semantic_contract']['entity_versions'],execution=execution,
                constraints=[x['text'] for x in context['constraints'] if x.get('kind')!='preference'],
                preferences=context['preferences'],
                domain_models={k:v for k,v in self.state['domain_models'].items() if k==scope},
                domain_versions={k:v for k,v in self.state['domain_versions'].items() if k==scope})
        identity=self.id+':work:'+node['id']+f':{node["version"]}:{node["attempt"]}'
        if node['tool']=='design_read':
            order['learning_source']=self.state['learning_queue'][node['args']['source_index']]
            order['learning_scope']=self.state['learning_scope']
        self.store.save(identity,order,provenance=[self.id,*self.state['decision_refs'][-1:]])
        node['status']='RUNNING'
        if self.execution:
            self.emit('tool.operation_started',{'operation_id':execution['attempt_ref']+':tool','tool':order['tool'],
                'directory':order['directory'],'effect_scope':'new assigned attempt directory only'})
        self.emit('worker.dispatched',{'order':identity,'node':node['id'],'tool':node['tool'],'inputs':order['input_versions']})
        return identity,deepcopy(order)

    def previous_artifact(self,node_id):
        """Supply actual prior content, not a file-read instruction to a tool-free worker."""
        prefix=self.id+':work:'+node_id+':'
        rows=self.store.memory._index.execute("SELECT object_id FROM objects WHERE substr(object_id,1,?)=? AND object_id LIKE '%:result' ORDER BY rowid DESC LIMIT 12",(len(prefix),prefix)).fetchall()
        project=Path(self.state['directory']).resolve()
        for row in rows:
            body,_=self.store.load(row[0])
            result=body.get('result',{})
            if body.get('commit_status')!='APPLIED' or not result.get('passed'):
                continue
            files=[]
            for ref in result.get('artifacts',[]):
                path=Path(ref).resolve()
                if not path.is_relative_to(project) or path.suffix not in {'.html','.md','.txt','.json'} or not path.is_file():
                    continue
                data=path.read_bytes()
                if not data.strip():
                    continue
                digest=hashlib.sha256(data).hexdigest()
                if str(path) in result.get('artifact_hashes',{}) and result['artifact_hashes'][str(path)]!=digest:
                    raise VersionConflictError('prior artifact content changed outside its version')
                if len(data)>262144 and not self.execution:
                    raise ValueError('prior artifact exceeds supported 256 KiB worker input; no silent truncation')
                content=data.decode('utf-8')
                if self.execution and path.suffix=='.html':
                    content=re.sub(r'data:audio/[^;"\s]+;base64,[A-Za-z0-9+/=]+','JERVIS_AUDIO_URI',content)
                if len(content)>262144:
                    raise ValueError('prior text artifact exceeds supported context; request a narrower interface')
                files.append({'path':str(path),'sha256':digest,'content':content,
                              **({'media_payload_notice':'Embedded audio omitted from text; rebound from actual interface media.'} if self.execution else {})})
            if files:
                return {'result_ref':row[0],'files':files,'status':'actual_prior_content'}
        return {}

    def commit(self,identity,order,result):
        self.apply_controls()
        self.state,self.version=self.store.load(self.id)
        by_id={n['id']:n for n in self.state['nodes']}
        node=by_id.get(order['node'])
        if node is None:
            self.emit('worker.result_rejected',{'order':identity,'reason':'node removed by current workgraph'})
            return
        changed_files=[path for path,digest in order.get('input_artifact_hashes',{}).items()
                       if not Path(path).is_file() or hashlib.sha256(Path(path).read_bytes()).hexdigest()!=digest]
        changed_outputs=[path for path,digest in result.get('artifact_hashes',{}).items()
                         if not Path(path).is_file() or hashlib.sha256(Path(path).read_bytes()).hexdigest()!=digest]
        if changed_outputs:
            result={**result,'passed':False,'error':'output artifact changed before commit',
                    'observations':result['observations']+[{'kind':'output_artifact_changed','files':changed_outputs}]}
        stale=(node['version']!=order['node_version'] or node['attempt']!=order['attempt'] or node['status']=='STALE'
               or bool(changed_files)
               or any(by_id[x]['version']!=v or by_id[x]['status']!='COMPLETE' for x,v in order['input_versions'].items())
               or any(self.state['domain_versions'].get(x)!=v for x,v in order['domain_versions'].items())
               or any(self.state['scope_versions'].get(x,0)!=v for x,v in order['scope_versions'].items()))
        if self.execution:
            stale=stale or bool(scoped.changed_bindings(self.state,order.get('entity_versions',{}))) or not self.execution.eligible(
                order['execution']['attempt_ref'],order['execution']['generation'])
        stopped=self.state['status'] in {'PAUSED','STOPPED'}
        if not stale and not stopped and result.get('passed') and result.get('domain_proposal'):
            proposal=result['domain_proposal']
            adapter=self.toolkit.domains[proposal['domain']]
            revision=DomainLearning(self,proposal['domain'],adapter,dependent_closure).record_observed(
                proposal['model'],proposal['assessment'],proposal['evidence'],apply_to_project=True)
            result['output']['revision']={'version':revision['version'],'status':revision['status'],'domain':proposal['domain']}
        result_ref=identity+':result'
        if self.store.exists(result_ref):
            result_ref=identity+':late:'+uuid.uuid4().hex
            stale=True
        self.store.save(result_ref,{'title':'Work result','order':identity,'result':result,
                                    'commit_status':'STALE' if stale else 'CANCELLED' if stopped else 'APPLIED'},provenance=[identity])
        if stale or stopped:
            if node['version']==order['node_version'] and node['attempt']==order['attempt']:
                replacing=node.pop('replacement_pending',False)
                node['status']='PENDING' if replacing else 'STALE' if stale else 'PENDING'
                if changed_files:
                    self.state['observations'].append({'kind':'artifact_changed','files':changed_files,'result':'rejected; regenerate affected inputs'})
                    self.state['needs_plan']=True
            self.emit('worker.result_rejected',{'order':identity,'reason':'stale inputs' if stale else 'control stop','artifacts':result.get('artifacts',[])})
        else:
            node['result']={**result,'result_ref':result_ref}
            node['status']='COMPLETE' if result['passed'] else 'FAILED'
            node['error']=result.get('error')
            if self.execution and result['passed']:
                for observation in self.state['observations']:
                    if observation.get('node')==node['id'] and observation.get('kind')=='failure':
                        observation.update(status='resolved',resolved_by=result_ref)
            self.state['observations'].extend(result['observations'])
            current=result.get('output',{}).get('current_knowledge')
            if current:
                self.state.setdefault('knowledge_history',[]).append({'unknowns':self.state['unknowns'],
                    'status':'superseded_by_observation','evidence_ref':result_ref})
                self.state['unknowns']=current['unknowns']
                self.emit('knowledge.updated',{'evidence_ref':result_ref,**current})
            self.state['no_progress']=0 if result['passed'] else self.state['no_progress']+1
            if result['passed'] and order['tool']=='design_read':
                output=result['output'];source=output['source']
                batch_events=self.store.registry.events.read_stream(
                    'execution:'+self.state.get('execution_batch',self.state['batch_id']))
                global_calls=sum(event.event_type=='model.prepared' for event in batch_events)
                checkpoint={
                    'round':source.get('round'),
                    'current_question':self.state['learning_question'],
                    'continuation_reason':source.get('continuation_reason',''),
                    'source':{'id':source['id'],'title':source['title'],'url':source['url'],
                              'items':source.get('source_items',[]),'bindings':source['bindings']},
                    'actual_input':output['actual_input'],
                    'actual_coverage':output['notes']['coverage'],
                    'decision':output['effective_decision'],
                    'object_change':output.get('revision'),
                    'basis':output['notes']['reason'],
                    'unknowns':output['current_knowledge']['unknowns'],
                    'next_reason':output['notes']['next_basis'],
                    'remaining_budget':{
                        'branch_calls':max(0,self.state['budget']['max_calls']-self.state['calls']),
                        'batch_calls':max(0,self.state['global_max_calls']-global_calls),
                        'usage':deepcopy(self.state['usage'])},
                    'result_ref':result_ref}
                self.emit('learning.round_checkpoint',checkpoint)
                if self.execution:
                    self.execution.checkpoint(order['execution']['attempt_ref'],
                                              'learning_round_checkpoint',checkpoint)
            if not result['passed']:
                transport_failure=self.execution and str(result.get('error','')).startswith(
                    ('CODEX_WORKER_EXIT_','WORKER_TIMED_OUT','WORKER_CANCELLED'))
                if transport_failure:
                    # A dead tool-free worker supplies no evidence against its
                    # dependencies. Reconstruct only this attempt from current
                    # bindings; preserve successful branches and charge the loss.
                    node['status']='PENDING'
                    self.emit('worker.replacement_queued',{'node':node['id'],'failed_result':result_ref,
                        'reason':'transport failure; semantic inputs remain current'})
                else:
                    self.state['needs_plan']=True
                self.state['observations'].append({'node':node['id'],'kind':'failure','result':result})
            self.emit('worker.result_applied',{'order':identity,'result':result_ref,'status':node['status']})
        if self.execution:
            self.execution.checkpoint(order['execution']['attempt_ref'],'result_rejected' if stale or stopped else 'result_committed',
                {'result_ref':result_ref,'artifacts':result.get('artifacts',[]),'passed':result.get('passed'),'unknowns':result.get('unknowns',[])})
        self.save()

    def execute_tool(self,order,worker):
        if order['tool']=='design_read':
            from .design_read import read_source
            result=read_source(self,order,worker)
        elif order['tool']=='design_study':
            from .design_judgment import study
            result=study(self,order,worker)
        elif order['tool']=='research':
            from .research import research
            output=research(self.store.memory,worker,Path(order['directory']),order['args'],self.emit)
            output['memory_packet']=self.store.memory.retrieve(order['args']['question'],domain=order['args']['domain'],mode='eval',limit=4,max_chars=12000)
            path=Path(order['directory'])/'research-result.json'
            dump(path,output)
            result={'artifacts':[str(path)],'output':output,'observations':[{'kind':'source_acquisition','source_ids':output['source_ids'],'failures':output['failures']}],
                    'passed':bool(output['source_ids']),'unknowns':output['gaps']}
        else:
            result=self.toolkit.execute(order,worker)
        for path in result['artifacts']:
            if not Path(path).resolve().is_relative_to(Path(order['directory']).resolve()):
                raise ValueError('tool returned an artifact outside assigned work directory')
        result['artifact_hashes']=artifact_hashes(result['artifacts'])
        return result

    def execute(self,max_cycles=None):
        if self.state['status']=='STOPPED':
            return self.state
        # A live coordinator owns scheduling; process death permits explicit recovery.
        if self.state.get('owner_pid') and self.state['owner_pid']!=os.getpid():
            import psutil
            if psutil.pid_exists(self.state['owner_pid']):
                raise RuntimeError('another coordinator process is still live')
        self.state['owner_pid']=os.getpid()
        if self.execution:
            self.account()
            recovered=self.execution.recover()
            self.state['recovery']=recovered
        for n in self.state['nodes']:
            if n['status']=='RUNNING':
                if self.execution and n.get('execution'):
                    self.execution.revoke(n['execution']['attempt_ref'],'coordinator_or_worker_interrupted')
                n['status']='PENDING'
                self.emit('worker.interrupted',{'node':n['id'],'recovery':'new attempt in separate output directory; no external side effects'})
        self.save()
        cycles=0
        try:
            while self.state['steps']<self.state['budget']['max_steps']:
                self.apply_controls()
                if self.state['status'] in {'PAUSED','STOPPED','WAITING'}:
                    break
                if max_cycles is not None and cycles>=max_cycles:
                    self.state['status']='READY'
                    break
                cycles+=1
                if self.state['needs_plan'] and self.state['calls']>=self.state['budget']['max_calls']:
                    self.state.update(status='WAITING',stop_reason='MODEL_CALL_BUDGET_EXHAUSTED')
                    break
                if self.state['no_progress']>=3:
                    self.state.update(status='WAITING',stop_reason='three failed actions without useful progress; retained hypotheses and outputs')
                    break
                self.state['steps']+=1
                if self.state['needs_plan']:
                    if self.state.get('execution_mode')=='LEARNING_ONLY':
                        self.state.update(status='WAITING',stop_reason='LEARNING_QUEUE_REQUIRES_REPAIR')
                        break
                    self.replan()
                    continue
                ready=self.ready()
                if not ready:
                    if self.state.get('execution_mode')=='LEARNING_ONLY':
                        self.state.update(status='WAITING',stop_reason='SOURCE_QUEUE_BOUNDARY; retain question and progress, not domain completion')
                        self.emit('learning.checkpoint',{'reason':self.state['stop_reason'],'calls':self.state['calls']})
                        break
                    done=all(n['status'] in {'COMPLETE','NOT_NEEDED'} for n in self.state['nodes']) and bool(self.state['nodes'])
                    if done and self.execution and self.state['calls']<self.state['budget']['max_calls']:
                        self.reconcile_current_knowledge()
                    completed=[n.get('result',{}).get('result_ref') for n in self.state['nodes'] if n['status']=='COMPLETE']
                    for domain in set(self.state['domains']) & set(getattr(self.toolkit,'domains',{})):
                        if not getattr(self.toolkit.domains[domain],'reflect_on_completion',True):
                            continue
                        marker=self.state.get('domain_reflected_for',{}).get(domain)
                        if done and marker!=completed and self.state['calls']<self.state['budget']['max_calls']:
                            revision=self.revise_domain(apply_to_project=False,domain=domain)
                            self.state.setdefault('domain_reflected_for',{})[domain]=completed
                            self.state.setdefault('subsequent_domain_revisions',{})[domain]={'version':revision['version'],'status':revision['status']}
                    self.state.update(status='COMPLETE' if done else 'WAITING',stop_reason='scope execution complete; human effect pending' if done else 'no feasible ready branch')
                    self.emit('mission.stopped',{'status':self.state['status'],'reason':self.state['stop_reason']})
                    break
                # Research serializes source/learning writes on the coordinator's connection.
                if any(n['tool'] in {'research','design_study','design_read'} for n in ready):
                    ready=[next(n for n in ready if n['tool'] in {'research','design_study','design_read'})]
                model_tools={'text','page','research'}|{x['name'] for x in self.toolkit.catalog if x.get('uses_model')}
                model_nodes=[n for n in ready if n['tool'] in model_tools]
                remaining=self.state['budget']['max_calls']-self.state['calls']
                if len(model_nodes)>remaining:
                    ready=[n for n in ready if n not in model_nodes]+model_nodes[:max(0,remaining)]
                    if not ready:
                        self.state.update(status='WAITING',stop_reason='MODEL_CALL_BUDGET_EXHAUSTED')
                        break
                worker=self.model() if any(n['tool'] in model_tools for n in ready) else self.worker
                orders=[self.work_order(n) for n in ready]
                self.save()
                if orders[0][1]['tool'] in {'research','design_study','design_read'}:
                    identity,order=orders[0]
                    try:
                        result=self.execute_tool(order,worker)
                    except Exception as error:
                        result={'passed':False,'error':str(error),'artifacts':[],'output':{},'observations':[],'unknowns':[str(error)]}
                    self.account()
                    self.commit(identity,order,result)
                    continue
                with ThreadPoolExecutor(max_workers=len(orders)) as pool:
                    futures=[pool.submit(self.execute_tool,order,worker) for _,order in orders]
                    for (identity,order),future in zip(orders,futures):
                        try:
                            result=future.result()
                        except Exception as error:
                            result={'passed':False,'error':str(error),'artifacts':[],'output':{},'observations':[], 'unknowns':[str(error)]}
                        self.account()
                        self.commit(identity,order,result)
            else:
                self.state.update(status='WAITING',stop_reason='ACTION_BUDGET_EXHAUSTED')
        except Exception as error:
            self.account()
            self.apply_controls()
            if self.state['status'] not in {'PAUSED','STOPPED'}:
                self.state.update(status='WAITING',stop_reason=str(error),needs_plan=True)
            self.emit('mission.failure',{'error':str(error),'recovery':'resume within remaining budget after inspecting failure'})
        finally:
            self.state['owner_pid']=None
            self.save()
        return self.state
