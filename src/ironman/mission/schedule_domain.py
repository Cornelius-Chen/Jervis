"""A domain-owned local scheduling laboratory, operations and evaluation.

The coordinator knows none of these semantics. Experiments use explicitly
constructed workloads; measurements are real solver executions, not preferences.
"""
from __future__ import annotations

from itertools import combinations

from ironman.learning.workers import obj, STRING, STRINGS


SESSION = obj({'id': STRING, 'title': STRING, 'start': {'type':'integer'},
               'end': {'type':'integer'}, 'resource': STRING, 'people': STRINGS,
               'value': {'type':'integer', 'minimum':0}})
REQUEST = obj({'sessions': {'type':'array','items':SESSION, 'maxItems':16},
               'turnaround': {'type':'integer','minimum':0}, 'capacity': {'type':'integer','minimum':1}})


def features(request):
    sessions = request['sessions']
    return {'weighted': len({x['value'] for x in sessions}) > 1,
            'shared_people': any(set(a['people']) & set(b['people']) for a,b in combinations(sessions,2)),
            'turnaround': request['turnaround'] > 0, 'size': len(sessions)}


def conflicts(a, b, gap):
    overlap = a['start'] < b['end'] + gap and b['start'] < a['end'] + gap
    return overlap and (a['resource'] == b['resource'] or bool(set(a['people']) & set(b['people'])))


def validate(request, selected):
    sessions = {s['id']:s for s in request['sessions']}
    problems = []
    if len(selected) != len(set(selected)) or any(i not in sessions for i in selected):
        return {'passed':False, 'violations':['unknown or duplicate session'], 'value':0}
    items = [sessions[i] for i in selected]
    for a,b in combinations(items,2):
        if conflicts(a,b,request['turnaround']):
            problems.append(f"{a['id']} conflicts with {b['id']}: room/person or turnaround")
    if len(items)>request['capacity']:
        problems.append('session capacity exceeded')
    if any(s['end']<=s['start'] for s in items):
        problems.append('nonpositive interval')
    return {'passed':not problems, 'violations':problems, 'value':sum(s['value'] for s in items)}


def solve(request, algorithm='greedy'):
    if len({s['id'] for s in request['sessions']})!=len(request['sessions']):
        raise ValueError('duplicate input session IDs')
    if len(request['sessions'])>16:
        raise ValueError('local exact solver budget is 16 sessions; acquire another tool for larger requests')
    if algorithm == 'greedy':
        selected=[]
        for s in sorted(request['sessions'],key=lambda s:(s['end'],s['id'])):
            trial=selected+[s['id']]
            if validate(request,trial)['passed']:
                selected=trial
    elif algorithm == 'optimal':
        ids=[s['id'] for s in request['sessions']]
        selected=[]
        best=0
        for size in range(1,min(len(ids),request['capacity'])+1):
            for subset in combinations(ids,size):
                result=validate(request,subset)
                if result['passed'] and result['value']>best:
                    selected=list(subset)
                    best=result['value']
    else:
        raise ValueError('unknown scheduling operation')
    return {'selected':selected,'algorithm':algorithm, **validate(request,selected),
            'sessions':[s for s in request['sessions'] if s['id'] in selected],
            'features':features(request)}


def material():
    def s(i,start,end,value,room='A',people=()):
        return dict(id=i,title=i,start=start,end=end,resource=room,people=list(people),value=value)
    return [
      {'name':'unequal-value intervals','request':{'sessions':[s('short',0,2,1),s('long',0,5,10),s('later',5,6,2)],'turnaround':0,'capacity':3}},
      {'name':'shared instructor across rooms','request':{'sessions':[s('a',0,2,2,'A',['Lee']),s('b',1,4,9,'B',['Lee']),s('c',4,5,1,'B',[])],'turnaround':0,'capacity':3}},
      {'name':'uniform nonoverlapping control','request':{'sessions':[s('a',0,1,1),s('b',1,2,1),s('c',2,3,1)],'turnaround':0,'capacity':3}},
      {'name':'turnaround changes compatibility','request':{'sessions':[s('a',0,2,1),s('b',2,4,6),s('c',5,6,2)],'turnaround':1,'capacity':3}}
    ]


def probe():
    return [{'name':x['name'],'request':x['request'], 'features':features(x['request']),
             'before':solve(x['request'],'greedy'),'alternative':solve(x['request'],'optimal')}
            for x in material()]


MODEL = obj({'title':STRING, 'objects':STRINGS, 'observations':STRINGS,
    'relations':{'type':'array','items':obj({'name':STRING,'condition':{'enum':['weighted','shared_people','turnaround']},
                                          'operation':{'enum':['greedy','optimal']},'expected_change':STRING,'falsified_by':STRING})},
    'uncertainties':STRINGS,'revision_reason':STRING})


def choose(model, request):
    actual=features(request)
    used=[r for r in model.get('relations',[]) if actual.get(r['condition'])]
    return (used[0]['operation'] if used else 'greedy'), used[:1]


def assess(model, observations):
    comparisons=[]
    for sample in observations:
        algorithm,relations=choose(model,sample['request'])
        result=solve(sample['request'],algorithm)
        comparisons.append({'sample':sample['name'],'algorithm':algorithm,'relations':relations,
                            'passed':result['passed'],'value':result['value'],
                            'baseline_value':sample['before']['value'],'oracle_value':sample['alternative']['value']})
    admissible=all(x['passed'] and x['value']>=x['baseline_value'] for x in comparisons)
    benefit=any(x['value']>x['baseline_value'] for x in comparisons)
    return {'decision':'experimental' if admissible and benefit else 'rejected',
            'comparisons':comparisons, 'evidence_scope':'constructed workload operations; no human or market evidence',
            'effect':'measured value improvement on these workloads only' if admissible and benefit else 'no supported improvement'}


FORMATION = '''Form or revise a small scheduling domain representation from the supplied REAL tool outputs.
Objects and relations must describe intervals, shared resources/people, weighted value and compatibility,
not page aesthetics. Each conditional relation chooses an available operation. Prefer the smallest
change supported by comparisons. The first matching relation controls execution. Retain uncertainties,
counterexamples and explicit limits: <=16 sessions, exact enumeration costs, constructed workloads only.
You may add/merge/split object concepts or relations when the observations justify it. No causal claim
beyond the intervention; no invented user preference. Return version content, not global code changes.'''


class Adapter:
    """Scheduling-owned representation, operations and utility comparisons."""

    schema = MODEL
    instruction = FORMATION
    title = 'Scheduling conditional operation model'
    scope = 'experimental; <=16 local sessions; no general-domain mastery'
    tools = ('schedule',)
    initial_model = {'relations': []}
    probe = staticmethod(probe)
    assess = staticmethod(assess)

    def experiments(self, state):
        observations = self.probe()
        for node in state['nodes']:
            output = node.get('result', {}).get('output', {})
            if node['tool'] in self.tools and output.get('request'):
                request = output['request']
                observations.append({'name':'current project ' + node['id'], 'request':request,
                    'features':features(request), 'before':solve(request, 'greedy'),
                    'alternative':solve(request, 'optimal')})
        return observations

    def score(self, model, observation):
        request = observation['request']
        return solve(request, choose(model, request)[0])['value']
