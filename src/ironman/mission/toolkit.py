"""Real local tools; domain semantics live here, outside the coordinator."""
from __future__ import annotations

import json
from pathlib import Path
from html import escape

from jsonschema import Draft202012Validator
from ironman.learning.workers import obj, STRING, STRINGS, dump
from . import schedule_domain as scheduling
from . import design_judgment
from . import compound
from .browser_actions import controls_snapshot, run_interactions


CATALOG = [
 {'name':'design_read','purpose':'Read admitted source text and images; preserve scoped observations and unvalidated explanations without generating or evaluating work.',
  'input':obj({'source_index':{'type':'integer','minimum':0}}),'domain':'designer','output':'source-reading.json','uses_model':True},
 {'name':'design_study','purpose':'When an actual visual-judgment gap matters, select available reference works/teaching, actually view images, create a complete original practice and targeted variant, compare, and form/refine scoped experimental Designer judgments. One bounded whole-work study; not required on every design task.',
  'input':obj({'question':STRING}),'domain':'designer','output':'judgment-result.json','uses_model':True},
 {'name':'research','purpose':'Resolve a material project gap using one frozen batch of up to 3 explicit public source URLs. Learn source-grounded candidates, no recursive search.',
  'input':obj({'question':STRING,'domain':STRING,'sources':{'type':'array','maxItems':3,'items':obj({'url':STRING,'title':STRING})}}),
  'domain':'research','output':'research-result.json'},
 {'name':'schedule', 'purpose':'Select weighted sessions without room/person/turnaround conflicts; <=16 sessions.',
  'input':scheduling.REQUEST, 'domain':'scheduling','output':'schedule.json'},
 {'name':'page', 'purpose':'Generate an actual standalone interactive HTML artifact from input artifacts and brief.',
  'input':obj({'instruction':STRING}), 'domain':'designer','output':'index.html'},
 {'name':'text', 'purpose':'Write a complete useful plain-language document; no visual skill needed.',
  'input':obj({'instruction':STRING}), 'domain':'writing','output':'document.md'},
 {'name':'inspect', 'purpose':'Observe the composed artifact in a real browser and check schedule compatibility plus displayed selected session IDs.',
  'input':obj({'instruction':STRING}), 'domain':'observation','output':'observation.json','uses_model':True},
]
CATALOG += compound.CATALOG

PAGE_SCHEMA = obj({'html':STRING,'used_capability_ids':STRINGS,'limitations':STRINGS})
TEXT_SCHEMA = obj({'text':STRING,'limitations':STRINGS})
SEMANTICS = obj({'passed':{'type':'boolean'},'violations':STRINGS,'unknowns':STRINGS,'summary':STRING})
INTERACTION_SEMANTICS = obj({**SEMANTICS['properties'],
    'interaction_plan':{'type':'array','maxItems':10,'items':obj({
        'action':{'enum':['click','fill','select','check','assert_text']},
        'selector':STRING,'value':{'type':['string','boolean']}})},
    'interaction_coverage':STRING})


def safe_target(directory, name):
    path=(Path(directory)/name).resolve()
    if not path.is_relative_to(Path(directory).resolve()):
        raise ValueError('artifact path escapes assigned work directory')
    return path


def observe_page(path, directory, expected_ids=None):
    from playwright.sync_api import sync_playwright
    errors=[]
    checks=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        context=browser.new_context(viewport={'width':1440,'height':1000},accept_downloads=False,service_workers='block')
        context.route('**/*',lambda route:route.abort())
        page=context.new_page()
        # Generated page code is untrusted: isolated context and zero network.
        page.on('pageerror',lambda error:errors.append(str(error)))
        csp="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; base-uri 'none'; form-action 'none'"
        page.set_content('<meta http-equiv="Content-Security-Policy" content="'+csp+'">'+Path(path).read_text(encoding='utf-8'),wait_until='load')
        text=page.locator('body').inner_text()
        controls=controls_snapshot(page)
        screenshots=[]
        for width,height,label in [(1440,1000,'desktop'),(390,844,'mobile')]:
            page.set_viewport_size({'width':width,'height':height})
            shot=Path(directory)/f'{label}.png'
            page.screenshot(path=str(shot),full_page=True)
            overflow=page.evaluate('document.documentElement.scrollWidth > window.innerWidth + 1')
            checks.append({'name':label+'_horizontal_overflow','passed':not overflow})
            screenshots.append(str(shot))
        ids=page.locator('[data-session-id]:visible').evaluate_all('(xs)=>xs.map(x=>x.dataset.sessionId)')
        button=page.locator('[data-action="show-all"]').first
        interaction={'available':button.count()>0,'changed':False}
        if button.count():
            before=page.locator('body').inner_text()
            button.click()
            interaction['changed']=page.locator('body').inner_text()!=before
        if expected_ids is not None:
            checks.append({'name':'composed_schedule_selected_ids','passed':set(expected_ids)==set(ids),
                           'expected':list(expected_ids),'actual':ids})
        checks.extend([{'name':'no_javascript_errors','passed':not errors},
                       {'name':'real_interaction_changes_view','passed':interaction['changed']}])
        browser.close()
    return {'passed':all(x['passed'] for x in checks),'checks':checks,'errors':errors,
            'screenshots':screenshots,'interaction':interaction,'observed_text':text,
            'controls':controls,
            'aesthetics':'AWAITING_HUMAN_EVIDENCE','perception':'browser DOM, layout metrics and captured screenshots; no model vision claim'}


