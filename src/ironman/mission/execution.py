"""Recoverable attempts and call accounting on the existing Registry/EventLog.

The event stream reserves calls before launch; worker threads never rebuild the
Memory index or share the coordinator's SQLite connection. Capsules are views,
not an additional source of authority.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import uuid

from jsonschema import Draft202012Validator, ValidationError, SchemaError
from ironman.learning.workers import input_identity
from ironman.storage import EventLog, _transaction


def _body(connection, identity):
    row = connection.execute(
        'SELECT v.payload_json FROM objects o JOIN object_versions v '
        'ON v.object_id=o.object_id AND v.version=o.current_version WHERE o.object_id=?',
        (identity,)).fetchone()
    if row is None:
        raise ValueError('unknown execution project: ' + identity)
    payload = json.loads(row[0])
    content = Path(payload['content_ref']).read_bytes()
    if hashlib.sha256(content).hexdigest() != payload['content_hash']:
        raise ValueError('project attachment integrity failure')
    return json.loads(content)


def _events(connection, stream, kind=None):
    query = 'SELECT event_type,payload_json FROM events WHERE stream_id=?'
    params = [stream]
    if kind:
        query += ' AND event_type=?'
        params.append(kind)
    return [(row[0], json.loads(row[1])) for row in connection.execute(
        query + ' ORDER BY stream_sequence', params).fetchall()]


class ExecutionLedger:
    def __init__(self, store, project_id, batch_id='vnext-alignment', global_max_calls=144):
        self.store = store
        self.project_id = project_id
        self.database = store.memory.database
        self.stream = project_id + ':execution'
        self.batch_id = batch_id
        self.batch_stream = 'execution:' + batch_id
        log = EventLog(self.database)
        try:
            with _transaction(log._connection):
                if not _events(log._connection, self.stream, 'execution.initialized'):
                    state = _body(log._connection, project_id)
                    log._append_in_transaction(stream_id=self.stream, actor='mission_execution',
                        event_type='execution.initialized', payload={
                            'project':project_id, 'baseline_calls':state.get('calls', 0),
                            'baseline_usage':state.get('usage', {}),
                            'baseline_usage_unknown':bool(state.get('calls', 0)),
                            'batch_id':batch_id})
                if not _events(log._connection, self.batch_stream, 'execution.batch_created'):
                    log._append_in_transaction(stream_id=self.batch_stream, actor='mission_execution',
                        event_type='execution.batch_created', payload={
                            'batch_id':batch_id, 'max_calls':global_max_calls,
                            'concurrency':2})
        finally:
            log.close()

    def begin_attempt(self, order_ref, order):
        ref = order_ref + ':attempt'
        if self.store.exists(ref):
            body, _ = self.store.load(ref)
        else:
            body = {'title':'Execution attempt', 'project':self.project_id,
                    'attempt_ref':ref, 'order_ref':order_ref,
                    'generation':int(order.get('generation', order.get('attempt', 1))),
                    'worker_instance':uuid.uuid4().hex, 'status':'RUNNING',
                    'initial_order':deepcopy(order),
                    'node':order.get('node'), 'call_name':order.get('call_name'),
                    'input_versions':deepcopy(order.get('input_versions', {})),
                    'scope_versions':deepcopy(order.get('scope_versions', {})),
                    'input_artifact_hashes':deepcopy(order.get('input_artifact_hashes', {})),
                    'directory':order.get('directory'), 'tool':order.get('tool'),
                    'checkpoints':[], 'operation_id':order_ref + ':operation'}
            self.store.save(ref, body, provenance=[order_ref])
            self.store.event(self.stream, 'attempt.started', body)
        return {key:body[key] for key in ('attempt_ref','generation','worker_instance')}

    def checkpoint(self, ref, phase, detail):
        body, version = self.store.load(ref)
        checkpoint = {'phase':phase, 'detail':deepcopy(detail)}
        body['checkpoints'].append(checkpoint)
        if body['status']!='REVOKED' and phase in {'result_committed','result_rejected'}:
            body['status'] = 'COMPLETE' if phase=='result_committed' else 'REJECTED'
        self.store.save(ref, body, version, provenance=[body['order_ref']])
        self.store.event(self.stream, 'attempt.checkpoint', {'attempt_ref':ref, **checkpoint})
        return checkpoint

    def revoke(self, ref, reason, successor=None):
        body, version = self.store.load(ref)
        body.update(status='REVOKED', reason=reason, successor=successor)
        self.store.save(ref, body, version, provenance=[body['order_ref']])
        self.store.event(self.stream, 'attempt.revoked', {
            'attempt_ref':ref, 'generation':body['generation'],
            'reason':reason, 'successor':successor})
        return body

    def eligible(self, ref, generation):
        body, _ = self.store.load(ref)
        return body['status']=='RUNNING' and body['generation']==generation

    def worker_event(self, stage, record):
        """Called on the actual worker thread; no private transport text accepted."""
        log = EventLog(self.database)
        try:
            with _transaction(log._connection):
                if stage=='prepared':
                    existing = _events(log._connection, self.batch_stream, 'model.prepared')
                    if any(item['call_id']==record['call_id'] for _,item in existing):
                        return
                    state = _body(log._connection, self.project_id)
                    if state['status'] in {'PAUSED','STOPPED'}:
                        raise RuntimeError('PROJECT_CONTROL_PREVENTS_MODEL_LAUNCH')
                    baseline = _events(log._connection, self.stream, 'execution.initialized')[0][1]
                    # Include this project's reservations in other batches too.
                    project_calls = log._connection.execute(
                        "SELECT payload_json FROM events WHERE event_type='model.prepared'").fetchall()
                    consumed = baseline['baseline_calls'] + sum(
                        json.loads(row[0])['project']==self.project_id for row in project_calls)
                    if consumed >= state['budget']['max_calls']:
                        raise RuntimeError('MODEL_CALL_BUDGET_EXHAUSTED')
                    batch = _events(log._connection, self.batch_stream, 'execution.batch_created')[0][1]
                    if len(existing)>=min(batch['max_calls'],state.get('global_max_calls',batch['max_calls'])):
                        raise RuntimeError('GLOBAL_MODEL_CALL_BUDGET_EXHAUSTED')
                    # Match the durable order by its actual model-call prefix.
                    attempts = _events(log._connection, self.stream, 'attempt.started')
                    matches = [a for _,a in attempts if a.get('call_name') and
                               (record['name']==a['call_name'] or record['name'].startswith(a['call_name']+'-'))]
                    binding = max(matches, key=lambda a:len(a['call_name'])) if matches else {}
                    if binding:
                        current = _body(log._connection, binding['attempt_ref'])
                        if current['status']!='RUNNING':
                            raise RuntimeError('REVOKED_ATTEMPT_CANNOT_LAUNCH')
                    record.update(project=self.project_id, batch_id=self.batch_id,
                        attempt_ref=binding.get('attempt_ref'), generation=binding.get('generation'),
                        worker_instance=binding.get('worker_instance',record['call_id']),
                        model_call_reserved=True)
                elif not record.get('model_call_reserved'):
                    # A rejected reservation has a local failure receipt but used no call.
                    return
                keys = ('call_id','name','input_hash','input_chars','input_path','receipt_path',
                        'project','batch_id','attempt_ref','generation','worker_instance',
                        'configuration','worker_pid','process_status','returncode',
                        'status','duration_seconds','error','usage_reported','usage')
                public = {key:deepcopy(record[key]) for key in keys if key in record}
                public['usage_available'] = bool(record.get('usage_reported', 'usage' in record))
                if not public['usage_available']:
                    public['usage'] = None
                log._append_in_transaction(stream_id=self.batch_stream, actor='mission_execution',
                    event_type='model.'+stage, payload=public)
        finally:
            log.close()

    def _call_records(self):
        records = {}
        for row in self.store.memory._index.execute(
                "SELECT event_type,payload_json FROM events WHERE event_type IN "
                "('model.prepared','model.started','model.finished') ORDER BY rowid"):
            item = json.loads(row[1])
            if item['project']==self.project_id:
                records.setdefault(item['call_id'], {}).update(item)
        return records

    def reconcile(self, worker_directory):
        records = self._call_records()
        directory = Path(worker_directory).resolve()
        for call in records.values():
            path = Path(call['receipt_path']).resolve()
            if not path.is_relative_to(directory) or not path.is_file():
                continue
            receipt = json.loads(path.read_text(encoding='utf-8'))
            if receipt.get('call_id')!=call['call_id'] or receipt.get('input_hash')!=call['input_hash']:
                continue
            # A verified public receipt survives a coordinator dying before its final event.
            for key in ('status','worker_pid','process_status','returncode','usage','usage_reported','error'):
                if key in receipt:
                    call[key] = deepcopy(receipt[key])
            call['usage_available'] = bool(receipt.get('usage_reported','usage' in receipt))
            if not call['usage_available']:
                call['usage'] = None
        baseline = _events(self.store.memory._index, self.stream, 'execution.initialized')[0][1]
        usage = deepcopy(baseline['baseline_usage'])
        for call in records.values():
            if call.get('usage_available'):
                for key,value in (call.get('usage') or {}).items():
                    usage[key] = usage.get(key,0)+value
        return {'calls':baseline['baseline_calls']+len(records), 'usage':usage,
                'unreported_calls':sum(not c.get('usage_available') for c in records.values()),
                'baseline_calls':baseline['baseline_calls'],
                'baseline_usage_unknown':baseline['baseline_usage_unknown'],
                'call_records':list(records.values())}

    def recover(self):
        attempts = _events(self.store.memory._index, self.stream, 'attempt.started')
        state,_ = self.store.load(self.project_id)
        calls = self.reconcile(Path(state['directory'])/'workers')['call_records']
        for call in calls:
            call['recoverable_output'] = self._recoverable_output(call)
        result = []
        for _,attempt in attempts:
            body, version = self.store.load(attempt['attempt_ref'])
            body['version'] = version
            body['calls'] = [c for c in calls if c.get('attempt_ref')==body['attempt_ref']]
            result.append(body)
        return result

    @staticmethod
    def _recoverable_output(call):
        """An exact final JSON response is reusable evidence; partial text is not."""
        if call.get('status')!='COMPLETE':
            return {'available':False,'reason':'no complete model response'}
        try:
            receipt=json.loads(Path(call['receipt_path']).read_text(encoding='utf-8'))
            inputs=json.loads(Path(call['input_path']).read_text(encoding='utf-8'))
            if (receipt.get('call_id')!=call['call_id'] or receipt.get('input_hash')!=call['input_hash']
                    or input_identity(inputs)!=call['input_hash']):
                return {'available':False,'reason':'saved input binding changed'}
            for binding in inputs.get('images',[]):
                if hashlib.sha256(Path(binding['artifact_path']).read_bytes()).hexdigest()!=binding['sha256']:
                    return {'available':False,'reason':'saved image bytes changed'}
            Draft202012Validator(inputs['schema']).validate(receipt['output'])
            return {'available':True,'input_hash':call['input_hash'],
                    'receipt_path':call['receipt_path'],'input_path':call['input_path'],
                    'output':receipt['output'], 'condition':'reuse only for identical current compiled input hash'}
        except (ValidationError,SchemaError):
            return {'available':False,'reason':'saved response schema validation failed'}
        except (OSError,ValueError,KeyError) as error:
            return {'available':False,'reason':'saved response could not be validated: '+str(error)}

    def capsule(self, ref):
        """Derive a handoff view from current records, without storing new authority."""
        body, version = self.store.load(ref)
        state, state_version = self.store.load(self.project_id)
        reconciliation = self.reconcile(Path(state['directory'])/'workers')
        return {'derived':True, 'attempt':body, 'attempt_version':version,
                'project':self.project_id, 'project_version':state_version,
                'status':state['status'], 'remaining_calls':max(0,state['budget']['max_calls']-reconciliation['calls']),
                'calls':[c for c in reconciliation['call_records'] if c.get('attempt_ref')==ref],
                'limitations':['Uncommitted in-flight reasoning is not recoverable.',
                               'Revalidate input versions and uncertain tool effects before reuse.']}
