"""Versioned mission records use the existing Registry and content attachments."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from ironman.learning.memory import Memory, _now
from ironman.storage import ObjectNotFoundError


class StateStore:
    def __init__(self, root, database):
        # Mission writes maintain this shared projection incrementally. Concurrent
        # project connections must not delete/rebuild each other's FTS tables.
        self.memory = Memory(root, database, rebuild=False)
        self.registry = self.memory.registry

    def close(self):
        self.memory.close()

    def load(self, identity):
        payload = self.memory._payload(identity)
        return self.memory._body(payload), payload['version']

    def exists(self, identity):
        try:
            self.registry.get_object(identity)
            return True
        except ObjectNotFoundError:
            return False

    def save(self, identity, body, expected=None, kind='ObjectEnvelope', provenance=()):
        payload = self.memory._envelope(identity, kind, 'mission', 'candidate', list(provenance))
        payload['created_by'] = 'jervis_mission_runtime'
        payload['tags'].append('whole_system_v1')
        if expected:
            old = self.memory._payload(identity)
            payload.update(version=f"0.{int(expected.split('.')[1])+1}.0", created_at=old['created_at'], updated_at=_now())
        payload['content_ref'], payload['content_hash'] = self.memory._attachment(deepcopy(body))
        payload['human_name'] = body.get('title', identity)
        if kind == 'ProjectOverlay':
            payload.update(project_id=identity, objectives=[body['brief']], constraints=body['constraints'],
                           decisions=body.get('decision_refs', []), allowed_domains=body.get('domains') or ['mission'],
                           file_refs=[str(Path(body['directory']))])
        self.memory._put(payload, expected)
        return payload['version']

    def event(self, project, kind, data):
        return self.registry.events.append(stream_id=project, actor='jervis_mission_runtime',
                                           event_type=kind, payload=data)

    def control(self, project, action, detail=None):
        return self.registry.events.append(stream_id=project+':control', actor='owner_or_local_test',
                    event_type=action, payload=detail or {})

    def controls(self, project, after=0):
        return self.registry.events.read_stream(project+':control', after_sequence=after)
