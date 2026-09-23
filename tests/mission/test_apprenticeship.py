"""Deterministic TEST_INTERVENTION fixtures; not evidence of visual competence."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from ironman.mission import design_judgment, toolkit
from ironman.mission.domains import DomainLearning
from ironman.mission.runtime import Mission, dependent_closure


ROOT = Path(__file__).resolve().parents[2]
HUMAN_UNKNOWN = 'human aesthetic and experience assessment pending'


class FixtureWorker:
    def __init__(self, outputs=()):
        self.outputs = iter(outputs)
        self.calls = 0
        self.usage = {}
        self.inputs = []

    def ask(self, name, instruction, data, schema, **kwargs):
        self.calls += 1
        self.inputs.append({'name': name, 'data': deepcopy(data), **kwargs})
        return deepcopy(next(self.outputs))


@pytest.fixture
def mission(tmp_path, monkeypatch):
    for relative in ('schemas/ironman.schema.yaml', 'control/LIFECYCLE_POLICY.yaml'):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    monkeypatch.setattr(Mission, 'export', lambda self: None)
    instance = Mission.create(tmp_path, tmp_path / 'state.sqlite', 'apprenticeship-fixture',
                              'Create a useful appointment page', worker=FixtureWorker())
    yield instance
    instance.close()


def mixed_model(source):
    return {'judgments': [{'id': 'group-related-actions', 'title': 'Group related actions',
        'representations': [
            {'kind': 'relation', 'content': 'Appointment details and their action share a visible group.', 'evidence_refs': [source]},
            {'kind': 'language', 'content': 'Leave breathing room between distinct choices; compact within each choice.', 'evidence_refs': [source]},
            {'kind': 'operation', 'content': 'Move the action into its detail group, then compare the full page.', 'evidence_refs': [source]},
        ], 'applies_when': ['several independent appointment choices'],
        'fails_when': ['a dense keyboard-operated data table'], 'observe': ['which action belongs to which choice'],
        'operations': ['group detail and action'], 'source_refs': [source],
        'limitations': ['TEST_INTERVENTION fixture; human preference unknown']}],
        'examples': [{'id': 'example-panel', 'path': 'fixture-reference.png', 'sha256': 'fixture-only'}]}


def assessment(decision):
    return {'decision': decision, 'reason': 'TEST_INTERVENTION lifecycle fixture',
            'evidence_kind': 'MODEL_JUDGMENT', 'human_effect': 'AWAITING_HUMAN_EVIDENCE'}


def test_mixed_judgment_forms_survive_registry_reopen_without_promoting_original_designer(mission):
    source = mission.source('fixture-observation', {'evidence_kind': 'TEST_INTERVENTION'}, 'designer')
    original = {'ready': True, 'capabilities': [{'id': 'existing-advisory', 'guidance': 'Keep actual controls working'}]}
    mission.state['designer'] = deepcopy(original)
    model = mixed_model(source)
    learning = DomainLearning(mission, 'designer', design_judgment.Adapter(), dependent_closure)
    revision = learning.record_observed(model, assessment('experimental'), [source], apply_to_project=True)
    mission.save()
    reopened = Mission(mission.root, mission.store.memory.database, mission.id, worker=FixtureWorker())
    try:
        assert reopened.state['domain_models']['designer'] == model
        assert reopened.state['domain_versions']['designer'] == revision['version']
        assert reopened.state['designer'] == original
        stored, _ = reopened.store.load('domain:designer:experimental')
        assert stored['active'] == model
        assert stored['assessment']['human_effect'] == 'AWAITING_HUMAN_EVIDENCE'
        assert reopened.store.memory._payload('domain:designer:experimental')['lifecycle_state'] == 'candidate'
        assert [r['kind'] for r in stored['active']['judgments'][0]['representations']] == ['relation', 'language', 'operation']
    finally:
        reopened.close()


@pytest.mark.parametrize('decision', ['no_update', 'rejected'])
def test_no_update_or_rejection_retains_active_judgment_and_full_proposal(mission, decision):
    source = mission.source('fixture-observation', {'evidence_kind': 'TEST_INTERVENTION'}, 'designer')
    learning = DomainLearning(mission, 'designer', design_judgment.Adapter(), dependent_closure)
    original = mixed_model(source)
    first = learning.record_observed(original, assessment('experimental'), [source], apply_to_project=True)
    proposal = deepcopy(original)
    proposal['judgments'][0]['representations'][1]['content'] = 'A narrower alternative explanation.'
    rejected = learning.record_observed(proposal, assessment(decision), [source], apply_to_project=True)
    assert rejected['version'] != first['version']
    assert rejected['prior_version'] == first['version']
    assert rejected['active'] == original and rejected['proposal'] == proposal
    assert mission.state['domain_models']['designer'] == original
    assert rejected['evidence'] == [source]
    assert rejected['assessment']['human_effect'] == 'AWAITING_HUMAN_EVIDENCE'


def test_applicability_can_inspect_saved_example_bindings_and_choose_no_style_transfer():
    model = mixed_model('source-fixture')
    worker = FixtureWorker([{'selected_ids': [], 'not_applicable': ['dense operational table'],
        'reason': 'Fixture context does not fit grouped appointment choices',
        'action_changes': [], 'observation_changes': [], 'example_refs': []}])
    result = design_judgment.applicability(worker, 'new-task', 'Build a compact dispatch table', model, '0.3.0')
    assert worker.inputs[0]['data']['examples'] == model['examples']
    assert result['judgments'] == [] and result['selected_ids'] == []
    assert result['domain_version'] == '0.3.0'


def inspection_node():
    return {'id': 'inspect', 'title': 'Inspect actual page', 'tool': 'inspect', 'args': {'instruction': 'Verify'},
            'requires_all': [], 'requires_any': [], 'joint_with': [], 'conflicts_with': [], 'resources': [],
            'scopes': ['page'], 'alternative_group': '', 'priority': 1,
            'expected': 'actual artifact checked', 'falsified_by': 'concrete violation', 'assumptions': [],
            'status': 'PENDING', 'version': 1, 'attempt': 0}


def observed_result(mission, monkeypatch):
    def render(path, directory, expected_ids=None):
        screenshot = Path(directory) / 'desktop.png'
        screenshot.write_bytes(b'NOT_REAL_PIXELS: TEST_INTERVENTION')
        return {'passed': True, 'checks': [{'name': 'real_interaction_changes_view', 'passed': True}],
                'screenshots': [str(screenshot)], 'errors': [], 'observed_text': 'Book appointment',
                'aesthetics': 'AWAITING_HUMAN_EVIDENCE'}
    monkeypatch.setattr(toolkit, 'observe_page', render)
    monkeypatch.setattr(toolkit, 'run_interactions', lambda *args: {
        'passed':True, 'steps':[], 'errors':[], 'screenshots':[], 'evidence_kind':'TEST_INTERVENTION'})
    mission.worker = FixtureWorker([
        {'passed': True, 'violations': [], 'unknowns': [], 'summary': 'Fixture technical issues absent',
         'interaction_plan':[], 'interaction_coverage':'No workflow exercised by this mechanics fixture'},
        {'observed_artifact': ['Fixture grouping visible'], 'model_judgment': 'Fixture supports brief',
         'violations': [], 'revision_needed': False, 'revision_instruction': '', 'judgment_scope_review': [], 'unknowns': []},
    ])
    mission.state['nodes'] = [inspection_node()]
    mission.state['unknowns'] = ['whether the current page action changes state', HUMAN_UNKNOWN]
    identity, order = mission.work_order(mission.state['nodes'][0])
    page = Path(mission.state['directory']) / 'input.html'
    page.write_text('<h1>Fixture appointment</h1>', encoding='utf-8')
    order['inputs'] = {'page': {'artifacts': [str(page)], 'output': {}}}
    mission.save()
    return identity, order, mission.execute_tool(order, mission.worker)


def test_current_observation_supersedes_technical_unknowns_but_not_human_evidence(mission, monkeypatch):
    identity, order, result = observed_result(mission, monkeypatch)
    mission.commit(identity, order, result)
    assert 'whether the current page action changes state' not in mission.state['unknowns']
    assert HUMAN_UNKNOWN in mission.state['unknowns']
    assert mission.state['knowledge_history'][-1]['status'] == 'superseded_by_observation'
    assert 'whether the current page action changes state' in mission.state['knowledge_history'][-1]['unknowns']
    output = mission.state['nodes'][0]['result']['output']
    assert output['visual_review']['human_effect'] == 'AWAITING_HUMAN_EVIDENCE'


def test_stale_observation_cannot_replace_current_unknowns(mission, monkeypatch):
    identity, order, result = observed_result(mission, monkeypatch)
    prior = deepcopy(mission.state['unknowns'])
    mission.state['scope_versions']['page'] = 1
    mission.save()
    mission.commit(identity, order, result)
    assert mission.state['unknowns'] == prior
    assert not mission.state.get('knowledge_history')
    stored, _ = mission.store.load(identity + ':result')
    assert stored['commit_status'] == 'STALE'


def test_neutral_panel_files_preserve_pixels_but_hide_source_condition_in_filenames(tmp_path):
    first, second = tmp_path / 'practice.png', tmp_path / 'variant.png'
    first.write_bytes(b'practice fixture bytes')
    second.write_bytes(b'variant fixture bytes')
    targets, mapping = design_judgment._neutral_images([('practice', first), ('variant', second)], tmp_path)
    for target in targets:
        path = Path(target)
        assert path.name.startswith('panel-')
        assert 'practice' not in path.name and 'variant' not in path.name
        original = {'practice': first, 'variant': second}[mapping[path.stem]]
        assert hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256(original.read_bytes()).digest()


@pytest.mark.parametrize('outcome', ['stale', 'stopped', 'failed', 'accepted'])
def test_study_proposal_becomes_active_only_after_accepted_successful_commit(mission, outcome):
    source = mission.source('fixture-study', {'evidence_kind':'TEST_INTERVENTION'}, 'designer')
    item = inspection_node()
    item.update(id='study', tool='design_study', args={'question':'Fixture practice'})
    mission.state['nodes'] = [item]
    identity, order = mission.work_order(item)
    mission.save()
    proposal = {'domain':'designer', 'model':mixed_model(source),
                'assessment':assessment('experimental'), 'evidence':[source]}
    result = {'passed':outcome != 'failed', 'output':{}, 'observations':[], 'unknowns':[],
              'artifacts':[], 'domain_proposal':proposal}
    if outcome == 'stale':
        mission.state['scope_versions']['page'] = 1
        mission.save()
    elif outcome == 'stopped':
        mission.store.control(mission.id, 'stop')
    mission.commit(identity, order, result)
    stored, _ = mission.store.load(identity + ':result')
    assert stored['result']['domain_proposal'] == proposal
    if outcome == 'accepted':
        active, version = mission.store.load('domain:designer:experimental')
        assert active['active'] == proposal['model']
        assert mission.state['domain_versions']['designer'] == version
        assert mission.state['domain_models']['designer'] == proposal['model']
        assert active['assessment']['human_effect'] == 'AWAITING_HUMAN_EVIDENCE'
        assert mission.state['nodes'][0]['status'] == 'COMPLETE'
    else:
        assert not mission.store.exists('domain:designer:experimental')
        assert 'designer' not in mission.state['domain_models']
        assert mission.state['nodes'][0]['status'] != 'COMPLETE'
