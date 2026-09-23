"""Shared learning lifecycle; domain adapters own representations and judgments."""
from __future__ import annotations

from copy import deepcopy

from jsonschema import Draft202012Validator

from ironman.learning.runtime import plain


class DomainLearning:
    def __init__(self, mission, domain, adapter, dependent_closure):
        self.mission = mission
        self.domain = domain
        self.adapter = adapter
        self.dependent_closure = dependent_closure
        self.identity = f'domain:{domain}:experimental'
        if mission.state.get('architecture')=='vnext':
            self.identity=mission.id+':'+self.identity

    def form(self):
        mission, adapter = self.mission, self.adapter
        legacy=f'domain:{self.domain}:experimental'
        if (mission.state.get('architecture')=='vnext' and not mission.store.exists(self.identity)
                and legacy in mission.state['scope_policy']['imports'] and mission.store.exists(legacy)):
            record,version=mission.store.load(legacy)
            mission.state['domain_models'][self.domain]=record['active']
            mission.state['domain_versions'][self.domain]=version
            mission.state.setdefault('domain_imports',{})[self.domain]={'id':legacy,'version':version,
                'scope':'candidate inference from provenance; Designer-only applicability selection required',
                'authority':'read-only experimental; not a global default'}
            mission.emit('domain.imported',mission.state['domain_imports'][self.domain])
            return
        if mission.store.exists(self.identity):
            record, version = mission.store.load(self.identity)
            mission.state['domain_models'][self.domain] = record['active']
            mission.state['domain_versions'][self.domain] = version
            mission.emit('domain.reused', {'id':self.identity, 'version':version,
                'evidence':record['evidence'], 'scope':'local experimental; no canonical promotion'})
            return
        if getattr(adapter, 'requires_observed_work', False):
            return
        observations = adapter.probe()
        source_id = mission.source(f'{self.domain}-operation-comparison', observations, self.domain)
        model = mission.ask('domain-form', adapter.instruction,
            {'materials':observations, 'source_id':source_id, 'prior_representation':None}, adapter.schema)
        Draft202012Validator(adapter.schema).validate(model)
        assessment = adapter.assess(model, observations)
        active = model if assessment['decision'] == 'experimental' else deepcopy(adapter.initial_model)
        record = {'title':adapter.title, 'active':active, 'proposal':model,
            'assessment':assessment, 'evidence':[source_id], 'status':assessment['decision'],
            'scope':adapter.scope}
        version = mission.store.save(self.identity, record, provenance=[source_id])
        mission.state['domain_models'][self.domain] = active
        mission.state['domain_versions'][self.domain] = version
        mission.emit('domain.formed', {'id':self.identity, 'version':version, **record})

    def record_observed(self, proposal, assessment, evidence, *, apply_to_project=False):
        """Persist a domain's comparative observation without imposing scalar scores.

        The domain owns representation and evidence interpretation. Experimental
        use remains distinct from verified benefit or canonical promotion.
        """
        mission=self.mission
        previous,version=mission.store.load(self.identity) if mission.store.exists(self.identity) else (None,None)
        decision=assessment['decision']
        if decision not in {'experimental','no_update','rejected'}:
            raise ValueError('unsupported observed-domain decision')
        active=proposal if decision=='experimental' else (previous['active'] if previous else
            deepcopy(mission.state['domain_models'].get(self.domain,self.adapter.initial_model)))
        record={'title':self.adapter.title,'active':active,'proposal':proposal,'assessment':assessment,
                'status':decision,'prior_version':version,'evidence':list(dict.fromkeys((previous or {}).get('evidence',[])+evidence)),
                'scope':assessment.get('scope',self.adapter.scope),
                'origin':assessment.get('origin','runtime_observed_practice')}
        new_version=mission.store.save(self.identity,record,version,provenance=record['evidence'])
        if apply_to_project:
            mission.state['domain_models'][self.domain]=active
            mission.state['domain_versions'][self.domain]=new_version
        mission.emit('domain.revised' if previous else 'domain.formed',{'id':self.identity,'version':new_version,**record})
        return {'version':new_version,**record}

    def revise(self, proposal=None, rollback_to=None, apply_to_project=True):
        """Evidence comparison precedes experimental adoption; old versions remain.

        Adapter scores express the domain's ordering, with larger values preferred.
        Explicit proposals are test interventions; runtime proposals use its worker.
        """
        mission, adapter = self.mission, self.adapter
        previous, version = mission.store.load(self.identity)
        if rollback_to:
            payload = mission.store.registry.get_version(self.identity, rollback_to)
            restored = mission.store.memory._body(plain(payload))
            record = {**previous, 'active':restored['active'], 'status':'withdrawn_to_prior',
                'withdrawal':{'from_version':version, 'to_version':rollback_to,
                              'reason':'explicit local revision withdrawal'}}
        else:
            observations = adapter.experiments(mission.state)
            source_id = mission.source(f'{self.domain}-revision-operations', observations, self.domain)
            intervention = proposal is not None
            if proposal is None:
                proposal = mission.ask('domain-revise', adapter.instruction,
                    {'materials':observations, 'prior_representation':previous['active'],
                     'project_observations':mission.state['observations'][-4:],
                     'feedback':mission.state['feedback']}, adapter.schema)
            Draft202012Validator(adapter.schema).validate(proposal)
            assessment = adapter.assess(proposal, observations)
            comparison=[(adapter.score(proposal,sample),adapter.score(previous['active'],sample)) for sample in observations]
            nonregression = all(new>=old for new,old in comparison)
            if not nonregression:
                assessment.update(decision='rejected', effect='regresses against active domain version')
            decision = assessment['decision'] if proposal != previous['active'] else 'no_update'
            if nonregression and not any(new>old for new,old in comparison):
                decision='no_update'
                assessment['effect']='no measured improvement over active behavior; preserve active representation'
            assessment['active_comparison']=[{'proposed':new,'active':old} for new,old in comparison]
            record = {'title':previous['title'],
                'active':proposal if decision == 'experimental' else previous['active'],
                'proposal':proposal, 'assessment':assessment, 'status':decision,
                'prior_version':version, 'evidence':previous['evidence'] + [source_id],
                'scope':previous['scope'], 'origin':'test_intervention' if intervention else 'runtime_reflection'}
        new_version = mission.store.save(self.identity, record, version, provenance=record['evidence'])
        if apply_to_project:
            mission.state['domain_models'][self.domain] = record['active']
            mission.state['domain_versions'][self.domain] = new_version
            if record['active'] != previous['active']:
                affected = self.dependent_closure(mission.state['nodes'], {
                    node['id'] for node in mission.state['nodes'] if node['tool'] in adapter.tools})
                for node in mission.state['nodes']:
                    if node['id'] in affected:
                        node['status'] = 'STALE'
                mission.state['needs_plan'] = True
                if mission.state['status'] not in {'PAUSED', 'STOPPED'}:
                    mission.state['status'] = 'READY'
        mission.emit('domain.revised', {'id':self.identity, 'version':new_version, **record})
        mission.save()
        return {'version':new_version, **record}
