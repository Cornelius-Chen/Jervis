"""Local user entrypoint; controls are durable EventLog messages."""
import argparse
import json
from pathlib import Path

from .runtime import Mission
from .state import StateStore


def main():
    parser=argparse.ArgumentParser(description='Jervis project runtime')
    parser.add_argument('command',choices=['new','resume','status','pause','stop','feedback','budget','domain-revise','domain-rollback','serve','portfolio','handoff','fact-change','review-experience'])
    parser.add_argument('--project')
    parser.add_argument('--projects',nargs='+')
    parser.add_argument('--architecture',choices=['vnext'])
    parser.add_argument('--exclude-scopes',nargs='*',default=[])
    parser.add_argument('--batch',default='vnext-alignment')
    parser.add_argument('--prepare-only',action='store_true')
    parser.add_argument('--slice',action='store_true')
    parser.add_argument('--node')
    parser.add_argument('--entity')
    parser.add_argument('--field')
    parser.add_argument('--value-json')
    parser.add_argument('--expected-version',type=int)
    parser.add_argument('--brief',help='plain goal text or UTF-8 text file')
    parser.add_argument('--learning-objective',help='Optional project learning purpose; runtime chooses how to pursue it while delivering the brief')
    parser.add_argument('--root',default=str(Path(__file__).resolve().parents[3]))
    parser.add_argument('--database',default='registry/jervis.sqlite')
    parser.add_argument('--max-calls',type=int,default=24)
    parser.add_argument('--max-steps',type=int,default=30)
    parser.add_argument('--text')
    parser.add_argument('--scopes',nargs='+',default=['scheduling'])
    parser.add_argument('--source-kind',choices=['human','tool_observation','test_intervention'],default='human')
    parser.add_argument('--source-ref',default='local owner CLI')
    parser.add_argument('--feedback-kind',choices=['constraint','preference','observation'],default='constraint')
    parser.add_argument('--apply-to-future',action='store_true')
    parser.add_argument('--supersedes-event-ids',nargs='*',default=[])
    parser.add_argument('--version')
    parser.add_argument('--port',type=int,default=8766)
    args=parser.parse_args()
    root=Path(args.root).resolve()
    database=Path(args.database)
    if not database.is_absolute():
        database=root/database
    if args.command=='serve':
        from .workbench import serve
        serve(root,database,args.port)
        return
    if args.command=='portfolio':
        from .portfolio import run_portfolio
        if not args.projects:parser.error('--projects is required')
        print(json.dumps(run_portfolio(root,database,args.projects,batch_id=args.batch),ensure_ascii=False))
        return
    if not args.project:
        parser.error('--project is required')
    identity=args.project if args.project.startswith('mission:') else 'mission:'+args.project
    if args.command in {'pause','stop','feedback','budget','handoff','fact-change'}:
        store=StateStore(root,database)
        if not store.exists(identity):
            parser.error('unknown project')
        data={}
        if args.command=='feedback':
            if not args.text:
                parser.error('--text is required for feedback')
            data={'text':args.text,'scopes':args.scopes,'source_kind':args.source_kind,'source_ref':args.source_ref,
                  'kind':args.feedback_kind,'apply_to_future':args.apply_to_future,'supersedes_event_ids':args.supersedes_event_ids}
        elif args.command=='budget':
            if not args.text:
                parser.error('--text must explain the explicit budget change')
            data={'max_calls':args.max_calls,'max_steps':args.max_steps,'reason':args.text}
        elif args.command=='handoff':
            data={'node':args.node,'reason':args.text or 'local owner planned replacement'}
        elif args.command=='fact-change':
            if not args.entity or not args.field or args.value_json is None or args.expected_version is None:
                parser.error('fact-change requires --entity --field --value-json --expected-version')
            data={'entity_id':args.entity,'field':args.field,'value':json.loads(args.value_json),
                  'expected_version':args.expected_version,'source_kind':args.source_kind,'source_ref':args.source_ref}
        event=store.control(identity,'fact_change' if args.command=='fact-change' else args.command,data)
        print(json.dumps({'project':identity,'action':args.command,'event':event.event_id,'state':'recorded; coordinator applies at next boundary'}))
        store.close()
        return
    if args.command=='new':
        if not args.brief:
            parser.error('--brief is required')
        brief=args.brief
        if len(brief)<240 and Path(brief).is_file():
            brief=Path(brief).read_text(encoding='utf-8-sig')
        mission=Mission.create(root,database,args.project,brief,max_calls=args.max_calls,max_steps=args.max_steps,learning_objective=args.learning_objective,
            architecture=args.architecture,excluded_scopes=args.exclude_scopes,batch_id=args.batch)
    else:
        mission=Mission(root,database,identity)
    try:
        if args.command=='status':
            mission.export()
            print(json.dumps(mission.state,ensure_ascii=False,indent=2))
        elif args.command=='domain-rollback':
            if not args.version:
                parser.error('--version is required')
            result=mission.revise_domain(rollback_to=args.version)
            print(json.dumps(result,ensure_ascii=False,indent=2))
        elif args.command=='review-experience':
            print(json.dumps(mission.review_local_experience(),ensure_ascii=False,indent=2))
        elif args.command=='domain-revise':
            print(json.dumps(mission.revise_domain(),ensure_ascii=False,indent=2))
        else:
            if args.command=='resume':
                mission.store.control(identity,'resume')
            if args.prepare_only:
                mission.export();state=mission.state
            else:
                state=mission.execute(max_cycles=1 if args.slice else None)
            print(json.dumps({'project':mission.id,'status':state['status'],'reason':state['stop_reason'],
                              'calls':state['calls'],'usage':state['usage'],'status_page':str(Path(state['directory'])/'status.html')},ensure_ascii=False))
    finally:
        mission.close()


if __name__=='__main__':
    main()
