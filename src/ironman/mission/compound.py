"""Project-scoped motion proposals, procedural PCM audio and real composition checks.

Workers choose the actual depiction and sound parameters. This adapter supplies
bounded rendering operations, not a rule that any material has one correct sound.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
from pathlib import Path
import random
import re
import struct
import time
import wave

from jsonschema import Draft202012Validator
from ironman.learning.workers import STRING, STRINGS, dump, obj


def number(low, high):
    return {'type':'number', 'minimum':low, 'maximum':high}


EVENT = obj({'event_id':STRING, 'entity_id':STRING, 'limb_id':STRING,
             'surface_id':STRING, 'time_s':number(0,12)})
SCENE = obj({'entity_id':STRING, 'label':STRING, 'duration_s':number(.5,12),
    'parts':{'type':'array', 'minItems':1, 'maxItems':16, 'items':obj({
        'id':STRING, 'shape':{'enum':['ellipse','rect']}, 'x':number(0,1), 'y':number(0,1),
        'width':number(.001,1), 'height':number(.001,1), 'fill':STRING})},
    'keyframes':{'type':'array', 'minItems':2, 'maxItems':64, 'items':obj({
        'time_s':number(0,12), 'x':number(0,1), 'y':number(0,1), 'angle_deg':number(-180,180)})},
    'events':{'type':'array', 'minItems':1, 'maxItems':32, 'items':EVENT},
    'limitations':STRINGS})
VOICE = obj({'surface_id':STRING, 'reason':STRING, 'duration_s':number(.02,.5),
    'gain':number(.01,.8), 'noise_mix':number(0,1), 'decay':number(1,20),
    'tones':{'type':'array', 'minItems':1, 'maxItems':4, 'items':obj({
        'frequency_hz':number(30,8000), 'weight':number(.01,1)})}})
AUDIO = obj({'voices':{'type':'array', 'minItems':1, 'maxItems':8, 'items':VOICE},
             'intent':STRING, 'limitations':STRINGS})
SAMPLE_RATE = 22050
AUDIO_TOKEN = 'JERVIS_AUDIO_URI'


def bounded_model_html(html):
    """Omit transport bytes only; original full HTML remains the browser artifact."""
    return re.sub(r'data:audio/[^;,\s"\']+;base64,[A-Za-z0-9+/=]+',
                  'embedded WAV omitted; media metadata supplied separately', html)

CATALOG = [
    {'name':'scene', 'purpose':'Propose an original abstract entity geometry, motion keyframes and contact event stream using actual shared entity identities. No invented physical facts.',
     'input':obj({'instruction':STRING}), 'domain':'motion', 'output':'scene.json', 'uses_model':True},
    {'name':'audio', 'purpose':'Compose actual procedural WAV audio and exact event-to-sample mapping from supplied contacts, surface facts and sound intent. Requires scene events; no geometry context.',
     'input':obj({'instruction':STRING}), 'domain':'audio', 'output':'audio.json', 'uses_model':True},
    {'name':'compose_inspect', 'purpose':'Observe an actual composed motion/audio page, WAV decoding, play/pause/seek and contact-marker synchronization in an isolated browser. Requires page, scene and audio dependencies.',
     'input':obj({'instruction':STRING}), 'domain':'observation', 'output':'composition-observation.json'}]


def interfaces(order):
    return [value['output']['interface'] for value in order['inputs'].values()
            if isinstance(value.get('output', {}).get('interface'), dict)]


def scene_interface(order):
    return next((item for item in interfaces(order) if item.get('kind') == 'scene'), None)


def _events(events, duration, entity_ids):
    ids = [event['event_id'] for event in events]
    if len(ids) != len(set(ids)):
        raise ValueError('contact event identities must be unique')
    for event in events:
        Draft202012Validator(EVENT).validate(event)
        if event['entity_id'] not in entity_ids or event['surface_id'] not in entity_ids:
            raise ValueError('contact references an entity absent from the semantic contract')
        if event['time_s'] >= duration:
            raise ValueError('contact onset is outside the timeline')
    if events != sorted(events, key=lambda event:event['time_s']):
        raise ValueError('contact stream must be time ordered')


def scene(order, worker):
    contract = order['semantic_contract']
    result = worker.ask(order['call_name'], '''Create an original fictional abstract motion scene as structured data, not a page or prose plan.
Use exactly the provided entity IDs; the moving entity and all contacted surfaces must be in entity_refs.
Parts use normalized local coordinates and shape primitives; keyframes use normalized viewport position and seconds.
Choose meaningful motion and contact timing yourself within the supplied purpose/brief; start keyframes at 0 and end at duration_s.
Each contact has a stable unique event_id, entity_id, a limb_id matching a part id, surface_id and onset time_s.
If contacts are already supplied in the semantic contract, preserve those events exactly. Otherwise propose original contacts.
The scene is an authored depiction, not a claim about real anatomy, weight or physical simulation. Unknown mass stays unknown.
Preserve supplied facts, source/status/units; do not fabricate measured properties or human judgments. Return limitations.''',
        {'brief':order['brief'], 'instruction':order['args']['instruction'],
         'semantic_contract':contract, 'context_packet':order.get('context_packet', {})}, SCENE)
    Draft202012Validator(SCENE).validate(result)
    entity_ids = {ref['entity_id'] for ref in contract['entity_refs']}
    if result['entity_id'] not in entity_ids:
        raise ValueError('moving entity is absent from semantic contract')
    _events(result['events'], result['duration_s'], entity_ids)
    if any(event['entity_id'] != result['entity_id'] for event in result['events']):
        raise ValueError('contact moving identity must match the depicted entity')
    if contract.get('events') and result['events'] != contract['events']:
        raise ValueError('scene changed supplied contact events')
    part_ids = [part['id'] for part in result['parts']]
    if len(set(part_ids)) != len(part_ids) or any(event['limb_id'] not in part_ids for event in result['events']):
        raise ValueError('contact limb must identify a unique depicted part')
    times = [frame['time_s'] for frame in result['keyframes']]
    if times[0] != 0 or times[-1] != result['duration_s'] or any(a >= b for a,b in zip(times,times[1:])):
        raise ValueError('motion keyframes must span duration in increasing order')
    public = {'kind':'scene', 'entity_id':result['entity_id'], 'label':result['label'],
        'duration_s':result['duration_s'], 'entity_refs':contract['entity_refs'], 'events':result['events'],
        'geometry':{'parts':result['parts'], 'keyframes':result['keyframes']},
        'limitations':result['limitations'], 'evidence_kind':'MODEL_AUTHORED_CREATIVE_PROPOSAL'}
    output = {'interface':public, 'input_versions':contract['input_versions'],
              'semantic_contract':contract, 'proposal':result}
    path = Path(order['directory']) / 'scene.json'
    dump(path, output)
    return {'artifacts':[str(path)], 'output':output, 'observations':[],
            'unknowns':result['limitations'], 'passed':True}


def render_audio(scene, recipe, directory):
    """Render model-selected damped tone/noise voices at exact contact samples."""
    Draft202012Validator(AUDIO).validate(recipe)
    duration = scene['duration_s']
    if not .5 <= duration <= 12:
        raise ValueError('audio timeline exceeds local rendering bounds')
    entity_ids = {ref['entity_id'] for ref in scene['entity_refs']}
    _events(scene['events'], duration, entity_ids)
    voices = {voice['surface_id']:voice for voice in recipe['voices']}
    used_surfaces = {event['surface_id'] for event in scene['events']}
    if len(voices) != len(recipe['voices']) or set(voices) != used_surfaces:
        raise ValueError('audio voices must cover exactly the contacted surfaces')
    frames = math.ceil(duration * SAMPLE_RATE)
    samples = [0.0] * frames
    event_map = []
    for event in scene['events']:
        voice = voices[event['surface_id']]
        start = round(event['time_s'] * SAMPLE_RATE)
        count = min(round(voice['duration_s'] * SAMPLE_RATE), frames-start)
        seed = int.from_bytes(hashlib.sha256(event['event_id'].encode()).digest()[:8], 'big')
        rng = random.Random(seed)
        weight = sum(tone['weight'] for tone in voice['tones'])
        for i in range(count):
            t = i / SAMPLE_RATE
            envelope = min(1, (i+1)/44) * math.exp(-voice['decay']*t/voice['duration_s'])
            tone = sum(v['weight'] * math.sin(2*math.pi*v['frequency_hz']*t) for v in voice['tones'])/weight
            noise = rng.uniform(-1,1)
            samples[start+i] += voice['gain'] * envelope * ((1-voice['noise_mix'])*tone + voice['noise_mix']*noise)
        event_map.append({**event, 'start_sample':start, 'end_sample':start+count})
    peak = max(abs(value) for value in samples)
    scale = min(1.0, .95/peak) if peak else 1.0
    pcm = b''.join(struct.pack('<h', round(value*scale*32767)) for value in samples)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'contacts.wav'
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(1); output.setsampwidth(2); output.setframerate(SAMPLE_RATE); output.writeframes(pcm)
    with wave.open(str(path), 'rb') as saved:
        decoded = struct.unpack('<'+'h'*saved.getnframes(), saved.readframes(saved.getnframes()))
    measured = {'frame_count':frames, 'nonzero_samples':sum(value != 0 for value in decoded),
        'peak_pcm_abs':max(abs(value) for value in decoded),
        'clipped_samples':sum(abs(value)>=32767 for value in decoded),
        'peak_before_scale':peak, 'applied_scale':scale, 'duration_s':frames/SAMPLE_RATE,
        'auditory_judgment':'No human or model listening judgment; waveform is procedural creative audio.'}
    public = {'kind':'audio', 'duration_s':frames/SAMPLE_RATE, 'sample_rate':SAMPLE_RATE,
        'wav_path':str(path), 'entity_refs':scene['entity_refs'], 'event_map':event_map,
        'render_status':{'stage':'AFTER_DETERMINISTIC_RENDER', 'evidence_kind':'TOOL_MEASUREMENT',
                         'wav_generated':True, 'wav_read_back':True, **measured},
        'model_proposal_assertions':{'stage':'BEFORE_DETERMINISTIC_RENDER',
            'evidence_kind':'MODEL_PROPOSAL', 'limitations':recipe['limitations'],
            'interpretation':'Historical recipe assertions, not current execution status; use render_status for actual generation and measured waveform facts.'},
        'limitations':['Procedural sound has no empirical material-fidelity or physical-model validation.',
                       'Browser playback, actual acoustic output and human listening effects require their own later observations.']}
    return public, measured


def audio(order, worker):
    contract = order['semantic_contract']
    supplied = scene_interface(order)
    if supplied is None:
        raise ValueError('audio requires the actual scene event interface')
    # Audio receives relevant identity/events, never deep visual geometry.
    local = {key:supplied[key] for key in ('kind','entity_id','duration_s','events')}
    local['entity_refs'] = contract['entity_refs']
    _events(local['events'], local['duration_s'], {ref['entity_id'] for ref in local['entity_refs']})
    if contract.get('events') and contract['events'] != local['events']:
        raise ValueError('audio event dependency differs from supplied semantic contract')
    recipe = worker.ask(order['call_name'], '''Compose a bounded procedural sound recipe for the supplied contact event stream and actual material facts.
Use one voice per contacted surface_id, with damped tones and noise. Choose parameters for the project's sound intention;
these primitives do not guarantee realistic wood/gravel/animal sound. Explain your artistic choices without claiming auditory evaluation.
Unknown body mass must stay unknown. Do not infer confirmed physical properties from the depiction.
Use no imported media, code, network or external service. Times/events are fixed; this task chooses sound texture, not movement timing.
Return only exact used surface IDs and allowed numeric parameters, with honest limitations.''',
        {'instruction':order['args']['instruction'], 'semantic_contract':contract,
         'scene_events':local, 'context_packet':order.get('context_packet', {})}, AUDIO)
    public, measured = render_audio(local, recipe, order['directory'])
    output = {'interface':public, 'recipe':recipe, 'measurement':measured,
              'input_versions':contract['input_versions'], 'semantic_contract':contract}
    path = Path(order['directory']) / 'audio.json'
    dump(path, output)
    return {'artifacts':[str(path),public['wav_path']], 'output':output,
        'observations':[{'kind':'tool_measurement', **measured}],
        'unknowns':public['limitations'], 'passed':measured['nonzero_samples'] > 0}


PAGE_INSTRUCTION = '''
This is a compound motion/audio work, not a schedule page. Compose the actual supplied scene geometry/keyframes/contact events and audio mapping.
Return original complete HTML/CSS/JS, with an audio element id="scene-audio" src="JERVIS_AUDIO_URI" preload="auto".
The runtime replaces that one src token with the real WAV bytes; never create fake sound, synthesize another track, or change event timing.
Use audio render_status as the actual post-render tool evidence. model_proposal_assertions are historical pre-render statements and must not be repeated as current claims that no WAV exists; retain genuinely unobserved listening effects separately.
Provide working buttons data-action="play" and data-action="pause", and input type="range" data-action="seek" with seconds (min=0,max=duration,step=0.01).
Animate the actual scene keyframes and limb/contact movement using audio.currentTime as the only clock, including pause/seek/replay and ended states.
Depict all supplied parts visibly. Use id="scene-entity" on the moving SVG/HTML entity; give it data-entity-id exactly as supplied.
Set its data-time-s on every render to the actual audio.currentTime; visual geometry must also change, not just this attribute.
Show a visible id="contact-marker" with data-event-id equal to the latest event_id at or before currentTime (empty before the first event), and explain the contacted material truthfully.
Render once initially, on seek/input/timeupdate/ended and in requestAnimationFrame during playback. Custom seek must set audio.currentTime and render immediately.
Include a useful working show-all details toggle required by the ordinary page interface. Do not display implementation schemas or raw debugging fields as the main product.
Preserve unknown physical properties. Give a clear sound-on action; never claim the user has heard or preferred it.
Use self-contained inline SVG/CSS/JS and the supplied WAV token, no external media or requests.
'''


def bind_audio(html, order):
    from bs4 import BeautifulSoup
    sound = next((item for item in interfaces(order) if item.get('kind') == 'audio'), None)
    if sound is None:
        raise ValueError('compound page requires actual audio output')
    path = Path(sound['wav_path'])
    artifacts = {str(Path(path).resolve()) for value in order['inputs'].values() for path in value.get('artifacts', [])}
    if str(path.resolve()) not in artifacts:
        raise ValueError('WAV was not supplied as a dependency artifact')
    with wave.open(str(path), 'rb') as wav:
        if wav.getframerate()!=SAMPLE_RATE or wav.getnchannels()!=1 or wav.getsampwidth()!=2 or wav.getnframes()>12*SAMPLE_RATE:
            raise ValueError('dependency WAV is outside compound media contract')
    parsed = BeautifulSoup(html, 'html.parser')
    media = parsed.select('audio#scene-audio')
    if len(media)!=1 or media[0].get('src')!=AUDIO_TOKEN or html.count(AUDIO_TOKEN)!=1:
        raise ValueError('page must bind the actual WAV once through audio#scene-audio src token')
    return html.replace(AUDIO_TOKEN, 'data:audio/wav;base64,'+base64.b64encode(path.read_bytes()).decode('ascii'))


def inspect(order):
    from playwright.sync_api import sync_playwright
    from .browser_actions import controls_snapshot
    directory = Path(order['directory'])
    directory.mkdir(parents=True, exist_ok=True)
    page_paths = [Path(path) for value in order['inputs'].values() for path in value.get('artifacts', []) if path.endswith('.html')]
    scene = scene_interface(order)
    sound = next((item for item in interfaces(order) if item.get('kind')=='audio'), None)
    if len(page_paths)!=1 or not scene or not sound:
        raise ValueError('compound observation needs one page and actual scene/audio interfaces')
    checks, errors, screenshots = [], [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width':1440,'height':1000}, accept_downloads=False, service_workers='block')
        context.route('**/*', lambda route:route.abort())
        page = context.new_page(); page.set_default_timeout(3500)
        page.on('pageerror', lambda error:errors.append(str(error)))
        csp = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; media-src data:; connect-src 'none'; base-uri 'none'; form-action 'none'"
        try:
            page.set_content('<meta http-equiv="Content-Security-Policy" content="'+csp+'">'+page_paths[0].read_text(encoding='utf-8'), wait_until='load')
            controls = controls_snapshot(page)
            _wait_media(page, '()=>document.querySelector("#scene-audio")?.readyState >= 2')
            media = page.locator('#scene-audio').evaluate('(a)=>({duration:a.duration,src:a.getAttribute("src"),muted:a.muted,volume:a.volume})')
            expected_wav = 'data:audio/wav;base64,'+base64.b64encode(Path(sound['wav_path']).read_bytes()).decode('ascii')
            checks.append({'name':'actual_wav_bound_and_decoded', 'passed':media['src']==expected_wav and abs(media['duration']-sound['duration_s'])<.01})
            for width,height,label in [(1440,1000,'desktop'),(390,844,'mobile')]:
                page.set_viewport_size({'width':width,'height':height})
                shot = directory / f'{label}.png'; page.screenshot(path=str(shot),full_page=True); screenshots.append(str(shot))
                checks.append({'name':label+'_horizontal_overflow','passed':page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')})
            page.locator('[data-action="play"]').click()
            _wait_media(page, '()=>document.querySelector("#scene-audio").currentTime > .1 && !document.querySelector("#scene-audio").paused')
            checks.append({'name':'audio_play_clock_advances', 'passed':True})
            page.locator('[data-action="pause"]').click()
            paused = page.locator('#scene-audio').evaluate('(a)=>({paused:a.paused,t:a.currentTime,muted:a.muted,volume:a.volume})')
            checks.append({'name':'audio_pause_and_audible_configuration','passed':paused['paused'] and not paused['muted'] and paused['volume']>0})
            states = []
            # Exercise real UI seek across beginning, middle and end contact states.
            times = sorted(set([0, scene['duration_s']*.5, min(scene['duration_s']-.01, scene['events'][-1]['time_s']+.01)]))
            for t in times:
                page.locator('[data-action="seek"]').evaluate('(el,t)=>{el.value=String(t);el.dispatchEvent(new Event("input",{bubbles:true}));el.dispatchEvent(new Event("change",{bubbles:true}))}', t)
                _wait_media(page, '(t)=>Math.abs(document.querySelector("#scene-audio").currentTime-t)<.04', t)
                actual = page.evaluate('''()=>{const a=document.querySelector('#scene-audio'),e=document.querySelector('#scene-entity'),m=document.querySelector('#contact-marker'),r=e.getBoundingClientRect();return {time:a.currentTime,entity:e.dataset.entityId,render_time:Number(e.dataset.timeS),event:m.dataset.eventId,visual:[r.x,r.y,r.width,r.height,e.getAttribute('transform'),getComputedStyle(e).transform]}}''')
                expected = [event for event in scene['events'] if event['time_s']<=actual['time']]
                checks.append({'name':'seek_event_sync', 'time_s':t, 'actual_event':actual['event'],
                    'expected_event':expected[-1]['event_id'] if expected else '',
                    'passed':actual['entity']==scene['entity_id'] and abs(actual['time']-actual['render_time'])<.05 and actual['event']==(expected[-1]['event_id'] if expected else '')})
                states.append(actual)
            checks.append({'name':'rendered_entity_changes_with_time','passed':len({json.dumps(state['visual']) for state in states})>1})
            shot=directory/'seek-contact.png';page.screenshot(path=str(shot),full_page=True);screenshots.append(str(shot))
        except Exception as exc:
            checks.append({'name':'compound_interaction_execution','passed':False,'error':str(exc)})
            controls = []
        finally:
            browser.close()
    checks.append({'name':'no_javascript_errors','passed':not errors})
    output = {'passed':all(check['passed'] for check in checks), 'checks':checks, 'errors':errors,
        'screenshots':screenshots, 'controls':controls, 'evidence_kind':'TOOL_OBSERVATION',
        'limitations':['Browser decoding/playback clock and rendered synchronization observed; no acoustic loopback recording or human listening assessment.',
                       'Rendering changes are observed, not proof of natural gait or artistic quality.'],
        'human_effect':'AWAITING_HUMAN_EVIDENCE'}
    path=directory/'composition-observation.json';dump(path,output)
    return {'artifacts':[str(path)]+screenshots, 'output':output,
            'observations':[{'kind':'tool_observation',**output}], 'unknowns':output['limitations'], 'passed':output['passed']}


def _wait_media(page, predicate, argument=None):
    # Playwright wait_for_function uses eval while polling, forbidden by this CSP.
    # Poll actual browser state without weakening the generated-page protection.
    for _ in range(35):
        if page.evaluate(predicate, argument):
            return
        time.sleep(.1)
    raise TimeoutError('media state did not satisfy the observed condition within 3.5 seconds')
