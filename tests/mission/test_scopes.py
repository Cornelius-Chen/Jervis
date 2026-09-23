"""Scope and semantic version boundaries; synthetic markers are not quality evidence."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import shutil

import pytest

from ironman.mission import scopes
from ironman.mission.state import StateStore
from ironman.storage import VersionConflictError

ROOT=Path(__file__).resolve().parents[2]

@pytest.fixture
def mission(tmp_path):
    for name in ('schemas/ironman.schema.yaml','control/LIFECYCLE_POLICY.yaml'):
        target=tmp_path/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/name,target)
    store=StateStore(tmp_path,tmp_path/'state.sqlite')
    state={**scopes.initialize('mission:a',excluded=['horror','moba']),'brief':'Pip is a dog. The floor is wood.',
           'nodes':[],'preferences':[]}
    m=SimpleNamespace(id='mission:a',state=state,store=store,emit=lambda kind,data:None)
    yield m
    store.close()

def test_denied_memory_body_never_read_and_matching_ancestor_is_minimal(mission):
    good=scopes.put_memory(mission,{'title':'Contact event timing','keywords':['contact'],'text':'permitted'},scope='audio')
    unrelated=scopes.put_memory(mission,{'title':'Contact visual silhouette','keywords':['contact'],'text':'not audio'},scope='motion')
    mission.state['memory_catalog'] += [
        {'id':'never-read-foreign','project':'mission:b','scope':'audio','tags':[],'status':'experimental','title':'contact'},
        {'id':'never-read-horror','project':mission.id,'scope':'audio','tags':['horror'],'status':'experimental','title':'contact'}]
    reads=[];load=mission.store.load
    def spy(identity):reads.append(identity);return load(identity)
    mission.store.load=spy
    result=scopes.retrieve_local(mission,'audio/foley','contact timing')
    assert reads==[good['id']]
    assert {x['reason'] for x in result['excluded']}=={'other_project','explicit_scope_exclusion','unrelated_professional_scope'}
    assert result['selected'][0]['content']['text']=='permitted'
    with pytest.raises(PermissionError):scopes.put_memory(mission,{'title':'x'},scope='audio',tags=['horror'])
    with pytest.raises(PermissionError):scopes.put_memory(mission,{'title':'x'},scope='audio',identity='mission:b:memory:x')

def test_sourced_facts_owner_version_and_field_specific_impact(mission):
    scopes.adopt_initial_facts(mission,[{'entity_id':'floor','fields':[{'name':'material','value_json':'"wood"',
       'status':'confirmed','source_quote':'The floor is wood.','owner_scope':'audio/material','unit':''}]}])
    change={'entity_id':'floor','field':'material','value':'gravel','expected_version':1}
    with pytest.raises(PermissionError):scopes.change_fact(mission,change,actor_scope='motion',source_kind='tool_observation',source_ref='x')
    changed=scopes.change_fact(mission,change,actor_scope='owner',source_kind='test_intervention',source_ref='test1')
    with pytest.raises(VersionConflictError):scopes.change_fact(mission,change,actor_scope='owner',source_kind='test_intervention',source_ref='test2')
    mission.state['nodes']=[{'id':'sound','contract':{'entity_fields':[{'entity_id':'floor','fields':['material']}]}},
                            {'id':'motion','contract':{'entity_fields':[]}}]
    impact=scopes.semantic_impact(mission.state,[changed])
    assert impact['definite']==['sound'] and impact['unrelated']==['motion']
    assert scopes.changed_bindings(mission.state,{'floor.material':1})==['floor.material']
    with pytest.raises(ValueError,match='exact source quote'):
        scopes.adopt_initial_facts(mission,[{'entity_id':'pip','fields':[{'name':'mass','value_json':'12',
           'status':'confirmed','source_quote':'Pip weighs twelve','owner_scope':'motion','unit':'kg'}]}])

def test_audio_packet_strips_geometry_and_unrelated_corrections(mission):
    contract={'purpose':'make contact sound','receiver_scope':'audio/foley','entity_fields':[],
              'memory_query':'contact','missing_information':[],'assumptions':[]}
    mission.state['constraint_records']=[{'text':'audio-only','scopes':['audio']},{'text':'visual-only','scopes':['motion']}]
    mission.toolkit=SimpleNamespace(catalog=[{'name':'audio','domain':'audio'}])
    inputs={'move':{'artifacts':[],'output':{'interface':{'kind':'scene','events':[],
                           'geometry':{'PRIVATE_GEOMETRY_SENTINEL':1}},'deep_author_explanation':'DO_NOT_SHARE'}}}
    packet,selected=scopes.compile_context(mission,{'tool':'audio','id':'sound','contract':contract},inputs)
    assert packet['constraints']==[mission.state['constraint_records'][0]]
    assert 'geometry' not in selected['move']['output']['interface']
    assert 'deep_author_explanation' not in str(packet)
    assert packet['actual_chars']>0 and packet['health']['action']=='continue'
