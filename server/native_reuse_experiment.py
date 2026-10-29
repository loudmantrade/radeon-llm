#!/usr/bin/python3
"""Evaluate existing llama.cpp RAM prefix caching, then non-prefix KV shifting."""
import argparse
import json
import re
from pathlib import Path
import signal
import subprocess
import time
import urllib.error
import urllib.request
import daemon

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = daemon.RUNTIME
PORT = 18091
WORK = ROOT.parent.parent / 'work/native-reuse'
KEY = None


def api(path, body=None):
    req = urllib.request.Request('http://127.0.0.1:%d' % PORT + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={'Authorization':'Bearer '+(KEY or (RUNTIME/'server/api-key').read_text().strip()),'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=600) as response:
        return json.load(response)


def request(label, messages, reuse=0, cache=True, max_tokens=12):
    started=time.monotonic()
    response=api('/v1/chat/completions',{'model':'qwen3-4b-local','messages':messages,
        'temperature':0,'max_tokens':max_tokens,'cache_prompt':cache,'n_cache_reuse':reuse})
    result={'label':label,'content':response['choices'][0]['message']['content'].strip(),
            'wall_seconds':round(time.monotonic()-started,3),'timings':response['timings']}
    print('%s: answer=%s; cached=%d, evaluated=%d, prefill=%.2fs' %
        (label,json.dumps(result['content'],ensure_ascii=False),result['timings']['cache_n'],
         result['timings']['prompt_n'],result['timings']['prompt_ms']/1000),flush=True)
    return result


def decode_case(case, default_system, default_padding):
    if len(case) not in (6,8):
        raise ValueError('A reuse case needs six fields and optional system/padding overrides')
    return tuple(case[:6]) + (tuple(case[6:]) if len(case)==8 else (default_system,default_padding))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--cpu',action='store_true')
    parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('--phase',choices=['all','prefix','shift'],default='all')
    parser.add_argument('--context',type=int)
    parser.add_argument('--case',choices=['neutral_removal','override_removal','value_override_removal','replacement_prefix','context_sensitive_removal'])
    parser.add_argument('--probe-decode',action='store_true',help='Compare generation before and after KV shifting')
    parser.add_argument('--active-only-shift',action='store_true',help='Use isolated build with occupied-cell K-shift optimization')
    options=parser.parse_args()
    context=options.context or (2048 if options.cpu else 16384)
    if context not in ((2048,8192,16384) if options.cpu else (8192,16384)):
        parser.error('Supported context: 8192/16384 on GPU; 2048/8192/16384 on CPU')
    if options.dry_run:
        print(json.dumps({'backend':'CPU' if options.cpu else 'Radeon Vulkan',
            'context':context,'phases':options.phase,'case':options.case,'probe_decode':options.probe_decode,
            'ready_made_mechanisms':['llama.cpp --cache-ram','llama.cpp n_cache_reuse'],
            'ram_cache_mib':512,'shift_min_tokens':64,'production_config_changed':False,
            'active_only_shift':options.active_only_shift,
            'restore_daemon_after_test':not options.cpu},indent=2))
        return
    WORK.mkdir(parents=True,exist_ok=True)
    # Use the already installed, GPU-tested runtime and libraries.
    daemon.ROOT=RUNTIME
    daemon.CONFIG=json.loads((RUNTIME/'server/config.json').read_text())
    daemon.BIN=RUNTIME/daemon.CONFIG.get('native_binary','llama.cpp/build-vulkan/bin/llama-server')
    if options.active_only_shift:
        daemon.BIN=ROOT/'experimental/shift-active/bin/llama-server'
        assert daemon.BIN.exists(),'Build/package the isolated active-shift runtime first'
    daemon.KEY=RUNTIME/'server/api-key'
    args,env=daemon.command(cpu=options.cpu,port=PORT,context=context,
                           cache_dir=WORK/('slots-'+str(time.time_ns())))
    args[args.index('--fit')+1]='off'
    args[args.index('--verbosity')+1]='4'
    env.pop('LLAMA_KV_SHIFT_ACTIVE_ONLY',None)
    if options.active_only_shift:
        env['LLAMA_KV_SHIFT_ACTIVE_ONLY']='1'
    suffix='cpu' if options.cpu else 'gpu'
    if options.active_only_shift:
        suffix+='-active-shift'
    if context!=(2048 if options.cpu else 16384):
        suffix+='-ctx%d' % context
    phase_suffix='' if options.phase=='all' else '-'+options.phase+'-results'
    output=ROOT/('logs/native-reuse-%s%s.json' % (suffix,phase_suffix))
    report={'backend':suffix,'context':context,'selected_case':options.case,
            'active_only_shift':options.active_only_shift,
            'production_config_changed':False,'prefix':None,'shift':[],
            'experimental_shift_promoted':False}
    stopped=False
    child=None
    log_file=None
    active_log=None
    active_phase=None

    def save(): output.write_text(json.dumps(report,indent=2)+'\n')

    def shutdown():
        nonlocal child,log_file,active_log,active_phase
        if child is not None and child.poll() is None:
            child.terminate()
            try: child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                child.kill();child.wait()
        child=None
        if log_file is not None:
            log_file.close();log_file=None
        if active_log is not None:
            text=active_log.read_text(errors='replace')
            initial=re.findall(r'Vulkan0 compute buffer size =\s*([\d.]+) MiB',text)
            final=re.findall(r'Vulkan0 compute buffer size of ([\d.]+) MiB',text)
            active_rows=re.findall(r'active-only K-shift, cells = (\d+) / (\d+)',text)
            report.setdefault('compute_memory',{})[active_phase]={
                'initial_mib':float(initial[0]) if initial else None,
                'exit_mismatch_mib':float(final[-1]) if final else None,
                'active_shift_rows':[[int(a),int(b)] for a,b in active_rows]}
            active_log=active_phase=None

    def start(phase,ram_mib):
        nonlocal child,log_file,active_log,active_phase
        shutdown()
        command=list(args)
        command[command.index('--cache-ram')+1]=str(ram_mib)
        command+=['--cache-reuse','0']
        detail='-'+options.case if options.case else ''
        if options.probe_decode:
            detail+='-decode-probe'
        log_path=ROOT/('logs/native-reuse-%s-%s%s.log' % (suffix,phase,detail))
        active_log,active_phase=log_path,phase
        log_file=log_path.open('w')
        child=subprocess.Popen(command,env=env,stdout=log_file,stderr=subprocess.STDOUT)
        print('Starting %s phase; log: %s' % (phase,log_path),flush=True)
        deadline=time.monotonic()+180
        while time.monotonic()<deadline:
            if child.poll() is not None:
                raise RuntimeError('Experimental server exited; inspect '+str(log_path))
            try:
                if api('/health').get('status')=='ok':
                    props=api('/props')
                    assert props['default_generation_settings']['n_ctx']==report['context']
                    startup=log_path.read_text(errors='replace')
                    assert 'K (q8_0)' in startup and 'V (q8_0)' in startup, 'Q8 not active'
                    if not options.cpu:
                        assert 'offloaded 37/37' in startup
                        assert 'experimental AMD MoltenVK shared FA only = 1; other shuffle disabled' in startup
                    return
            except (OSError,ValueError): pass
            time.sleep(.5)
        raise RuntimeError('Experimental server startup timed out')

    try:
        if not options.cpu:
            status=subprocess.check_output([str(ROOT/'daemon.command'),'status'],text=True)
            assert 'API: ready' in status,'Start the stable daemon first'
            stopped=True
            subprocess.run([str(ROOT/'daemon.command'),'stop'],check=True)
        if options.phase in ('all','prefix'):
            start('prefix',512)
            def prefix_body(name):
                return [{'role':'system','content':'PROJECT_'+name+'\n'+'\n'.join(
                    'Entry %04d: no action needed.' % i for i in range(45))},
                    {'role':'user','content':'What is 2+2? Reply only with 4.'}]
            runs=[request('native RAM A cold',prefix_body('A')),
                  request('native RAM B cold',prefix_body('B')),
                  request('native RAM A return',prefix_body('A'))]
            assert all(r['content']=='4' for r in runs),runs
            timing=runs[-1]['timings']
            reused=timing['cache_n']/(timing['cache_n']+timing['prompt_n'])
            assert reused>.9,('Native RAM prefix was not reused',runs)
            report['prefix']={'passed':True,'requests':runs,
                              'reuse_fraction':reused,'mechanism':'upstream RAM prompt-state cache; not a paged block store'}
            save()
        if options.phase in ('all','shift'):
            start('shift',0)
            probe_messages=[{'role':'user','content':'List the integers from 1 to 40 separated by spaces. Output the numbers only.'}]
            if options.probe_decode:
                report['decode_before_shift']=request('decode probe before shift',probe_messages,cache=False,max_tokens=32)
            padding='\n'.join('Entry %04d: neutral reference record.' % i for i in range(12))
            system=('Inspect only the USER message. If it contains an OVERRIDE line, reply with the value '
                    'from that line. Otherwise reply with the DEFAULT value. Output the chosen value only, '
                    'without explanation. Neutral document entries are irrelevant.')
            cases=[
                ('neutral_removal','DEFAULT=BLUE\nUNUSED_NOTE=archived\n',
                 'DEFAULT=BLUE\n','BLUE','BLUE','Apply the DEFAULT/OVERRIDE rule.'),
                ('override_removal','DEFAULT=BLUE\nOVERRIDE=RED\n',
                 'DEFAULT=BLUE\n','BLUE','RED','Apply the DEFAULT/OVERRIDE rule.'),
                ('value_override_removal','DEFAULT=A7C29F\nOVERRIDE=B4D81E\n',
                 'DEFAULT=A7C29F\n','A7C29F','B4D81E','Apply the DEFAULT/OVERRIDE rule.'),
                ('replacement_prefix','DEFAULT=BLUE\nOVERRIDE=RED\n',
                 'DEFAULT=BLUE\nOVERRIDE=GREEN\n','GREEN','RED','Apply the DEFAULT/OVERRIDE rule.'),
                ('context_sensitive_removal','DEFAULT=BLUE\nOVERRIDE=RED\n',
                 'DEFAULT=BLUE\n','BLUE','RED','Apply the rule.',
                 'Use the OVERRIDE value if present; otherwise use DEFAULT. Output the chosen value only.',
                 '\n'.join('Entry %04d: neutral record.' % i for i in range(23)))]
            for case in cases:
                name,old_head,new_head,expected,old_expected,question,case_system,case_padding=decode_case(case,system,padding)
                if options.case and name!=options.case:
                    continue
                def messages(head):
                    return [{'role':'system','content':case_system},
                            {'role':'user','content':head+'DOCUMENT\n'+case_padding+'\n'+question}]
                baseline=request(name+' full recompute',messages(new_head),cache=False)
                warm=request(name+' source warmup',messages(old_head),cache=False)
                exact=request(name+' exact-prefix control',messages(new_head),reuse=0)
                warm_again=request(name+' source warmup again',messages(old_head),cache=False)
                shifted=request(name+' KV chunk shift',messages(new_head),reuse=64)
                normalize=lambda x:x.strip().upper()
                valid=(normalize(baseline['content'])==expected.upper()
                       and normalize(warm['content'])==old_expected.upper()
                       and normalize(warm_again['content'])==old_expected.upper()
                       and normalize(exact['content'])==expected.upper())
                entry={'case':name,'baseline':baseline,'source':warm,'source_again':warm_again,
                       'exact_prefix':exact,'shifted':shifted,
                       'expected':expected,'controls_correct':valid,
                       'shift_answer_matches':normalize(shifted['content'])==expected.upper(),
                       'shift_extra_cached_tokens':shifted['timings']['cache_n']-exact['timings']['cache_n']}
                report['shift'].append(entry)
                save()
            report['shift_controls_passed']=all(e['controls_correct'] for e in report['shift'])
            report['shift_answers_passed']=all(e['shift_answer_matches'] for e in report['shift'])
            report['shift_observed']=any(e['shift_extra_cached_tokens']>=64 for e in report['shift'])
            if options.active_only_shift and report['shift_observed']:
                assert 'active-only K-shift, cells =' in active_log.read_text(errors='replace'), 'Active-only shift not confirmed'
            if options.probe_decode:
                after=request('decode probe after shift',probe_messages,cache=False,max_tokens=32)
                report['decode_after_shift']=after
                before_rate=report['decode_before_shift']['timings']['predicted_per_second']
                after_rate=after['timings']['predicted_per_second']
                report['decode_after_before_ratio']=after_rate/before_rate
                print('Decode before/after shift: %.2f -> %.2f tokens/s' % (before_rate,after_rate),flush=True)
            print('Experimental shift: controls=%s, answers=%s, actual chunk reuse=%s' %
                (report['shift_controls_passed'],report['shift_answers_passed'],report['shift_observed']),flush=True)
            print('Experimental KV shifting remains disabled in the production daemon.',flush=True)
            report['shift_note']='Small experiment only; KV shifting is not full attention recomputation or CacheBlend. Not enabled in the daemon.'
        report['completed']=True
        save()
        print('Saved: '+str(output),flush=True)
    except BaseException as exc:
        report['error']=str(exc)
        save()
        raise
    finally:
        try:
            shutdown()
            save()
        finally:
            if stopped:
                print('Restoring the original daemon profile...',flush=True)
                subprocess.run([str(ROOT/'daemon.command'),'start'],check=True)


if __name__=='__main__':
    def interrupted(signum,frame): raise KeyboardInterrupt('Signal %d' % signum)
    signal.signal(signal.SIGTERM,interrupted)
    signal.signal(signal.SIGHUP,interrupted)
    main()
