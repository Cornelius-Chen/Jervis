"""Two shared execution slots; each Mission owns its connection in its thread."""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import os
from pathlib import Path

from .state import StateStore


def _resources(state):
    """Resources of the next ready cycle, with the runtime's AND/OR readiness."""
    if state.get('needs_plan'):
        return set()
    done = {n['id'] for n in state['nodes'] if n['status']=='COMPLETE'}
    used = {n.get('alternative_group') for n in state['nodes'] if n['status']=='COMPLETE'}
    for node in sorted(state['nodes'], key=lambda n:-n['priority']):
        if node['status']!='PENDING' or (node.get('alternative_group') and node['alternative_group'] in used):
            continue
        if set(node['requires_all'])<=done and (not node['requires_any'] or done.intersection(node['requires_any'])):
            return set(node.get('resources', []))
    return set()


def _cycle(root, database, project_id):
    # Imports here also leave the module independent of Mission's import graph.
    from .runtime import Mission
    mission = Mission(root, database, project_id)
    try:
        return mission.execute(max_cycles=1)
    finally:
        mission.close()


def _apply_pending_controls(root, database, store, project_id, state):
    # Active projects apply their own controls. STOP is never implicitly resumed.
    if state['status']=='STOPPED' or not store.controls(project_id,state.get('control_sequence',0)):
        return state
    from .runtime import Mission
    mission=Mission(root,database,project_id)
    try:
        mission.apply_controls()
        mission.save()
        return mission.state
    finally:
        mission.close()


def run_portfolio(root, database, project_ids, *, batch_id='vnext-alignment',
                  global_max_calls=144, max_cycles=120, priorities=None):
    """Run ready projects fairly; waiting/control states release their slots.

    One cycle per dispatch prevents a long project from monopolizing a worker.
    Existing execute remains the owner of plans, tools, commits and recovery.
    """
    root, database = Path(root).resolve(), Path(database).resolve()
    projects = list(dict.fromkeys(p if p.startswith('mission:') else 'mission:'+p for p in project_ids))
    priorities = priorities or {}
    store = StateStore(root, database)
    identity = 'portfolio:' + batch_id
    owns_portfolio = False
    try:
        if store.exists(identity):
            state, version = store.load(identity)
            if state['projects']!=projects:
                raise ValueError('portfolio project set differs; use a separate batch identity')
            if state.get('owner_pid'):
                import psutil
                if psutil.pid_exists(state['owner_pid']):
                    raise RuntimeError('portfolio already has a live coordinator')
        else:
            state = {'title':'Shared project execution', 'projects':projects,
                     'budget':{'max_calls':global_max_calls,'concurrency':2},
                     'dispatch_count':0,'last_dispatched':{},'status':'READY','errors':[]}
            version = store.save(identity, state, provenance=projects)
        state['owner_pid']=os.getpid()
        version=store.save(identity,state,version,provenance=projects)
        owns_portfolio=True
        # The portfolio owns two slots; each project cycle must occupy only one.
        for project in projects:
            project_state, project_version = store.load(project)
            if project_state.get('owner_pid'):
                import psutil
                if psutil.pid_exists(project_state['owner_pid']):
                    raise RuntimeError('project already has a live coordinator: '+project)
            project_state['budget']['concurrency'] = 1
            project_state['execution_batch'] = batch_id
            project_state['batch_id'] = batch_id
            project_state['global_max_calls'] = state['budget']['max_calls']
            store.save(project,project_state,project_version,'ProjectOverlay',project_state.get('decision_refs',[]))
        active = {}
        dispatched = 0
        failed = set()
        state['status'] = 'RUNNING'
        with ThreadPoolExecutor(max_workers=2) as pool:
            while dispatched<max_cycles or active:
                if dispatched<max_cycles:
                    running_projects = {value[0] for value in active.values()}
                    candidates = []
                    for project in projects:
                        if project in running_projects or project in failed:
                            continue
                        current, _ = store.load(project)
                        current = _apply_pending_controls(root,database,store,project,current)
                        if current['status'] in {'COMPLETE','WAITING','PAUSED','STOPPED'}:
                            continue
                        candidates.append((project,current))
                    candidates.sort(key=lambda pair:(state['last_dispatched'].get(pair[0],-1),
                        -priorities.get(pair[0],priorities.get(pair[0].removeprefix('mission:'),0))))
                    for project,current in candidates:
                        if len(active)>=2 or dispatched>=max_cycles:
                            break
                        resources = _resources(current)
                        if any(resources & running[1] for running in active.values()):
                            continue
                        state['dispatch_count'] += 1
                        state['last_dispatched'][project] = state['dispatch_count']
                        version = store.save(identity,state,version,provenance=projects)
                        store.event(identity,'portfolio.dispatched',{
                            'project':project,'dispatch':state['dispatch_count'],'resources':sorted(resources),
                            'active_slots':len(active)+1,'concurrency':2})
                        future = pool.submit(_cycle,root,database,project)
                        active[future] = (project,resources)
                        dispatched += 1
                if not active:
                    break
                completed,_ = wait(active,timeout=0.25,return_when=FIRST_COMPLETED)
                for future in completed:
                    project,_ = active.pop(future)
                    try:
                        outcome = future.result()
                        store.event(identity,'portfolio.released',{'project':project,'status':outcome['status']})
                    except Exception as error:
                        failed.add(project)
                        state['errors'].append({'project':project,'error':str(error)})
                        store.event(identity,'portfolio.cycle_failed',{'project':project,'error':str(error)})
        snapshots = {project:store.load(project)[0] for project in projects}
        state['project_statuses'] = {key:value['status'] for key,value in snapshots.items()}
        state['status'] = 'COMPLETE' if all(value['status']=='COMPLETE' for value in snapshots.values()) else 'WAITING'
        state['stop_reason'] = 'cycle budget exhausted' if dispatched>=max_cycles else 'no runnable projects'
        state['calls'] = {key:value.get('calls',0) for key,value in snapshots.items()}
        state['owner_pid']=None
        version = store.save(identity,state,version,provenance=projects)
        owns_portfolio=False
        return {**state,'version':version,'portfolio':identity}
    finally:
        if owns_portfolio:
            current,current_version=store.load(identity)
            if current.get('owner_pid')==os.getpid():
                current['owner_pid']=None
                store.save(identity,current,current_version,provenance=projects)
        store.close()