class Toolkit:
    catalog=CATALOG
    domains={'scheduling':scheduling.Adapter(),'designer':design_judgment.Adapter()}

    def execute(self, order, worker):
        directory=Path(order['directory'])
        directory.mkdir(parents=True,exist_ok=True)
        tool=order['tool']
        args=order['args']
        spec=next(t for t in self.catalog if t['name']==tool)
        Draft202012Validator(spec['input']).validate(args)
        inputs=order['inputs']
        packet_data = ({'semantic_contract':order.get('semantic_contract', {}),
                        'context_packet':order['context_packet']} if 'context_packet' in order else {})
        if tool in {'scene','audio'}:
            return getattr(compound, tool)(order, worker)
        if tool=='compose_inspect' or (tool=='inspect' and compound.scene_interface(order)):
            return compound.inspect(order)
        if tool=='schedule':
            domain=order['domain_models'].get('scheduling',{})
            algorithm,relations=scheduling.choose(domain,args)
            output=scheduling.solve(args,algorithm)
            output.update(request=args, consumed_relations=relations,
                          domain_version=order['domain_versions'].get('scheduling'))
            path=directory/'schedule.json'
            dump(path,output)
            return {'artifacts':[str(path)],'output':output,'observations':[{'kind':'tool_measurement','passed':output['passed'],'value':output['value']}],
                    'unknowns':[], 'passed':output['passed']}
        if tool=='text':
            result=worker.ask(order['call_name'], 'Write the requested useful document. Use real provided inputs; no imaginary execution or outcomes.',
                            {'brief':order['brief'],'instruction':args['instruction'],'inputs':inputs,'preferences':order.get('preferences',[]),**packet_data},TEXT_SCHEMA)
            path=directory/'document.md'
            path.write_text(result['text'],encoding='utf-8')
            return {'artifacts':[str(path)],'output':result,'observations':[], 'unknowns':result['limitations'],'passed':True}
        if tool=='page':
            compound_page = compound.scene_interface(order) is not None
            result=worker.ask(order['call_name'], '''Produce complete standalone HTML/CSS/JS for the actual brief. Use supplied Designer advisory guidance only where applicable.
No remote fonts, media, imports, fetches, network or file writes. Content must be original; no copying source expression.
Use supplied schedule output EXACTLY for selected IDs/titles/times, explain resource/people constraints. Include all candidates in an alternate view.
Each selected session has data-session-id=its exact ID. Include a working button data-action="show-all" that toggles between selected and all candidates and visibly changes the text.
Show selected value and constraints truthfully. If no schedule was requested, make show-all expand useful details.
Create responsive, legible, distinct design suited to the actual product. Keep browser output errors absent. State limitations, no unmeasured aesthetic superiority.
If judgment_application is supplied, implement its applicable action changes concretely; treat examples as conditional references, not a layout to copy. Preserve the original Designer guidance and task constraints. Ignore judgments explicitly deemed inapplicable.
Return real document markup, not a plan. Use only genuinely supplied capability IDs.''' + (compound.PAGE_INSTRUCTION if compound_page else ''),
                {'brief':order['brief'],'constraints':order['constraints'],'instruction':args['instruction'],
                 'inputs':inputs,'designer':order['designer'],'previous_artifact':order.get('previous_artifact',{}),
                 'preferences':order.get('preferences',[]),'judgment_application':order.get('design_application',{}),
                 **packet_data}, PAGE_SCHEMA,
                 **({'images':order['design_images']} if order.get('design_images') else {}))
            if not result['html'].strip():
                raise ValueError('worker produced an empty HTML artifact')
            allowed={x.get('object_id',x.get('id')) for x in order['designer'].get('registry_candidates',[])}
            allowed.update(x.get('id',x.get('entry_id')) for x in order['designer'].get('capabilities',[]))
            if not set(result['used_capability_ids'])<=allowed:
                raise ValueError('worker attributed guidance to an unsupplied capability')
            # Guidance sources are advisory, and model attribution remains an assertion.
            result['attribution_note']='Model-reported guidance use; inspect actual inputs and artifact, not proof of aesthetic gain.'
            if compound_page:
                result['html'] = compound.bind_audio(result['html'], order)
            path=directory/'index.html'
            path.write_text(result['html'],encoding='utf-8')
            return {'artifacts':[str(path)],'output':{**{k:v for k,v in result.items() if k!='html'},'judgment_application':order.get('design_application',{})},
                    'observations':[], 'unknowns':result['limitations'],'passed':True}
        if tool=='inspect':
            # Study inputs contain practice/variant pages, not the final delivery.
            pages=[p for value in inputs.values() if 'comparison' not in value.get('output',{})
                   for p in value['artifacts'] if p.endswith('.html')]
            schedules=[v['output'] for v in inputs.values() if 'selected' in v.get('output',{})]
            if not pages:
                # Nonvisual work is inspected as an actual file, without injecting Designer.
                paths=[p for value in inputs.values() for p in value['artifacts']]
                output={'passed':bool(paths) and all(Path(p).stat().st_size>0 for p in paths),
                        'files':paths,'observation':'files exist and are nonempty; content quality requires review'}
            else:
                output=observe_page(pages[0],directory,schedules[0]['selected'] if schedules else None)
                output['inspected_artifact']=pages[0]
                for schedule in schedules:
                    actual=scheduling.validate(schedule['request'],schedule['selected'])
                    output['checks'].append({'name':'schedule_constraints','passed':actual['passed'],'detail':actual})
                output['passed']=all(x['passed'] for x in output['checks'])
            semantic=worker.ask(order['call_name']+'-semantics',
                '''Independently check the actual supplied final artifacts against the ordinary brief and later constraints.
Check factual/semantic consistency, especially all session times, values, shared resources, turnaround,
the selected total, and explanation correctness. Browser checks and solver validation are supplied facts.
Do not score aesthetics, invent human preference, or treat a generated explanation as a tool observation.
Later explicit constraints override initial ones. Distinguish violations from unknowable human effect.
Return passed only if the supplied work has no identified material brief/constraint violation.
When interaction_plan is requested, propose at most ten concrete browser actions to exercise the brief's main workflow, using supplied actual control selectors/options and artifact_html. Assert the changed result in its actual output container, including count/status and identity where applicable; static workshop text elsewhere in body cannot verify a confirmation. Use value='' for click, Boolean for check, text for other actions. Do not claim these planned actions already passed. Explain covered and uncovered behaviors in interaction_coverage.''',
                {'brief':order['brief'],'constraints':order['constraints'],'preferences':order.get('preferences',[]),
                 'inputs':inputs,'previous_artifact':order.get('previous_artifact',{}),'tool_observation':output,**packet_data,
                 **({'artifact_html':compound.bounded_model_html(Path(pages[0]).read_text(encoding='utf-8'))} if pages and order.get('visual_observation') else {})},
                 INTERACTION_SEMANTICS if pages and order.get('visual_observation') else SEMANTICS)
            output['semantic_review']={'evidence_kind':'independent fresh model judgment; not human evidence',**semantic}
            output['passed']=output['passed'] and semantic['passed']
            if pages and order.get('visual_observation'):
                interaction=run_interactions(pages[0],semantic['interaction_plan'],directory)
                output['workflow_observation']=interaction
                output['screenshots'].extend(interaction['screenshots'])
                output['passed']=output['passed'] and interaction['passed']
                visual=design_judgment.visual_review(worker,order,output,output['screenshots'])
                output['visual_review']={'evidence_kind':'MODEL_JUDGMENT','human_effect':'AWAITING_HUMAN_EVIDENCE',**visual}
                output['passed']=output['passed'] and not visual['revision_needed'] and not visual['violations']
            output['current_knowledge']={'unknowns':list(dict.fromkeys(output.get('visual_review',semantic)['unknowns']
                                         +(['human aesthetic and experience assessment pending'] if pages else []))),
                                         'basis':'actual current composed artifact observation; prior planning unknowns superseded, not silently declared resolved'}
            dump(directory/'observation.json',output)
            return {'artifacts':[str(directory/'observation.json')]+output.get('screenshots',[]),
                    'output':output,'observations':[{'kind':'tool_observation',**output}],
                    'unknowns':['human aesthetic and experience assessment pending'] if pages else [],'passed':output['passed']}
        raise ValueError('tool not installed')
