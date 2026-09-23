"""Designer-owned apprenticeship: actual pixels, original practice, scoped judgment.

No global aesthetic score or authority is created. All durable records use the
existing Mission StateStore / DomainLearning path.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import random
import shutil

from ironman.learning.workers import obj, STRING, STRINGS, dump


REPRESENTATION=obj({'kind':{'enum':['numeric','relation','language','example','operation']},
                    'content':STRING,'evidence_refs':STRINGS})
JUDGMENT=obj({'id':STRING,'title':STRING,'representations':{'type':'array','items':REPRESENTATION},
              'applies_when':STRINGS,'fails_when':STRINGS,'observe':STRINGS,'operations':STRINGS,
              'source_refs':STRINGS,'limitations':STRINGS})
MODEL=obj({'judgments':{'type':'array','maxItems':4,'items':JUDGMENT}})
SELECT=obj({'work_ids':STRINGS,'method_ids':STRINGS,'reason':STRING,'rejected':STRINGS})
CONTACT=obj({'observed_artifact':STRINGS,'author_claims':STRINGS,'model_hypotheses':STRINGS,
             'unseen_or_uncertain':STRINGS,'practice_instruction':STRING,
             'comparison_question':STRING,'variant_instruction':STRING,'held_constant':STRINGS})
COMPARISON=obj({'observed_differences':STRINGS,'model_judgment':STRING,
                'outcome':{'enum':['first','second','different','tie','unknown']},
                'hypothesis_status':{'enum':['supported_in_example','revised','rejected','unknown']},
                'reason':STRING,'scope_change':STRING,'model':MODEL,
                'decision':{'enum':['experimental','no_update','rejected']},'unknowns':STRINGS})
APPLY=obj({'selected_ids':STRINGS,'not_applicable':STRINGS,'reason':STRING,
          'action_changes':STRINGS,'observation_changes':STRINGS,'example_refs':STRINGS})
VISUAL=obj({'observed_artifact':STRINGS,'model_judgment':STRING,'violations':STRINGS,
            'revision_needed':{'type':'boolean'},'revision_instruction':STRING,
            'judgment_scope_review':STRINGS,'unknowns':STRINGS})


class Adapter:
    title='Designer judgment from observed works and practice'
    scope='Local reversible experimental use, task-scoped; model support only unless separate human evidence exists.'
    initial_model={'judgments':[]}
    requires_observed_work=True
    reflect_on_completion=False
    tools={'page','inspect','design_study'}


def image_paths(catalog):
    return [str(Path(p).resolve()) for w in catalog.get('works',[]) for p in w.get('images',[])]


def catalog_for(root, reference=None, project=None):
    path=(Path(root)/(reference or 'examples/apprenticeship/reference_catalog.json')).resolve()
    if not path.is_relative_to(Path(root).resolve()):
        raise PermissionError('Designer reference catalog must belong to the caller project repository')
    catalog=json.loads(path.read_text(encoding='utf-8-sig')) if path.is_file() else {'works':[],'methods':[]}
    if catalog.get('scope') and catalog['scope']!=project:
        raise PermissionError('Designer reference catalog is scoped to another project')
    return catalog


def applicability(worker, name, brief, model, version):
    """The same persisted relations can be rejected for a different task/style."""
    result=worker.ask(name+'-judgment-fit',
        '''Select only persisted Designer judgments relevant to this new ordinary brief. Do not impose the training layout or style.
Give specific changes to actual generation/observation, not a citation list. Reject incompatible conditions explicitly.
Examples are inspection material, not source code to replay. Empty selection is valid. Use only supplied IDs and example refs.''',
        {'brief':brief,'domain_version':version,'judgments':model.get('judgments',[]),'examples':model.get('examples',[])},APPLY)
    known={j['id'] for j in model.get('judgments',[])}
    if not set(result['selected_ids'])<=known:
        raise ValueError('unsupplied judgment ID selected')
    result['judgments']=[j for j in model.get('judgments',[]) if j['id'] in result['selected_ids']]
    result['domain_version']=version
    return result


def _render(path, directory):
    from .toolkit import observe_page
    Path(directory).mkdir(parents=True,exist_ok=True)
    return observe_page(path,directory)


def _neutral_images(items, directory):
    """Stable randomized labels hide condition names, not actual pixels."""
    pairs=list(items)
    random.Random(9173).shuffle(pairs)
    paths=[]; mapping={}
    for index,(identity,path) in enumerate(pairs):
        target=Path(directory)/f'panel-{index+1}.png'
        shutil.copyfile(path,target)
        paths.append(str(target));mapping[f'panel-{index+1}']=identity
    dump(Path(directory)/'comparison-key.json',mapping)
    return paths,mapping


def study(mission, order, worker):
    """A bounded whole-work practice within the existing runtime work graph."""
    from .toolkit import PAGE_SCHEMA
    directory=Path(order['directory']);directory.mkdir(parents=True,exist_ok=True)
    catalog=catalog_for(mission.root,mission.state.get('designer_reference_catalog'),mission.id)
    selection=worker.ask(order['call_name']+'-sources',
        'Choose up to two actual works and up to two teaching sources relevant to this complete creation task. Explain purpose and rejected directions. Rights notes constrain use; fame does not prove quality. Return supplied IDs only.',
        {'brief':order['brief'],'task':order['args']['question'],'catalog':catalog},SELECT)
    chosen={}
    for group,key in [('works','work_ids'),('methods','method_ids')]:
        known={x['id']:x for x in catalog[group]}
        if not set(selection[key])<=known.keys() or len(selection[key])>2:
            raise ValueError('study selection exceeds supplied source catalog')
        chosen[group]=[known[x] for x in selection[key]]
    if not chosen['works'] or not chosen['methods']:
        raise ValueError('actual reference works and teaching material are both required')
    pixels=image_paths(chosen)
    if not pixels:
        raise ValueError('reference media unavailable; text cannot substitute for visual observation')
    source=mission.source('designer-reference-selection',{'catalog':chosen,'selection':selection},'designer')
    contact=worker.ask(order['call_name']+'-contact',
        '''Actually inspect the attached reference images and supplied teaching sections. Separate visible facts, author claims, your explanations and unknowns.
Prepare an original complete page for the brief, not a clone. Derive a useful practice and a targeted variant that tests one explanation from these materials.
Methods are hypotheses, not authority. Keep task content/function constant; do not assume deviation is worse. Use visual relationships and nonnumeric qualities where useful, no universal score.
Return an actionable practice instruction and a distinguishable whole-page variant. This is runtime planning, not proof of learning.''',
        {'brief':order['brief'],'source_ref':source,'sources':chosen,'selection':selection},CONTACT,images=pixels)
    dump(directory/'source-contact.json',{'source_ref':source,'selection':selection,**contact})
    artifacts=[str(directory/'source-contact.json')]
    views=[]; practices=[]; practice_checks=[]
    for name,instruction in [('practice',contact['practice_instruction']),('variant',contact['variant_instruction'])]:
        workdir=directory/name;workdir.mkdir()
        data={'brief':order['brief'],'work_role':name,'instruction':instruction,'original_designer':mission.state['designer'],
              'teaching_sections':chosen['methods'],'reference_observation':{k:contact[k] for k in ('observed_artifact','author_claims','model_hypotheses','unseen_or_uncertain')},
              'prior_html':practices[0]['html'] if practices else None,'held_constant':contact['held_constant']}
        result=worker.ask(order['call_name']+'-'+name,
            '''Create complete original standalone HTML/CSS/JS for the full brief. The variant should implement only its targeted change as far as possible.
Preserve content and useful functionality. No network, imports, external services, submissions or persistent storage.
Include a real data-action="show-all" button that expands useful extra detail and changes visible text. Responsive phone/desktop layout.
Existing Designer remains advisory; do not copy reference branding or expressive artwork. Return the HTML itself; no aesthetic success claims.''',data,PAGE_SCHEMA,images=pixels)
        if not result['html'].strip():
            raise ValueError('empty practice artifact')
        page=workdir/'index.html';page.write_text(result['html'],encoding='utf-8')
        observed=_render(page,workdir/'observation')
        practice_checks.append({'work':name,'passed':observed['passed'],'checks':observed['checks']})
        dump(workdir/'observation.json',observed)
        artifacts.extend([str(page),str(workdir/'observation.json'),*observed['screenshots']])
        views.append((name,observed['screenshots'][0]));practices.append(result)
    neutral,mapping=_neutral_images(views,directory)
    comparison=worker.ask(order['call_name']+'-compare',
        '''Compare the actual attached whole-page images in a fresh context. Do not read creator self-assessments or infer a preferred condition from order.
Evaluate the fixed brief and comparison question. Different/tie/unknown are valid. Identify visible differences separately from your judgments and causal hypotheses.
Form at most four useful scoped judgments from the observed external references AND practice comparison, allowing relations, abstract language, examples and operations; numeric forms only if genuinely measured.
Each judgment must guide a concrete future observation/operation and include applicability, failure boundaries and evidence. Do not invent human feedback or aesthetic laws.
If the exercise does not justify a new/revised useful hypothesis, return no_update or rejected. Experimental means reversible model-supported trial, never verified aesthetic superiority.
When refining prior judgments, preserve supported content and modify/remove only what this comparison supports. ID references must come from supplied sources/artifacts or stable prior judgment IDs.''',
        {'brief':order['brief'],'question':contact['comparison_question'],'held_constant':contact['held_constant'],
         'reference_contact':contact,'source_ref':source,'sources':chosen,
         'panels':[{'label':f'panel-{i+1}','artifact':p} for i,p in enumerate(neutral)],
         'tool_checks':[{k:v for k,v in json.loads(Path(a).read_text(encoding='utf-8')).items() if k in {'checks','passed','errors'}}
                        for a in artifacts if Path(a).name=='observation.json'],
         'prior_model':mission.state['domain_models'].get('designer',{})},COMPARISON,images=neutral+pixels)
    # Pixel-derived descriptions and comparisons are model evidence, not objective measurements.
    comparison['evidence_kind']='MODEL_JUDGMENT'
    comparison['human_effect']='AWAITING_HUMAN_EVIDENCE'
    comparison['panel_mapping']=mapping
    dump(directory/'comparison.json',comparison)
    source2=mission.source('designer-observed-practice',{'contact':contact,'comparison':comparison,'artifacts':artifacts},'designer')
    model=comparison['model']
    model['examples']=[{'id':name,'path':path,'sha256':hashlib.sha256(Path(path).read_bytes()).hexdigest()} for name,path in views]
    assessment={k:comparison[k] for k in ('decision','reason','scope_change','hypothesis_status','human_effect','evidence_kind')}
    artifacts.extend([str(directory/'comparison.json'),str(directory/'comparison-key.json'),*neutral])
    proposal={'domain':'designer','model':model,'assessment':assessment,'evidence':[source,source2]}
    dump(directory/'judgment-result.json',proposal);artifacts.append(str(directory/'judgment-result.json'))
    return {'artifacts':artifacts,'output':{'proposal':proposal,'selection':selection,'comparison':comparison,'practice_checks':practice_checks},
            'domain_proposal':proposal,
            'observations':[{'kind':'observed_design_practice','status':assessment['decision'],
                             'evidence_kind':'MODEL_JUDGMENT','source_refs':[source,source2]}],
            'unknowns':comparison['unknowns'],'passed':all(c['passed'] for c in practice_checks)}


def visual_review(worker, order, observed, images):
    return worker.ask(order['call_name']+'-visual',
        '''Inspect the ACTUAL attached rendered page images. Text/DOM measurements are complementary, not a substitute for pixels.
Separate visible facts from design interpretation. Check whether the composition supports this brief and the intended actions.
Use persisted judgments as conditional hypotheses; challenge mismatched style/context. No universal score, no human preference claims.
Request a bounded revision only for a concrete visible problem relevant to the task; different acceptable styles may remain.
Name the visible region and actionable change. Avoid optional endless polish. Unknown is allowed.
Return the CURRENT unresolved unknowns after considering the actual workflow results and earlier semantic review: retain untested issues, but remove earlier uncertainties resolved by these later observations. Human comprehension and preference remain unknown without real feedback.''',
        {'brief':order['brief'],'constraints':order['constraints'],'observations':observed,
         'judgment_application':order.get('design_application',{}),'domain_model':order['domain_models'].get('designer',{})},VISUAL,images=images)
